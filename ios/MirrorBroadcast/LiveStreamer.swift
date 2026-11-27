// "Go Live" mode: the iPhone streams by itself to Facebook / YouTube / any
// RTMP(S) server - no laptop. Like PRISM Live Studio, but with a stream key.
//
//   ReplayKit frame -> FrameShaper (rotate upright + fit 16:9 / 9:16) ->
//   HaishinKit MediaMixer + RTMPStream (H.264 + AAC) -> RTMPS to the platform
//   game sound (track 1) + iPhone mic (track 0) are mixed by HaishinKit.
//
// Library: HaishinKit 2.2.5 (BSD-3-Clause), https://github.com/HaishinKit/HaishinKit.swift

import CoreMedia
import CoreVideo
import Foundation
import HaishinKit
import Network
import ReplayKit
import RTMPHaishinKit
import UIKit
import VideoToolbox

// MARK: - settings handoff from the app

enum ConfigFetcher {
    private static let cacheKey = "lastLiveConfig"

    /// Ask the app (127.0.0.1:2399) for its settings; fall back to the last copy.
    static func fetch(timeout: TimeInterval = 2.0, done: @escaping (LiveConfig) -> Void) {
        request(timeout: timeout) { fresh in
            if let fresh = fresh {
                UserDefaults.standard.set(fresh.encoded(), forKey: cacheKey)
                NSLog("[Mirror] settings from the app: mode \(fresh.mode)")
                done(fresh)
            } else {
                let cached = UserDefaults.standard.data(forKey: cacheKey).flatMap(LiveConfig.decode) ?? LiveConfig()
                NSLog("[Mirror] app not reachable - using last settings: mode \(cached.mode)")
                done(cached)
            }
        }
    }

    /// One request; nil when the app is not open.
    static func request(timeout: TimeInterval, done: @escaping (LiveConfig?) -> Void) {
        let queue = DispatchQueue(label: "config.fetch")
        let conn = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: LinkPorts.config)!, using: .tcp)
        var finished = false
        var data = Data()

        func finish(_ fresh: LiveConfig?) {
            guard !finished else { return }
            finished = true
            conn.cancel()
            done(fresh)
        }

        func receive() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { chunk, _, complete, error in
                if let chunk = chunk { data.append(chunk) }
                if complete || error != nil {
                    finish(LiveConfig.decode(data))
                } else {
                    receive()
                }
            }
        }
        conn.stateUpdateHandler = { state in
            switch state {
            case .ready: receive()
            case .failed, .waiting: finish(nil)
            default: break
            }
        }
        conn.start(queue: queue)
        queue.asyncAfter(deadline: .now() + timeout) { finish(LiveConfig.decode(data)) }
    }
}

// MARK: - rotate + fit frames

/// Makes every ReplayKit frame upright and exactly width x height
/// (black bars when the shapes differ), using the GPU (VideoToolbox).
final class FrameShaper {
    let width: Int
    let height: Int
    private var rotation: VTPixelRotationSession?
    private var transfer: VTPixelTransferSession?
    private var rotatedPool: CVPixelBufferPool?
    private var rotatedKey = ""
    private var outPool: CVPixelBufferPool?
    private var format: CMVideoFormatDescription?
    private var card: CVPixelBuffer?

    init(width: Int, height: Int) {
        self.width = width
        self.height = height
        VTPixelTransferSessionCreate(allocator: nil, pixelTransferSessionOut: &transfer)
        if let t = transfer {
            VTSessionSetProperty(t, key: kVTPixelTransferPropertyKey_ScalingMode, value: kVTScalingMode_Letterbox)
        }
        VTPixelRotationSessionCreate(nil, &rotation)
    }

    deinit {
        if let t = transfer { VTPixelTransferSessionInvalidate(t) }
        if let r = rotation { VTPixelRotationSessionInvalidate(r) }
    }

