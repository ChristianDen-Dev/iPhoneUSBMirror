// Hardware H.264 encoder (VideoToolbox), tuned for low latency:
// real-time, Main profile, no B-frames (no frame reordering), ~8 Mbps,
// keyframe at least every 1 s. Frames are downscaled so the long side is
// at most 1920 px. Output is converted to Annex-B and handed to `onPacket`.
//
// Memory rules (extension limit ~50 MB):
// - never queue raw frames: if the encoder or the socket is busy, the
//   caller drops the frame BEFORE encoding (so P-frames stay valid).
// - one small pixel-buffer pool for the scaled frames.

import Foundation
import VideoToolbox
import CoreMedia
import CoreVideo

final class VideoEncoder {
    /// Called from the VideoToolbox thread with ready-to-send packets.
    var onPacket: ((Data) -> Void)?

    static let maxLongSide = 1920
    static let bitrate = 8_000_000          // bits per second (average)
    static let peakBytesPerSecond = 1_500_000 // ~12 Mbps hard cap
    static let fps = 60

    let width: Int
    let height: Int
    private let sourceWidth: Int
    private let sourceHeight: Int

    private var session: VTCompressionSession?
    private var transfer: VTPixelTransferSession?
    private let lock = NSLock()          // guards session / buffers / timestamps
    private let countLock = NSLock()     // guards framesInFlight only (used by the VT callback)
    private var framesInFlight = 0
    private var forceKeyframe = true
    private var lastScaled: CVPixelBuffer?
    private var lastPTS = CMTime.invalid
    private var lastEncodeHostTime = CFAbsoluteTimeGetCurrent()

    /// Returns nil if VideoToolbox refuses to create the session.
    init?(sourceWidth: Int, sourceHeight: Int) {
        self.sourceWidth = sourceWidth
        self.sourceHeight = sourceHeight
        // Keep aspect ratio, long side <= 1920, even dimensions.
        let longSide = max(sourceWidth, sourceHeight)
        let scale = longSide > Self.maxLongSide ? Double(Self.maxLongSide) / Double(longSide) : 1.0
        width = max(2, Int((Double(sourceWidth) * scale) / 2) * 2)
        height = max(2, Int((Double(sourceHeight) * scale) / 2) * 2)

        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()
        ]

