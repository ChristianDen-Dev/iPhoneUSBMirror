// "Go Live" mode: the iPhone streams by itself to Facebook / YouTube / any
// RTMP(S) server - no laptop. Like PRISM Live Studio, but with a stream key.
//
//   ReplayKit frame -> Compositor (upright, fit/blur/fill, logo, banner, LIVE badge) ->
//   HaishinKit MediaMixer + RTMPStream (H.264 + AAC) -> RTMPS to the platform
//   game sound (track 1) + iPhone mic (track 0) are mixed by HaishinKit.
//
// Library: HaishinKit 2.2.5 (BSD-3-Clause), https://github.com/HaishinKit/HaishinKit.swift

import CoreImage
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

// MARK: - stream look: rotate, fit, overlays (GPU, Core Image)

/// The look settings the compositor uses (copied from LiveConfig).
struct StreamLook: Equatable {
    var fit = "blur"
    var logo = ""
    var logoCorner = 1
    var logoScale = 0.16
    var banner = ""
    var bannerColor = 0
    var badge = true
    var brbText = "Be right back"

    init() {}
    init(_ c: LiveConfig) {
        fit = c.fit; logo = c.logo; logoCorner = c.logoCorner; logoScale = c.logoScale
        banner = c.banner; bannerColor = c.bannerColor; badge = c.badge; brbText = c.brbText
    }
}

/// Builds every frame the viewers see: the game picture turned upright and
/// fitted into the stream size, plus logo, lower-third banner, LIVE badge,
/// or the "Be right back" card. Output: BGRA frames of exactly width x height.
final class Compositor {
    let width: Int
    let height: Int
    private let canvas: CGRect
    private let ctx = CIContext(options: [.cacheIntermediates: false, .name: "live"])
    private let rgb = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
    private var pool: CVPixelBufferPool?
    private let lock = NSLock()

    private var look = StreamLook()
    private var logoImage: CIImage?       // already sized + placed
    private var bannerImage: CIImage?
    private var cardImage: CIImage?
    private var badgeImage: CIImage?
    private var badgeSecond = -1