    private static func pool(_ w: Int, _ h: Int, _ fmt: OSType) -> CVPixelBufferPool? {
        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: fmt,
            kCVPixelBufferWidthKey as String: w,
            kCVPixelBufferHeightKey as String: h,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()
        ]
        var p: CVPixelBufferPool?
        CVPixelBufferPoolCreate(nil, [kCVPixelBufferPoolMinimumBufferCountKey as String: 3] as CFDictionary,
                                attrs as CFDictionary, &p)
        return p
    }

    /// orientation = CGImagePropertyOrientation from ReplayKit (1 up, 3 down, 6 / 8 sideways)
    func shape(_ src: CVPixelBuffer, orientation: UInt8, pts: CMTime) -> CMSampleBuffer? {
        let fmt = CVPixelBufferGetPixelFormatType(src)
        var upright = src
        let turn: CFString?
        var quarter = true                       // 90 degree turn: width and height swap
        switch orientation {
        case 3, 4: turn = kVTRotation_180; quarter = false
        case 5, 6: turn = kVTRotation_CCW90
        case 7, 8: turn = kVTRotation_CW90
        default: turn = nil
        }
        if let turn = turn, let rot = rotation {
            let sw = CVPixelBufferGetWidth(src), sh = CVPixelBufferGetHeight(src)
            let (rw, rh) = quarter ? (sh, sw) : (sw, sh)
            let key = "\(rw)x\(rh)/\(fmt)"
            if key != rotatedKey { rotatedPool = Self.pool(rw, rh, fmt); rotatedKey = key }
            var dst: CVPixelBuffer?
            if let p = rotatedPool { CVPixelBufferPoolCreatePixelBuffer(nil, p, &dst) }
            guard let d = dst else { return nil }
            VTSessionSetProperty(rot, key: kVTPixelRotationPropertyKey_Rotation, value: turn)
            guard VTPixelRotationSessionRotateImage(rot, src, d) == noErr else { return nil }
            upright = d
        }
        if outPool == nil { outPool = Self.pool(width, height, fmt) }
        var out: CVPixelBuffer?
        if let p = outPool { CVPixelBufferPoolCreatePixelBuffer(nil, p, &out) }
        guard let o = out, let t = transfer, VTPixelTransferSessionTransferImage(t, from: upright, to: o) == noErr
        else { return nil }
        return Self.sampleBuffer(o, pts: pts, format: &format)
    }

    /// "Be right back" picture (made once, in the same pixel format as the
    /// screen frames so the encoder never sees a format change).
    func privacyCard(like src: CVPixelBuffer) -> CVPixelBuffer? {
        if let c = card { return c }
        let fmt = CVPixelBufferGetPixelFormatType(src)
        var bgra: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA,
                            [kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()] as CFDictionary, &bgra)
        guard let b = bgra else { return nil }
        CVPixelBufferLockBaseAddress(b, [])
        if let ctx = CGContext(data: CVPixelBufferGetBaseAddress(b), width: width, height: height, bitsPerComponent: 8,
                               bytesPerRow: CVPixelBufferGetBytesPerRow(b), space: CGColorSpaceCreateDeviceRGB(),
                               bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) {
            Self.drawCard(ctx, width: CGFloat(width), height: CGFloat(height))
        }
        CVPixelBufferUnlockBaseAddress(b, [])
        if outPool == nil { outPool = Self.pool(width, height, fmt) }
        var out: CVPixelBuffer?
        if let p = outPool { CVPixelBufferPoolCreatePixelBuffer(nil, p, &out) }
        guard let o = out, let t = transfer, VTPixelTransferSessionTransferImage(t, from: b, to: o) == noErr else { return nil }
        card = o
        return o
    }

    private static func drawCard(_ ctx: CGContext, width w: CGFloat, height h: CGFloat) {
        let space = CGColorSpaceCreateDeviceRGB()
        let colors = [UIColor(red: 0.10, green: 0.05, blue: 0.20, alpha: 1).cgColor,
                      UIColor(red: 0.45, green: 0.08, blue: 0.20, alpha: 1).cgColor] as CFArray
        if let g = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) {
            ctx.drawLinearGradient(g, start: .zero, end: CGPoint(x: w, y: h), options: [])
        }
        // UIKit text is drawn top-down: flip the context
        ctx.translateBy(x: 0, y: h)
        ctx.scaleBy(x: 1, y: -1)
        UIGraphicsPushContext(ctx)
        let short = min(w, h)
        let center = NSMutableParagraphStyle()
        center.alignment = .center
        let title: [NSAttributedString.Key: Any] = [
            .font: UIFont.systemFont(ofSize: short * 0.12, weight: .heavy),
            .foregroundColor: UIColor.white, .paragraphStyle: center]
        let sub: [NSAttributedString.Key: Any] = [
            .font: UIFont.systemFont(ofSize: short * 0.05, weight: .semibold),
            .foregroundColor: UIColor(white: 1, alpha: 0.75), .paragraphStyle: center]
        ("\u{23F8}  Be right back" as NSString)
            .draw(in: CGRect(x: 0, y: h * 0.5 - short * 0.13, width: w, height: short * 0.16), withAttributes: title)
        ("Stay tuned \u{2014} the stream is still on" as NSString)
            .draw(in: CGRect(x: 0, y: h * 0.5 + short * 0.05, width: w, height: short * 0.08), withAttributes: sub)
        UIGraphicsPopContext()
    }

    static func sampleBuffer(_ px: CVPixelBuffer, pts: CMTime, format: inout CMVideoFormatDescription?) -> CMSampleBuffer? {
        if format == nil || !CMVideoFormatDescriptionMatchesImageBuffer(format!, imageBuffer: px) {
            CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: px, formatDescriptionOut: &format)
        }
        guard let f = format else { return nil }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: pts, decodeTimeStamp: .invalid)
        var sb: CMSampleBuffer?
        CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: px, formatDescription: f,
                                                 sampleTiming: &timing, sampleBufferOut: &sb)
        return sb
    }
}

