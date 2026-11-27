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

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        server.onClientConnected = { [weak self] in
            guard let self = self else { return }
            self.stateLock.lock(); self.orientationDirty = true; self.stateLock.unlock()
            self.encoder?.requestKeyframe()
        }
        server.onTick = { [weak self] in
            guard let self = self, self.server.isClientConnected else { return }
            self.encoder?.repeatLastFrameIfIdle()
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

    // MARK: audio

    private func handleAudio(_ sb: CMSampleBuffer) {
        guard server.isClientConnected, let pcm = audio.convert(sb) else { return }
        let ts = microseconds(CMSampleBufferGetPresentationTimeStamp(sb))
        server.send(Packet.make(.audioPCM, timestampUs: ts, payload: pcm), isVideo: false)
    }
}