        var s: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: nil,
            imageBufferAttributes: attrs as CFDictionary,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refcon: nil,
            compressionSessionOut: &s)
        guard status == noErr, let session = s else {
            NSLog("[Mirror] VTCompressionSessionCreate failed: \(status)")
            return nil
        }
        self.session = session

        func set(_ key: CFString, _ value: CFTypeRef) {
            let st = VTSessionSetProperty(session, key: key, value: value)
            if st != noErr { NSLog("[Mirror] VT property \(key) not set: \(st)") }
        }
        set(kVTCompressionPropertyKey_RealTime, kCFBooleanTrue)
        set(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_Main_AutoLevel)
        set(kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse)   // no B-frames
        set(kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: Self.bitrate))
        set(kVTCompressionPropertyKey_DataRateLimits,
            [NSNumber(value: Self.peakBytesPerSecond), NSNumber(value: 1)] as CFArray)
        set(kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: Self.fps))
        set(kVTCompressionPropertyKey_MaxKeyFrameInterval, NSNumber(value: Self.fps))
        set(kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, NSNumber(value: 1.0))
        VTCompressionSessionPrepareToEncodeFrames(session)

        if width != sourceWidth || height != sourceHeight {
            var t: VTPixelTransferSession?
            if VTPixelTransferSessionCreate(allocator: kCFAllocatorDefault, pixelTransferSessionOut: &t) == noErr {
                transfer = t
            }
        }
        NSLog("[Mirror] encoder \(sourceWidth)x\(sourceHeight) -> \(width)x\(height)")
    }

    deinit { invalidate() }

    func invalidate() {
        lock.lock(); defer { lock.unlock() }
        // The output callback never takes `lock`, so waiting here is safe.
        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
        }
        session = nil
        if let t = transfer { VTPixelTransferSessionInvalidate(t) }
        transfer = nil
        lastScaled = nil
    }

    private var inFlight: Int {
        countLock.lock(); defer { countLock.unlock() }
        return framesInFlight
    }

    private func changeInFlight(_ delta: Int) {
        countLock.lock(); framesInFlight = max(0, framesInFlight + delta); countLock.unlock()
    }

    func matches(width w: Int, height h: Int) -> Bool { w == sourceWidth && h == sourceHeight }

    func requestKeyframe() {
        lock.lock(); forceKeyframe = true; lock.unlock()
    }

    /// True if the encoder can take another frame right now.
    var isReady: Bool {
        lock.lock(); let hasSession = session != nil; lock.unlock()
        return hasSession && inFlight < 2
    }

    /// Encode one ReplayKit frame (call only when `isReady` and the socket can take it).
    func encode(_ pixelBuffer: CVPixelBuffer, pts: CMTime) {
        lock.lock(); defer { lock.unlock() }
        guard let session = session else { return }

        // Downscale into a buffer from the encoder's own pool.
        var input = pixelBuffer
        if let transfer = transfer, let pool = VTCompressionSessionGetPixelBufferPool(session) {
            var out: CVPixelBuffer?
            guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &out) == kCVReturnSuccess,
                  let scaled = out,
                  VTPixelTransferSessionTransferImage(transfer, from: pixelBuffer, to: scaled) == noErr
            else { return }   // drop this frame
            input = scaled
            lastScaled = scaled
        } else {
            lastScaled = nil // don't hold ReplayKit's own buffers
        }
        encodeLocked(session, input, pts: pts)
    }

    /// If nothing changed on screen for a while (ReplayKit sends no frames),
    /// re-send the last image as a keyframe so a newly connected receiver
    /// gets a picture. Called from the 1 s timer.
    func repeatLastFrameIfIdle() {
        lock.lock(); defer { lock.unlock() }
        guard let session = session, let last = lastScaled, inFlight == 0,
              CFAbsoluteTimeGetCurrent() - lastEncodeHostTime > 1.0 else { return }
        forceKeyframe = true
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        let pts = lastPTS.isValid ? CMTimeMaximum(now, CMTimeAdd(lastPTS, CMTime(value: 1, timescale: 1000))) : now
        encodeLocked(session, last, pts: pts)
    }

    // MARK: private

    /// Must be called with `lock` held.
    private func encodeLocked(_ session: VTCompressionSession, _ buffer: CVPixelBuffer, pts: CMTime) {
        // Timestamps must increase.
        var pts = pts
        if lastPTS.isValid && CMTimeCompare(pts, lastPTS) <= 0 {
            pts = CMTimeAdd(lastPTS, CMTime(value: 1, timescale: 1000))
        }
        lastPTS = pts
        lastEncodeHostTime = CFAbsoluteTimeGetCurrent()

        var props: CFDictionary?
        if forceKeyframe {
            props = [kVTEncodeFrameOptionKey_ForceKeyFrame as String: true] as CFDictionary
            forceKeyframe = false
        }
        changeInFlight(+1)
        let status = VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: buffer,
            presentationTimeStamp: pts,
            duration: .invalid,
            frameProperties: props,
            infoFlagsOut: nil
        ) { [weak self] status, _, sampleBuffer in
            guard let self = self else { return }
            self.changeInFlight(-1)
            guard status == noErr, let sb = sampleBuffer else { return }
            self.handleEncoded(sb)
        }
        if status != noErr {
            changeInFlight(-1)
            forceKeyframe = true
            NSLog("[Mirror] encode failed: \(status)")
        }
    }

    private static let startCode: [UInt8] = [0, 0, 0, 1]

    private func handleEncoded(_ sb: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sb),
              let fmt = CMSampleBufferGetFormatDescription(sb),
              let block = CMSampleBufferGetDataBuffer(sb) else { return }

        let ts = microseconds(CMSampleBufferGetPresentationTimeStamp(sb))

        // Keyframe = no "NotSync" attachment.
        var isKey = true
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[String: Any]],
           let first = attachments.first,
           let notSync = first[kCMSampleAttachmentKey_NotSync as String] as? Bool {
            isKey = !notSync
        }

        // NAL length prefix size (normally 4).
        var nalHeaderLength: Int32 = 4
        var psCount = 0
        CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            fmt, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil,
            parameterSetCountOut: &psCount, nalUnitHeaderLengthOut: &nalHeaderLength)

        // Before every keyframe: send SPS/PPS so a (re)connected receiver can start.
        if isKey {
            var config = Data()
            Packet.appendBE(&config, UInt16(width))
            Packet.appendBE(&config, UInt16(height))
            for i in 0..<psCount {
                var ptr: UnsafePointer<UInt8>?
                var size = 0
                if CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr, parameterSetSizeOut: &size,
                    parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil) == noErr, let p = ptr {
                    config.append(contentsOf: Self.startCode)
                    config.append(p, count: size)
                }
            }
            onPacket?(Packet.make(.videoConfig, timestampUs: ts, payload: config))
        }

        // Copy the AVCC data out and rewrite length prefixes as start codes.
        let total = CMBlockBufferGetDataLength(block)
        guard total > 0 else { return }
        var avcc = Data(count: total)
        let copyStatus = avcc.withUnsafeMutableBytes { raw -> OSStatus in
            CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: total, destination: raw.baseAddress!)
        }
        guard copyStatus == kCMBlockBufferNoErr else { return }

        let lenSize = Int(nalHeaderLength)
        var annexB = Data(capacity: total + 16)
        avcc.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let bytes = raw.bindMemory(to: UInt8.self)
            var offset = 0
            while offset + lenSize <= total {
                var nalLen = 0
                for k in 0..<lenSize { nalLen = (nalLen << 8) | Int(bytes[offset + k]) }
                offset += lenSize
                guard nalLen > 0, offset + nalLen <= total else { break }
                annexB.append(contentsOf: Self.startCode)
                annexB.append(bytes.baseAddress! + offset, count: nalLen)
                offset += nalLen
            }
        }
        guard !annexB.isEmpty else { return }
        onPacket?(Packet.make(.videoFrame, flags: isKey ? PacketFlags.keyframe : 0, timestampUs: ts, payload: annexB))
    }
}