    init(width: Int, height: Int, look: StreamLook) {
        self.width = width
        self.height = height
        canvas = CGRect(x: 0, y: 0, width: width, height: height)
        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: height,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()
        ]
        CVPixelBufferPoolCreate(nil, [kCVPixelBufferPoolMinimumBufferCountKey as String: 4] as CFDictionary,
                                attrs as CFDictionary, &pool)
        apply(look)
    }

    /// New settings from the app (only rebuilds what changed).
    func update(_ new: StreamLook) {
        lock.lock(); defer { lock.unlock() }
        if new != look { apply(new) }
    }

    private func apply(_ new: StreamLook) {
        let old = look
        look = new
        let W = CGFloat(width), H = CGFloat(height)
        if new.logo != old.logo || new.logoCorner != old.logoCorner || new.logoScale != old.logoScale || logoImage == nil {
            logoImage = nil
            if !new.logo.isEmpty, let d = Data(base64Encoded: new.logo), let ui = UIImage(data: d), let ci = CIImage(image: ui) {
                let h = H * CGFloat(min(max(new.logoScale, 0.05), 0.4))
                let s = h / max(ci.extent.height, 1)
                let img = ci.transformed(by: CGAffineTransform(scaleX: s, y: s))
                let m = H * 0.035
                let x = new.logoCorner % 2 == 0 ? m : W - img.extent.width - m
                let y = new.logoCorner < 2 ? H - img.extent.height - m : m      // Core Image: y goes up
                logoImage = img.transformed(by: CGAffineTransform(translationX: x - img.extent.minX, y: y - img.extent.minY))
            }
        }
        if new.banner != old.banner || new.bannerColor != old.bannerColor || bannerImage == nil {
            bannerImage = new.banner.isEmpty ? nil : Self.bannerImage(new.banner, color: new.bannerColor, W: W, H: H)
        }
        if new.brbText != old.brbText || cardImage == nil {
            cardImage = Self.cardImage(new.brbText, W: W, H: H)
        }
        badgeSecond = -1
    }

    /// src = ReplayKit frame (nil = card only). seconds = time on air (nil = not live yet).
    func render(_ src: CVPixelBuffer?, orientation: UInt8, privacy: Bool, seconds: Int?) -> CVPixelBuffer? {
        lock.lock(); defer { lock.unlock() }
        var out: CVPixelBuffer?
        if let p = pool { CVPixelBufferPoolCreatePixelBuffer(nil, p, &out) }
        guard let o = out else { return nil }

        var frame: CIImage
        if privacy || src == nil {
            frame = cardImage ?? CIImage(color: .black).cropped(to: canvas)
        } else {
            frame = picture(src!, orientation: orientation)
        }
        if let b = bannerImage, !privacy { frame = b.composited(over: frame) }
        if let l = logoImage { frame = l.composited(over: frame) }
        if look.badge, let s = seconds {
            if s != badgeSecond { badgeImage = Self.badgeImage(s, W: CGFloat(width), H: CGFloat(height), logoCorner: look.logoCorner, hasLogo: logoImage != nil); badgeSecond = s }
            if let b = badgeImage { frame = b.composited(over: frame) }
        }
        ctx.render(frame.cropped(to: canvas), to: o, bounds: canvas, colorSpace: rgb)
        return o
    }

    private func picture(_ src: CVPixelBuffer, orientation: UInt8) -> CIImage {
        let W = CGFloat(width), H = CGFloat(height)
        var img = CIImage(cvPixelBuffer: src)
        if let o = CGImagePropertyOrientation(rawValue: UInt32(orientation)), o != .up { img = img.oriented(o) }
        img = img.transformed(by: CGAffineTransform(translationX: -img.extent.minX, y: -img.extent.minY))
        let e = img.extent
        let sFit = min(W / e.width, H / e.height)
        let sFill = max(W / e.width, H / e.height)

        func placed(_ s: CGFloat, in w: CGFloat, _ h: CGFloat) -> CIImage {
            let i = img.transformed(by: CGAffineTransform(scaleX: s, y: s))
            return i.transformed(by: CGAffineTransform(translationX: (w - i.extent.width) / 2 - i.extent.minX,
                                                       y: (h - i.extent.height) / 2 - i.extent.minY))
        }
        switch look.fit {
        case "fill":
            return placed(sFill, in: W, H).cropped(to: canvas)
        case "fit":
            return placed(sFit, in: W, H).composited(over: CIImage(color: .black).cropped(to: canvas))
        default:   // blur: blurred, zoomed copy behind the fitted picture (blurred small = cheap)
            let q: CGFloat = 0.2
            let small = placed(sFill * q, in: W * q, H * q)
            let bg = small.clampedToExtent()
                .applyingGaussianBlur(sigma: 6)
                .cropped(to: CGRect(x: 0, y: 0, width: W * q, height: H * q))
                .transformed(by: CGAffineTransform(scaleX: 1 / q, y: 1 / q))
                .applyingFilter("CIColorControls", parameters: [kCIInputBrightnessKey: -0.18, kCIInputSaturationKey: 1.15])
                .cropped(to: canvas)
            return placed(sFit, in: W, H).composited(over: bg.composited(over: CIImage(color: .black).cropped(to: canvas)))
        }
    }

    // MARK: overlay pictures (drawn with UIKit once, then reused)

    private static func ciImage(_ size: CGSize, _ draw: (CGContext) -> Void) -> CIImage? {
        let f = UIGraphicsImageRendererFormat()
        f.scale = 1
        f.opaque = false
        let ui = UIGraphicsImageRenderer(size: size, format: f).image { draw($0.cgContext) }
        return CIImage(image: ui)
    }

    static let bannerColors: [UIColor] = [
        UIColor(red: 0.92, green: 0.16, blue: 0.30, alpha: 0.92), UIColor(red: 0.15, green: 0.40, blue: 0.95, alpha: 0.92),
        UIColor(white: 0.05, alpha: 0.82), UIColor(red: 0.50, green: 0.25, blue: 0.95, alpha: 0.92)]

    private static func bannerImage(_ text: String, color: Int, W: CGFloat, H: CGFloat) -> CIImage? {
        let short = min(W, H)
        let font = UIFont.systemFont(ofSize: short * 0.055, weight: .heavy)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
        let textW = min((text as NSString).size(withAttributes: attrs).width, W * 0.86)
        let size = CGSize(width: textW + short * 0.08, height: short * 0.1)
        guard let img = ciImage(size, { _ in
            let r = CGRect(origin: .zero, size: size)
            bannerColors[min(max(color, 0), bannerColors.count - 1)].setFill()
            UIBezierPath(roundedRect: r, cornerRadius: size.height * 0.3).fill()
            (text as NSString).draw(with: CGRect(x: short * 0.04, y: (size.height - font.lineHeight) / 2, width: textW, height: font.lineHeight),
                                    options: [.truncatesLastVisibleLine, .usesLineFragmentOrigin], attributes: attrs, context: nil)
        }) else { return nil }
        return img.transformed(by: CGAffineTransform(translationX: (W - size.width) / 2, y: H * 0.05))
    }

    private static func badgeImage(_ s: Int, W: CGFloat, H: CGFloat, logoCorner: Int, hasLogo: Bool) -> CIImage? {
        let short = min(W, H)
        let text = "LIVE  " + (s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
                                         : String(format: "%02d:%02d", s / 60, s % 60))
        let font = UIFont.monospacedDigitSystemFont(ofSize: short * 0.04, weight: .heavy)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
        let tw = (text as NSString).size(withAttributes: attrs).width
        let size = CGSize(width: tw + short * 0.075, height: short * 0.066)
        guard let img = ciImage(size, { _ in
            UIColor(red: 1, green: 0.17, blue: 0.33, alpha: 0.95).setFill()
            UIBezierPath(roundedRect: CGRect(origin: .zero, size: size), cornerRadius: size.height / 2).fill()
            UIColor.white.setFill()
            let d = size.height * 0.28
            UIBezierPath(ovalIn: CGRect(x: short * 0.022, y: (size.height - d) / 2, width: d, height: d)).fill()
            (text as NSString).draw(at: CGPoint(x: short * 0.05, y: (size.height - font.lineHeight) / 2), withAttributes: attrs)
        }) else { return nil }
        let m = H * 0.035
        let left = !(hasLogo && logoCorner == 0)          // top-left unless the logo is there
        let x = left ? m : W - size.width - m
        return img.transformed(by: CGAffineTransform(translationX: x, y: H - size.height - m))
    }

    private static func cardImage(_ title: String, W: CGFloat, H: CGFloat) -> CIImage? {
        let size = CGSize(width: W, height: H)
        return ciImage(size) { c in
            let colors = [UIColor(red: 0.10, green: 0.05, blue: 0.22, alpha: 1).cgColor,
                          UIColor(red: 0.50, green: 0.08, blue: 0.24, alpha: 1).cgColor] as CFArray
            if let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1]) {
                c.drawLinearGradient(g, start: .zero, end: CGPoint(x: W, y: H), options: [])
            }
            let short = min(W, H)
            let center = NSMutableParagraphStyle()
            center.alignment = .center
            let t: [NSAttributedString.Key: Any] = [.font: UIFont.systemFont(ofSize: short * 0.11, weight: .heavy),
                                                    .foregroundColor: UIColor.white, .paragraphStyle: center]
            let sub: [NSAttributedString.Key: Any] = [.font: UIFont.systemFont(ofSize: short * 0.045, weight: .semibold),
                                                      .foregroundColor: UIColor(white: 1, alpha: 0.75), .paragraphStyle: center]
            let name = title.isEmpty ? "Be right back" : title
            (name as NSString).draw(in: CGRect(x: W * 0.05, y: H * 0.5 - short * 0.14, width: W * 0.9, height: short * 0.16), withAttributes: t)
            ("Stay tuned \u{2014} the stream is still on" as NSString)
                .draw(in: CGRect(x: 0, y: H * 0.5 + short * 0.05, width: W, height: short * 0.08), withAttributes: sub)
        }
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
    private let compositor: Compositor
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
    private var lastSource: CVPixelBuffer?          // last game frame (re-drawn while the screen is still)
    private var lastOrient: UInt8 = 1
    private var lastFrameTime = CFAbsoluteTimeGetCurrent()
    private var format: CMVideoFormatDescription?
    private var framesThisSecond = 0
    private var micSeen = false
    private var privacy = false             // live controls from the app
    private var micMuted = false
    private var idleTimer: DispatchSourceTimer?
    private let minFrameGap: Double

    init(config: LiveConfig) {
        cfg = config
        compositor = Compositor(width: config.width, height: config.height, look: StreamLook(config))
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
            guard let shaped = compose(px, orient: orient, pts: pts) else { return }
            lastPTS = pts
            lock.lock()
            lastSource = px
            lastOrient = orient
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

    /// One finished frame (privacy card or game picture + overlays).
    private func compose(_ px: CVPixelBuffer?, orient: UInt8, pts: CMTime) -> CMSampleBuffer? {
        lock.lock()
        let hide = privacy
        let seconds = startedAt.map { Int(Date().timeIntervalSince($0)) }
        lock.unlock()
        guard let out = compositor.render(px, orientation: orient, privacy: hide, seconds: seconds) else { return nil }
        lock.lock(); defer { lock.unlock() }
        lastFrameTime = CFAbsoluteTimeGetCurrent()
        return Compositor.sampleBuffer(out, pts: pts, format: &format)
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
            let src = self.lastSource, orient = self.lastOrient
            self.lock.unlock()
            guard idle else { return }
            // re-draw (so new look / privacy / timer show even on a still screen)
            let pts = CMClockGetTime(CMClockGetHostTimeClock())
            if let sb = self.compose(src, orient: orient, pts: pts) { feed.yield((sb, 0)) }
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
            self.compositor.update(StreamLook(c))
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
