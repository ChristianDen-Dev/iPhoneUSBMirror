// Broadcast Upload Extension entry point. ReplayKit calls
// processSampleBuffer for every screen frame and audio buffer while the
// broadcast runs. Two modes (chosen in the app, see Shared/LiveConfig.swift):
//   mirror : encode + send to the Windows receiver (USB or Wi-Fi)
//   live   : the phone streams by itself to Facebook / YouTube (LiveStreamer)

import ReplayKit
import CoreMedia
import CoreVideo
import QuartzCore

class SampleHandler: RPBroadcastSampleHandler {
    static let port: UInt16 = 2345
    /// Sound-only channel: its own TCP connection, so sound never waits behind
    /// big video frames (lower, steadier audio delay). Older receivers don't
    /// connect here and keep getting sound on the main port.
    static let audioPort: UInt16 = 2346

    private let server = MirrorServer(port: SampleHandler.port)
    private let audioServer = MirrorServer(port: SampleHandler.audioPort)
    private let audio = AppAudioConverter()
    private let micAudio = AppAudioConverter()   // separate converter: mic has its own format
    private var encoder: VideoEncoder?

    private let stateLock = NSLock()
    private var lastOrientation: UInt8 = 0
    private var orientationDirty = true   // resend orientation (e.g. new client)
    private var sourceDescription = "none" // last ReplayKit buffer size/format (diagnostics)
    private var encoderDescription = "none"
    private var tickCount = 0

    // which mode this broadcast runs in ("pending" until the settings arrive)
    private let modeLock = NSLock()
    private var mode = "pending"
    private var live: LiveStreamer?

    // Wi-Fi: adaptive bitrate. Frames dropped because the network is behind
    // -> step down; several clean seconds -> step up again.
    private static let wifiLevels = [12_000_000, 8_000_000, 5_000_000, 3_000_000]
    private var wifiLevel = 0
    private var cleanSeconds = 0
    private var droppedFrames = 0          // this second (guarded by stateLock)
    private var droppedTotal = 0
    private var targetBitrate: Int {
        server.isWiFiClient ? Self.wifiLevels[wifiLevel] : VideoEncoder.bitrate
    }