// MARK: - live streamer

final class LiveStreamer: @unchecked Sendable {
    /// Called when the live cannot continue (shown by iOS as an alert).
    var onFatal: ((String) -> Void)?

    private let cfg: LiveConfig
    private let shaper: FrameShaper
    private let mixer = MediaMixer(captureSessionMode: .manual, multiTrackAudioMixingEnabled: true)
    private var session: (any Session)?
    private let statusServer = MirrorServer(port: LinkPorts.status)

    // ordered hand-off of buffers into the async library
    private var feed: AsyncStream<(CMSampleBuffer, UInt8)>.Continuation?
    private var feedTask: Task<Void, Never>?

    private let lock = NSLock()
    private var status = LiveStatus()
    private var startedAt: Date?
    private var stopped = false
    private var lastPTS = CMTime.invalid
    private var lastFrame: CVPixelBuffer?
    private var lastFrameTime = CFAbsoluteTimeGetCurrent()
    private var repeatFormat: CMVideoFormatDescription?
    private var framesThisSecond = 0
    private var micSeen = false
    private var privacy = false             // live controls from the app
    private var micMuted = false
    private var privacyFrame: CVPixelBuffer?
    private var repeatPrivacyFormat: CMVideoFormatDescription?
    private var idleTimer: DispatchSourceTimer?
    private let minFrameGap: Double

    init(config: LiveConfig) {
        cfg = config
        shaper = FrameShaper(width: config.width, height: config.height)
        minFrameGap = 1.0 / Double(max(1, config.fps)) - 0.004
        status.width = config.width
        status.height = config.height
        privacy = config.privacy
        micMuted = config.micMuted
    }

    // MARK: start / stop

    func start() {
        guard let url = cfg.publishURL, ["rtmp", "rtmps"].contains(url.scheme?.lowercased() ?? "") else {
            onFatal?("Go Live: the server or stream key is missing or wrong. Open USB Mirror \u{2192} Go Live and check them.")
            return
        }
        setState("connecting")
        statusServer.onTick = { [weak self] in self?.tick() }
        statusServer.start()

        var cont: AsyncStream<(CMSampleBuffer, UInt8)>.Continuation?
        let stream = AsyncStream<(CMSampleBuffer, UInt8)>(bufferingPolicy: .bufferingNewest(40)) { cont = $0 }
        feed = cont
        let mixer = self.mixer
        feedTask = Task {
            for await (sb, track) in stream {
                await mixer.append(sb, track: track)
            }
        }

        startIdleTimer()
        Task { await self.setUp(url) }
    }

