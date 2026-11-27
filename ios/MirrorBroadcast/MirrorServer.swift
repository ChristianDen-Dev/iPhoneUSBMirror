// TCP server on the iPhone (PeerTalk-style). The Windows receiver reaches it
// through usbmux over the USB cable; usbmuxd on the phone connects to
// 127.0.0.1:<port>, so we listen on loopback only (no Wi-Fi exposure,
// no Local Network permission prompt).
//
// - One client at a time. A new client replaces the old one (makes
//   reconnects after an unplug instant).
// - Tracks bytes waiting to be sent so the caller can DROP video frames
//   instead of queueing them (memory limit is ~50 MB).

import Foundation
import Network

final class MirrorServer {
    /// Called (on the network queue) when a new receiver connects.
    var onClientConnected: (() -> Void)?
    /// Called once per second (on the network queue).
    var onTick: (() -> Void)?

    private let port: NWEndpoint.Port
    private let queue = DispatchQueue(label: "mirror.net")
    private var listener: NWListener?
    private var connection: NWConnection?
    private var timer: DispatchSourceTimer?
    private var stopped = true
    private var bindLoopbackOnly = true

    // Shared with other threads -> guarded by lock.
    private let lock = NSLock()
    private var _connected = false
    private var _pendingBytes = 0

    /// Video is skipped when more than this is still waiting to be sent.
    private let videoBacklogLimit = 300_000
    /// Audio (tiny) is only dropped when things are badly stuck.
    private let audioBacklogLimit = 1_000_000

    init(port: UInt16) {
        self.port = NWEndpoint.Port(rawValue: port)!
    }

    var isClientConnected: Bool {
        lock.lock(); defer { lock.unlock() }
        return _connected
    }

    /// True when a client is connected and the send backlog is small.
    var canSendVideo: Bool {
        lock.lock(); defer { lock.unlock() }
        return _connected && _pendingBytes < videoBacklogLimit
    }

    // MARK: start / stop

    func start() {
        queue.async {
            self.stopped = false
            self.startListener()
            self.startTimer()
        }
    }

    func stop() {
        queue.sync {
            self.stopped = true
            self.timer?.cancel(); self.timer = nil
            self.connection?.cancel(); self.connection = nil
            self.listener?.cancel(); self.listener = nil
            self.setConnected(false)
        }
    }

    private func startListener() {
        guard !stopped else { return }
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true                       // low latency: no Nagle
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true

        do {
            let l: NWListener
            if bindLoopbackOnly {
                params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: port)
                l = try NWListener(using: params)
            } else {
                l = try NWListener(using: params, on: port)
            }
            l.stateUpdateHandler = { [weak self] state in
                guard let self = self else { return }
                switch state {
                case .ready:
                    NSLog("[Mirror] listening on port \(self.port) (loopbackOnly=\(self.bindLoopbackOnly))")
                case .failed(let err):
                    NSLog("[Mirror] listener failed: \(err)")
                    l.cancel()
                    if self.listener === l { self.listener = nil }
                    // If loopback binding is refused, fall back to any address.
                    self.bindLoopbackOnly.toggle()
                    self.queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
                default:
                    break
                }
            }
            l.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
            l.start(queue: queue)
            listener = l
        } catch {
            NSLog("[Mirror] cannot create listener: \(error)")
            bindLoopbackOnly.toggle()
            queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
        }
    }

    private func startTimer() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self = self, self.connection != nil else { return }
            self.sendNow(Packet.make(.heartbeat, timestampUs: 0, payload: Data()))
            self.onTick?()
        }
        t.resume()
        timer = t
    }

    // MARK: connection handling (network queue)

    private func accept(_ conn: NWConnection) {
        // Newest client wins.
        connection?.cancel()
        lock.lock(); _pendingBytes = 0; lock.unlock()
        connection = conn

        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            switch state {
            case .ready:
                NSLog("[Mirror] client connected")
                guard self.connection === conn else { return }
                self.setConnected(true)
                self.onClientConnected?()
            case .failed, .cancelled:
                if self.connection === conn {
                    NSLog("[Mirror] client gone")
                    self.connection = nil
                    self.setConnected(false)
                }
                if case .failed = state { conn.cancel() }
            default:
                break
            }
        }
        conn.start(queue: queue)
        receiveLoop(conn)
    }

    /// We don't expect data from the receiver; reading just detects closes.
    private func receiveLoop(_ conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self, weak conn] _, _, isComplete, error in
            guard let self = self, let conn = conn else { return }
            if isComplete || error != nil {
                conn.cancel()
                return
            }
            self.receiveLoop(conn)
        }
    }

    private func setConnected(_ v: Bool) {
        lock.lock(); _connected = v; if !v { _pendingBytes = 0 }; lock.unlock()
    }

    // MARK: sending

    /// Thread-safe. `isVideo` packets were already gated by `canSendVideo`.
    func send(_ packet: Data, isVideo: Bool) {
        if !isVideo {
            lock.lock()
            let tooMuch = !_connected || _pendingBytes > audioBacklogLimit
            lock.unlock()
            if tooMuch { return }
        }
        queue.async { self.sendNow(packet) }
    }

    private func sendNow(_ packet: Data) {
        guard let conn = connection else { return }
        let n = packet.count
        lock.lock(); _pendingBytes += n; lock.unlock()
        conn.send(content: packet, completion: .contentProcessed { [weak self] error in
            guard let self = self else { return }
            self.lock.lock(); self._pendingBytes = max(0, self._pendingBytes - n); self.lock.unlock()
            if let error = error {
                NSLog("[Mirror] send error: \(error)")
                conn.cancel()
            }
        })
    }
}
