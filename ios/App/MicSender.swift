// "Den-Dev Live mic": the app records your voice itself and hands it to the
// broadcast extension (127.0.0.1:2398).
//
// Why: the Microphone switch in the iOS Start Broadcast sheet records in
// "call" mode, and iOS then turns the game sound down. Recording here in
// normal mode, mixed with other apps, keeps Mobile Legends at full volume.
//
// The extension connects when a live starts -> recording starts; it
// disconnects when the live ends -> recording stops (orange dot goes away).
// Format sent: 48 kHz, mono, 16-bit little-endian PCM.

import AVFoundation
import Foundation
import Network

final class MicSender: @unchecked Sendable {
    static let shared = MicSender()

    private let queue = DispatchQueue(label: "mic.sender")
    private let engine = AVAudioEngine()
    private var listener: NWListener?
    private var conns: [NWConnection] = []
    private var converter: AVAudioConverter?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 48_000, channels: 1, interleaved: true)!
    private var running = false
    private let lock = NSLock()

    private init() {
        NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { [weak self] n in
            // e.g. a phone call or another app took the mic: restart afterwards
            guard let self = self,
                  let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            self.queue.asyncAfter(deadline: .now() + 0.5) { if self.hasClients { self.startRecording() } }
        }
    }

    private var hasClients: Bool { lock.lock(); defer { lock.unlock() }; return !conns.isEmpty }

    /// Listen for the broadcast extension (loopback only).
    func listen() {
        queue.async {
            guard self.listener == nil else { return }
            let params = NWParameters.tcp
            params.allowLocalEndpointReuse = true
            params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: NWEndpoint.Port(rawValue: LinkPorts.mic)!)
            guard let l = try? NWListener(using: params) else { return }
            l.newConnectionHandler = { [weak self] c in self?.accept(c) }
            l.stateUpdateHandler = { [weak self] st in
                if case .failed = st {
                    self?.queue.async { self?.listener?.cancel(); self?.listener = nil }
                    self?.queue.asyncAfter(deadline: .now() + 1) { self?.listen() }
                }
            }
            l.start(queue: self.queue)
            self.listener = l
        }
    }

    private func accept(_ c: NWConnection) {
        c.stateUpdateHandler = { [weak self, weak c] st in
            guard let self = self, let c = c else { return }
            switch st {
            case .failed, .cancelled: self.drop(c)
            default: break
            }
        }
        c.start(queue: queue)
        // notice when the extension goes away
        c.receive(minimumIncompleteLength: 1, maximumLength: 16) { [weak self, weak c] _, _, done, err in
            if done || err != nil, let c = c { self?.drop(c) }
        }
        lock.lock(); conns.append(c); lock.unlock()
        NSLog("[DenDev] mic: broadcast connected")
        startRecording()
    }

    private func drop(_ c: NWConnection) {
        lock.lock()
        conns.removeAll { $0 === c }
        let none = conns.isEmpty
        lock.unlock()
        c.cancel()
        if none { stopRecording() }
    }

    private func startRecording() {
        queue.async {
            guard !self.running else { return }
            do {
                let s = AVAudioSession.sharedInstance()
                // normal recording mode (NOT voice chat) + mix with others = the game is not turned down
                try s.setCategory(.playAndRecord, mode: .default,
                                  options: [.mixWithOthers, .defaultToSpeaker, .allowBluetoothA2DP, .allowBluetooth])
                try s.setActive(true)
                let input = self.engine.inputNode
                let inFmt = input.outputFormat(forBus: 0)
                guard inFmt.sampleRate > 0 else { throw NSError(domain: "mic", code: 1) }
                self.converter = AVAudioConverter(from: inFmt, to: self.outFormat)
                input.removeTap(onBus: 0)
                input.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { [weak self] buf, _ in self?.send(buf) }
                self.engine.prepare()
                try self.engine.start()
                self.running = true
                NSLog("[DenDev] mic: recording (\(inFmt.sampleRate) Hz)")
            } catch {
                NSLog("[DenDev] mic: could not start: \(error)")
            }
        }
    }

    private func stopRecording() {
        queue.async {
            guard self.running else { return }
            self.engine.inputNode.removeTap(onBus: 0)
            self.engine.stop()
            self.running = false
            // back to quiet playback mode (keeps the comments PiP working), never ducks the game
            try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: [.mixWithOthers])
            NSLog("[DenDev] mic: stopped")
        }
    }

    private func send(_ buf: AVAudioPCMBuffer) {
        guard let conv = converter else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * 48_000 / buf.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: cap) else { return }
        var given = false
        var err: NSError?
        _ = conv.convert(to: out, error: &err) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buf
        }
        guard out.frameLength > 0, let p = out.int16ChannelData else { return }
        let data = Data(bytes: p[0], count: Int(out.frameLength) * 2)
        lock.lock(); let targets = conns; lock.unlock()
        for c in targets { c.send(content: data, completion: .idempotent) }
    }
}