    func stop() {
        lock.lock(); stopped = true; lock.unlock()
        idleTimer?.cancel(); idleTimer = nil
        feed?.finish()
        setState("ended")
        let session = self.session, mixer = self.mixer
        Task {
            try? await session?.close()
            await mixer.stopRunning()
        }
        statusServer.stop()
    }

    private func setUp(_ url: URL) async {
        do {
            await SessionBuilderFactory.shared.register(RTMPSessionFactory())
            guard let s = try await SessionBuilderFactory.shared.make(url).build() else {
                throw NSError(domain: "USBMirror", code: 2, userInfo: [NSLocalizedDescriptionKey: "unsupported address"])
            }
            session = s
            let stream = await s.stream

            var video = await stream.videoSettings
            video.videoSize = CGSize(width: cfg.width, height: cfg.height)
            video.bitRate = cfg.bitrate
            video.profileLevel = kVTProfileLevel_H264_High_AutoLevel as String
            video.maxKeyFrameIntervalDuration = 2          // platforms want a keyframe every 2 s
            video.expectedFrameRate = Double(cfg.fps)
            try await stream.setVideoSettings(video)

            var audio = await stream.audioSettings
            audio.bitRate = 128_000
            try await stream.setAudioSettings(audio)

            await stream.setBitRateStrategy(StreamVideoAdaptiveBitRateStrategy(mamimumVideoBitrate: cfg.bitrate))
            await stream.setVideoInputBufferCounts(5)          // ReplayKit has ~50 MB: keep queues short

            var vm = await mixer.videoMixerSettings
            vm.mode = .passthrough                             // frames are already shaped
            await mixer.setVideoMixerSettings(vm)
            var am = await mixer.audioMixerSettings
            am.mainTrack = 1                                   // game sound always exists; mic is optional
            am.tracks[0] = AudioMixerTrackSettings(volume: Float(cfg.micVolume))
            am.tracks[1] = AudioMixerTrackSettings(volume: Float(cfg.gameVolume))
            await mixer.setAudioMixerSettings(am)
            await mixer.startRunning()
            await mixer.addOutput(stream)

            await connect(first: true)
        } catch {
            fail("Go Live could not start: \(error.localizedDescription)")
        }
    }

    private func connect(first: Bool) async {
        guard let s = session, !isStopped else { return }
        setState(first ? "connecting" : "reconnecting")
        do {
            try await s.connect { [weak self] in
                self?.disconnected()
            }
            lock.lock()
            if startedAt == nil { startedAt = Date() }
            status.message = ""
            lock.unlock()
            setState("live")
            NSLog("[Mirror] LIVE")
        } catch {
            NSLog("[Mirror] live connect failed: \(error)")
            if first {
                fail("Could not go live. Check the stream key and server (\(cfg.platform)), and that the live is set up on the website. (\(error.localizedDescription))")
            } else {
                retryLater()
            }
        }
    }

    private func disconnected() {
        guard !isStopped else { return }
        NSLog("[Mirror] live connection lost - reconnecting")
        retryLater()
    }