    // audio delivery diagnostics (how evenly ReplayKit hands us sound)
    private let audioStatsLock = NSLock()
    private var lastAudioArrival: CFTimeInterval = 0
    private var audioMaxGapMs: Double = 0
    private var audioPieces = 0
    private var micPieces = 0

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        ConfigFetcher.fetch { [weak self] cfg in
            guard let self = self else { return }
            if cfg.isLive {
                self.startLive(cfg)
            } else {
                self.startMirror()
            }
        }
    }

    private func setMode(_ m: String) {
        modeLock.lock(); mode = m; modeLock.unlock()
    }

    private var currentMode: String {
        modeLock.lock(); defer { modeLock.unlock() }
        return mode
    }

    private func startLive(_ cfg: LiveConfig) {
        let l = LiveStreamer(config: cfg)
        l.onFatal = { [weak self] message in
            let err = NSError(domain: "USBMirror", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
            self?.finishBroadcastWithError(err)
        }
        live = l
        setMode("live")
        l.start()
    }

    private func startMirror() {
        server.onClientConnected = { [weak self] in
            guard let self = self else { return }
            self.stateLock.lock(); self.orientationDirty = true; self.stateLock.unlock()
            self.wifiLevel = 1                      // start Wi-Fi at 8 Mbps, climb if clean
            self.cleanSeconds = 0
            self.encoder?.setBitrate(self.targetBitrate)
            self.encoder?.requestKeyframe()
        }
        server.onTick = { [weak self] in
            guard let self = self, self.server.isClientConnected else { return }
            self.encoder?.repeatLastFrameIfIdle()
            self.adaptBitrate()
            self.tickCount += 1
            if self.tickCount % 5 == 1 { self.sendInfo() }   // every ~5 s
        }
        server.start()
        audioServer.start()
        setMode("mirror")
    }

    override func broadcastPaused() {}
    override func broadcastResumed() { encoder?.requestKeyframe() }

    override func broadcastFinished() {
        live?.stop()
        live = nil
        server.stop()
        audioServer.stop()
        encoder?.invalidate()
        encoder = nil
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        switch currentMode {
        case "live":
            live?.process(sampleBuffer, type: sampleBufferType)
            return
        case "mirror":
            break
        default:
            return                     // settings not here yet (first ~0.1 s)
        }
        switch sampleBufferType {
        case .video:
            handleVideo(sampleBuffer)
        case .audioApp:
            handleAudio(sampleBuffer)
        case .audioMic:
            handleMic(sampleBuffer)
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
            encoder?.setBitrate(targetBitrate)
            stateLock.lock()
            sourceDescription = "\(w)x\(h) \(fourCC(CVPixelBufferGetPixelFormatType(pixelBuffer)))"
            encoderDescription = encoder.map { "\($0.width)x\($0.height)" } ?? "FAILED"
            stateLock.unlock()
            sendInfo()
        }
        guard let enc = encoder else { return }

        // Busy encoder or slow socket -> DROP this frame (never queue).
        guard enc.isReady else { return }
        guard server.canSendVideo else {
            stateLock.lock(); droppedFrames += 1; droppedTotal += 1; stateLock.unlock()
            return
        }
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

    /// Once per second (network queue).
    private func adaptBitrate() {
        stateLock.lock(); let dropped = droppedFrames; droppedFrames = 0; stateLock.unlock()
        guard server.isWiFiClient else { return }
        if dropped >= 3 {
            cleanSeconds = 0
            if wifiLevel < Self.wifiLevels.count - 1 { wifiLevel += 1 }
        } else {
            cleanSeconds += 1
            if cleanSeconds >= 6 && wifiLevel > 0 { wifiLevel -= 1; cleanSeconds = 0 }
        }
        encoder?.setBitrate(targetBitrate)
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
        audioStatsLock.lock()
        let pieces = audioPieces, maxGap = audioMaxGapMs, mics = micPieces
        audioPieces = 0; audioMaxGapMs = 0; micPieces = 0
        audioStatsLock.unlock()
        let text = String(format: "source %@ -> encode %@ | orient %d | mem %.1f MB | thermal %@ | lowPower %@"
                          + " | audio in %d pieces, max gap %.0f ms, channel %@ | mic %@ | link %@"
                          + " | bitrate %d Mbps, dropped %d",
                          src, enc, Int(orient), memoryFootprintMB(), thermal,
                          ProcessInfo.processInfo.isLowPowerModeEnabled ? "on" : "off",
                          pieces, maxGap, audioServer.isClientConnected ? "separate" : "main",
                          mics > 0 ? "on" : "off", server.isWiFiClient ? "wifi" : "usb",
                          (encoder?.currentBitrate ?? 0) / 1_000_000, droppedTotalSnapshot())
        server.send(Packet.make(.info, timestampUs: 0, payload: Data(text.utf8)), isVideo: false)
    }

    private func droppedTotalSnapshot() -> Int {
        stateLock.lock(); defer { stateLock.unlock() }
        let n = droppedTotal; droppedTotal = 0
        return n
    }

    // MARK: audio

    private func handleAudio(_ sb: CMSampleBuffer) {
        let now = CACurrentMediaTime()
        audioStatsLock.lock()
        if lastAudioArrival > 0 { audioMaxGapMs = max(audioMaxGapMs, (now - lastAudioArrival) * 1000) }
        lastAudioArrival = now
        audioPieces += 1
        audioStatsLock.unlock()

        // Prefer the sound-only channel; fall back to the main one.
        let target = audioServer.isClientConnected ? audioServer : server
        guard target.isClientConnected, let pcm = audio.convert(sb) else { return }
        let ts = microseconds(CMSampleBufferGetPresentationTimeStamp(sb))
        target.send(Packet.make(.audioPCM, timestampUs: ts, payload: pcm), isVideo: false)
    }

    /// iPhone microphone (only arrives when the Microphone toggle was ON when
    /// the broadcast started). Sent as its own packet type so the receiver
    /// decides where it goes (stream only, never the laptop speakers).
    private func handleMic(_ sb: CMSampleBuffer) {
        audioStatsLock.lock(); micPieces += 1; audioStatsLock.unlock()
        let target = audioServer.isClientConnected ? audioServer : server
        guard target.isClientConnected, let pcm = micAudio.convert(sb) else { return }
        let ts = microseconds(CMSampleBufferGetPresentationTimeStamp(sb))
        target.send(Packet.make(.audioMic, timestampUs: ts, payload: pcm), isVideo: false)
    }
}
