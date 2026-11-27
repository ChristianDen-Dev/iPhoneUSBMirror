// Broadcast Upload Extension entry point. ReplayKit calls
// processSampleBuffer for every screen frame and audio buffer while the
// broadcast runs. We encode/convert immediately and send over USB.

import ReplayKit
import CoreMedia
import CoreVideo

class SampleHandler: RPBroadcastSampleHandler {
    static let port: UInt16 = 2345

    private let server = MirrorServer(port: SampleHandler.port)
    private let audio = AppAudioConverter()
    private var encoder: VideoEncoder?

    private let stateLock = NSLock()
    private var lastOrientation: UInt8 = 0
    private var orientationDirty = true   // resend orientation (e.g. new client)
    private var sourceDescription = "none" // last ReplayKit buffer size/format (diagnostics)
    private var encoderDescription = "none"
    private var tickCount = 0

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        server.onClientConnected = { [weak self] in
            guard let self = self else { return }
            self.stateLock.lock(); self.orientationDirty = true; self.stateLock.unlock()
            self.encoder?.requestKeyframe()
        }
        server.onTick = { [weak self] in
            guard let self = self, self.server.isClientConnected else { return }
            self.encoder?.repeatLastFrameIfIdle()
            self.tickCount += 1
            if self.tickCount % 5 == 1 { self.sendInfo() }   // every ~5 s
        }
        server.start()
    }

    override func broadcastPaused() {}
    override func broadcastResumed() { encoder?.requestKeyframe() }

    override func broadcastFinished() {
        server.stop()
        encoder?.invalidate()
        encoder = nil
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        switch sampleBufferType {
        case .video:
            handleVideo(sampleBuffer)
        case .audioApp:
            handleAudio(sampleBuffer)
        case .audioMic:
            break   // game sound only
        @unknown default:
            break
        }
    }

    // MARK: video

    private func handleVideo(_ sb: CMSampleBuffer) {
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)

        // Orientation comes as an attachment (CGImagePropertyOrientation raw value).
        if let n = CMGetAttachment(sb, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber {
            sendOrientationIfNeeded(n.uint8Value, pts: pts)
        }

        // No receiver connected -> don't even encode (saves battery).
        guard server.isClientConnected, let pixelBuffer = CMSampleBufferGetImageBuffer(sb) else { return }

        let w = CVPixelBufferGetWidth(pixelBuffer)
        let h = CVPixelBufferGetHeight(pixelBuffer)
        if encoder == nil || !(encoder!.matches(width: w, height: h)) {
            encoder?.invalidate()
            encoder = VideoEncoder(sourceWidth: w, sourceHeight: h)
            encoder?.onPacket = { [weak self] packet in self?.server.send(packet, isVideo: true) }
            stateLock.lock()
            sourceDescription = "\(w)x\(h) \(fourCC(CVPixelBufferGetPixelFormatType(pixelBuffer)))"
            encoderDescription = encoder.map { "\($0.width)x\($0.height)" } ?? "FAILED"
            stateLock.unlock()
            sendInfo()
        }
        guard let enc = encoder else { return }

        // Busy encoder or slow socket -> DROP this frame (never queue).
        guard enc.isReady, server.canSendVideo else { return }
        enc.encode(pixelBuffer, pts: pts)
    }

    private func sendOrientationIfNeeded(_ value: UInt8, pts: CMTime) {
        stateLock.lock()
        let changed = value != lastOrientation || orientationDirty
        lastOrientation = value
        orientationDirty = false
        stateLock.unlock()
        guard changed, server.isClientConnected else {
            if changed { stateLock.lock(); orientationDirty = true; stateLock.unlock() }
            return
        }
        server.send(Packet.make(.orientation, timestampUs: microseconds(pts), payload: Data([value])), isVideo: false)
    }

    // MARK: diagnostics

    private func sendInfo() {
        stateLock.lock()
        let src = sourceDescription, enc = encoderDescription, orient = lastOrientation
        stateLock.unlock()
        let thermal: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: thermal = "nominal"
        case .fair: thermal = "fair"
        case .serious: thermal = "SERIOUS"
        case .critical: thermal = "CRITICAL"
        @unknown default: thermal = "?"
        }
        let text = String(format: "source %@ -> encode %@ | orient %d | mem %.1f MB | thermal %@ | lowPower %@",
                          src, enc, Int(orient), memoryFootprintMB(), thermal,
                          ProcessInfo.processInfo.isLowPowerModeEnabled ? "on" : "off")
        server.send(Packet.make(.info, timestampUs: 0, payload: Data(text.utf8)), isVideo: false)
    }

    // MARK: audio

    private func handleAudio(_ sb: CMSampleBuffer) {
        guard server.isClientConnected, let pcm = audio.convert(sb) else { return }
        let ts = microseconds(CMSampleBufferGetPresentationTimeStamp(sb))
        server.send(Packet.make(.audioPCM, timestampUs: ts, payload: pcm), isVideo: false)
    }
}