    private func retryLater() {
        lock.lock()
        status.reconnects += 1
        let n = status.reconnects
        lock.unlock()
        if n > 10 {
            fail("The live connection was lost (no internet?). The broadcast was stopped.")
            return
        }
        setState("reconnecting")
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            await self.connect(first: false)
        }
    }

    private func fail(_ message: String) {
        lock.lock(); status.message = message; lock.unlock()
        setState("error")
        sendStatus()
        onFatal?(message)
    }

    private var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }

    private func setState(_ s: String) {
        lock.lock(); status.state = s; lock.unlock()
    }

    // MARK: samples from ReplayKit

    func process(_ sb: CMSampleBuffer, type: RPSampleBufferType) {
        guard let feed = feed, !isStopped else { return }
        switch type {
        case .video:
            let pts = CMSampleBufferGetPresentationTimeStamp(sb)
            if lastPTS.isValid && (pts - lastPTS).seconds < minFrameGap { return }   // keep the chosen fps
            guard let px = CMSampleBufferGetImageBuffer(sb) else { return }
            var orient: UInt8 = 1
            if let n = CMGetAttachment(sb, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber {
                orient = n.uint8Value
            }
            lock.lock(); let hide = privacy; lock.unlock()
            let shaped: CMSampleBuffer
            if hide {
                // privacy screen: viewers see the card, not your screen
                guard let card = shaper.privacyCard(like: px),
                      let sb2 = FrameShaper.sampleBuffer(card, pts: pts, format: &repeatPrivacyFormat) else { return }
                lock.lock(); privacyFrame = card; lock.unlock()
                shaped = sb2
            } else {
                guard let sb2 = shaper.shape(px, orientation: orient, pts: pts) else { return }
                shaped = sb2
            }
            lastPTS = pts
            lock.lock()
            lastFrame = CMSampleBufferGetImageBuffer(shaped)
            lastFrameTime = CFAbsoluteTimeGetCurrent()
            framesThisSecond += 1
            lock.unlock()
            feed.yield((shaped, 0))
        case .audioApp:
            if sb.dataReadiness == .ready { feed.yield((sb, 1)) }
        case .audioMic:
            micSeen = true
            lock.lock(); let muted = micMuted; lock.unlock()
            if !muted && sb.dataReadiness == .ready { feed.yield((sb, 0)) }
        @unknown default:
            break
        }
    }

    /// A still screen gives no new frames; platforms then think the stream
    /// died. Repeat the last frame every 0.25 s while nothing changes.
    private func startIdleTimer() {
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "live.idle"))
        t.schedule(deadline: .now() + 0.5, repeating: 0.25)
        t.setEventHandler { [weak self] in
            guard let self = self, let feed = self.feed else { return }
            self.lock.lock()
            let idle = CFAbsoluteTimeGetCurrent() - self.lastFrameTime > 0.4
            let frame = (self.privacy ? self.privacyFrame : nil) ?? self.lastFrame
            self.lock.unlock()
            guard idle, let px = frame else { return }
            let pts = CMClockGetTime(CMClockGetHostTimeClock())
            if let sb = FrameShaper.sampleBuffer(px, pts: pts, format: &self.repeatFormat) {
                self.lock.lock(); self.lastFrameTime = CFAbsoluteTimeGetCurrent(); self.lock.unlock()
                feed.yield((sb, 0))
            }
        }
        t.resume()
        idleTimer = t
    }

    // MARK: status to the app (once per second)

    private func tick() {
        // live controls: the app answers only while it is open (it keeps the last values otherwise)
        ConfigFetcher.request(timeout: 0.6) { [weak self] fresh in
            guard let self = self, let c = fresh, c.isLive else { return }
            self.lock.lock()
            if c.privacy != self.privacy || c.micMuted != self.micMuted {
                NSLog("[Mirror] live controls: privacy \(c.privacy), mic muted \(c.micMuted)")
            }
            self.privacy = c.privacy
            self.micMuted = c.micMuted
            self.lock.unlock()
        }
        let session = self.session
        Task {
            var kbps = 0, target = 0
            if let s = session {
                let stream = await s.stream
                target = await stream.videoSettings.bitRate / 1000
                if let rtmp = stream as? RTMPStream {
                    kbps = await rtmp.info.currentBytesPerSecond * 8 / 1000
                }
            }
            self.lock.lock()
            self.status.kbps = kbps
            self.status.targetKbps = target
            self.status.fps = self.framesThisSecond
            self.framesThisSecond = 0
            self.status.mic = self.micSeen
            self.status.micMuted = self.micMuted
            self.status.privacy = self.privacy
            if let t = self.startedAt { self.status.seconds = Int(Date().timeIntervalSince(t)) }
            self.lock.unlock()
            self.sendStatus()
        }
    }

    private func sendStatus() {
        lock.lock(); let s = status; lock.unlock()
        guard let json = try? JSONEncoder().encode(s) else { return }
        statusServer.send(Packet.make(.info, timestampUs: 0, payload: json), isVideo: false)
    }
}
