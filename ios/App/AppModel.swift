// App state: chosen mode, Go Live settings (stream key in the Keychain),
// the settings server for the broadcast extension, live status, Wi-Fi info.

import Foundation
import Network
import Security
import SwiftUI
import UIKit

// MARK: - choices

enum MirrorMode: String, CaseIterable, Identifiable {
    case usb, wifi, live
    var id: String { rawValue }
}

enum Platform: String, CaseIterable, Identifiable {
    case facebook, youtube, tiktok, custom
    var id: String { rawValue }

    var name: String {
        switch self {
        case .facebook: return "Facebook"
        case .youtube: return "YouTube"
        case .tiktok: return "TikTok"
        case .custom: return "Custom"
        }
    }
    var symbol: String {
        switch self {
        case .facebook: return "f.circle.fill"
        case .youtube: return "play.rectangle.fill"
        case .tiktok: return "music.note"
        case .custom: return "server.rack"
        }
    }
    var color: Color {
        switch self {
        case .facebook: return Color(red: 0.09, green: 0.47, blue: 0.95)
        case .youtube: return Color(red: 1.0, green: 0.0, blue: 0.0)
        case .tiktok: return Color(red: 0.0, green: 0.95, blue: 0.92)
        case .custom: return Color.purple
        }
    }
    /// Default server (empty = the user types it)
    var server: String {
        switch self {
        case .facebook: return "rtmps://live-api-s.facebook.com:443/rtmp/"
        case .youtube: return "rtmps://a.rtmps.youtube.com:443/live2"
        case .tiktok, .custom: return ""
        }
    }
    var keyHelpURL: URL? {
        switch self {
        case .facebook: return URL(string: "https://www.facebook.com/live/producer")
        case .youtube: return URL(string: "https://studio.youtube.com")
        case .tiktok: return URL(string: "https://www.tiktok.com/live/studio")
        case .custom: return nil
        }
    }
    /// Page shown in the comments panel until you paste your own live link.
    var commentsDefault: String {
        switch self {
        case .facebook: return "https://www.facebook.com/live/producer"
        case .youtube: return "https://studio.youtube.com"
        case .tiktok: return "https://www.tiktok.com/live"
        case .custom: return ""
        }
    }
    /// These dashboards only work as the computer version of the website.
    var commentsDesktop: Bool { self == .facebook || self == .youtube }

    var keyHelp: String {
        switch self {
        case .facebook: return "facebook.com/live/producer \u{2192} Streaming software \u{2192} turn on \"Use a persistent stream key\" \u{2192} copy the key."
        case .youtube: return "YouTube Studio \u{2192} Create \u{2192} Go live \u{2192} Stream \u{2192} copy the stream key."
        case .tiktok: return "Only accounts with LIVE access get a server URL + stream key from TikTok. Paste both."
        case .custom: return "Any RTMP / RTMPS server: paste its server URL and stream key."
        }
    }
}

struct Quality: Identifiable, Equatable {
    let id: String
    let name: String
    let detail: String
    let width: Int, height: Int, fps: Int, bitrate: Int

    static let all: [Quality] = [
        Quality(id: "saver", name: "Data saver", detail: "480p \u{00b7} 30 fps", width: 854, height: 480, fps: 30, bitrate: 1_200_000),
        Quality(id: "hd", name: "HD", detail: "720p \u{00b7} 30 fps", width: 1280, height: 720, fps: 30, bitrate: 3_000_000),
        Quality(id: "hd60", name: "HD Smooth", detail: "720p \u{00b7} 60 fps", width: 1280, height: 720, fps: 60, bitrate: 4_500_000),
        Quality(id: "fhd", name: "Full HD", detail: "1080p \u{00b7} 30 fps", width: 1920, height: 1080, fps: 30, bitrate: 6_000_000),
    ]
    static func find(_ id: String) -> Quality { all.first { $0.id == id } ?? all[1] }
}

// MARK: - Keychain (stream key never stored in plain settings)

enum KeyStore {
    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "USBMirror.streamkey",
         kSecAttrAccount as String: account]
    }
    static func load(_ account: String) -> String {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return "" }
        return String(data: d, encoding: .utf8) ?? ""
    }
    static func save(_ account: String, _ value: String) {
        SecItemDelete(query(account) as CFDictionary)
        guard !value.isEmpty else { return }
        var q = query(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }
}

// MARK: - model

@MainActor
final class AppModel: ObservableObject {
    // mode the NEXT broadcast uses (set when you open a screen)
    @Published var armedMode: MirrorMode = .usb { didSet { defaults.set(armedMode.rawValue, forKey: "armedMode") } }

    // Go Live settings
    @Published var platform: Platform = .facebook {
        didSet {
            defaults.set(platform.rawValue, forKey: "platform")
            key = KeyStore.load(platform.rawValue)
            server = defaults.string(forKey: "server.\(platform.rawValue)") ?? platform.server
            commentsLink = defaults.string(forKey: "comments.\(platform.rawValue)") ?? ""
        }
    }
    /// Your live video / chat link (empty = the platform's dashboard).
    @Published var commentsLink: String = "" { didSet { defaults.set(commentsLink, forKey: "comments.\(platform.rawValue)") } }
    @Published var commentsOpen = false {
        didSet { UIApplication.shared.isIdleTimerDisabled = commentsOpen }   // screen stays on while reading
    }

    // stream look (Studio)
    @Published var fit = "blur" { didSet { defaults.set(fit, forKey: "look.fit") } }
    @Published var logoCorner = 1 { didSet { defaults.set(logoCorner, forKey: "look.logoCorner") } }
    @Published var logoScale = 0.16 { didSet { defaults.set(logoScale, forKey: "look.logoScale") } }
    @Published var banner = "" { didSet { defaults.set(banner, forKey: "look.banner") } }
    @Published var bannerColor = 0 { didSet { defaults.set(bannerColor, forKey: "look.bannerColor") } }
    @Published var badge = true { didSet { defaults.set(badge, forKey: "look.badge") } }
    @Published var brbText = "Be right back" { didSet { defaults.set(brbText, forKey: "look.brb") } }
    @Published private(set) var logoImage: UIImage?
    private var logoBase64 = ""
    private static var logoFile: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("stream-logo.png")
    }

    /// Picture from Photos -> small PNG (max 400 px) kept in the app's files.
    func setLogo(_ data: Data?) {
        guard let data = data, let img = UIImage(data: data) else {
            logoImage = nil; logoBase64 = ""
            try? FileManager.default.removeItem(at: Self.logoFile)
            return
        }
        let s = min(1, 400 / max(img.size.width, img.size.height))
        let size = CGSize(width: max(1, img.size.width * s), height: max(1, img.size.height * s))
        let f = UIGraphicsImageRendererFormat()
        f.scale = 1
        let small = UIGraphicsImageRenderer(size: size, format: f).image { _ in img.draw(in: CGRect(origin: .zero, size: size)) }
        guard let png = small.pngData() else { return }
        try? png.write(to: Self.logoFile)
        logoImage = small
        logoBase64 = png.base64EncodedString()
    }

    // live controls (sent to the broadcast every second while the app is open)
    @Published var privacy = false
    @Published var micMuted = false
    @Published var server: String = "" { didSet { defaults.set(server, forKey: "server.\(platform.rawValue)") } }
    @Published var key: String = "" { didSet { KeyStore.save(platform.rawValue, key) } }
    @Published var qualityID: String = "hd" { didSet { defaults.set(qualityID, forKey: "quality") } }
    @Published var portrait = false { didSet { defaults.set(portrait, forKey: "portrait") } }
    @Published var micVolume: Double = 1.0 { didSet { defaults.set(micVolume, forKey: "micVolume") } }
    @Published var gameVolume: Double = 1.0 { didSet { defaults.set(gameVolume, forKey: "gameVolume") } }
    /// The app records your voice itself (normal mode = the game is NOT turned down).
    @Published var appMic = true { didSet { defaults.set(appMic, forKey: "appMic") } }

    // live status from the extension
    @Published var live = LiveStatus()
    @Published var liveConnected = false

    // Wi-Fi
    @Published var addresses: [String] = []

    private let defaults = UserDefaults.standard
    private var configListener: NWListener?
    private var bonjour: NWListener?
    private var statusConn: NWConnection?
    private var statusBuffer = Data()
    private var timer: Timer?

    init() {
        armedMode = MirrorMode(rawValue: defaults.string(forKey: "armedMode") ?? "") ?? .usb
        let p = Platform(rawValue: defaults.string(forKey: "platform") ?? "") ?? .facebook
        platform = p
        server = defaults.string(forKey: "server.\(p.rawValue)") ?? p.server
        key = KeyStore.load(p.rawValue)
        commentsLink = defaults.string(forKey: "comments.\(p.rawValue)") ?? ""
        qualityID = defaults.string(forKey: "quality") ?? "hd"
        portrait = defaults.bool(forKey: "portrait")
        if defaults.object(forKey: "micVolume") != nil { micVolume = defaults.double(forKey: "micVolume") }
        if defaults.object(forKey: "appMic") != nil { appMic = defaults.bool(forKey: "appMic") }
        if defaults.object(forKey: "gameVolume") != nil { gameVolume = defaults.double(forKey: "gameVolume") }
        fit = defaults.string(forKey: "look.fit") ?? "blur"
        if defaults.object(forKey: "look.logoCorner") != nil { logoCorner = defaults.integer(forKey: "look.logoCorner") }
        if defaults.object(forKey: "look.logoScale") != nil { logoScale = defaults.double(forKey: "look.logoScale") }
        banner = defaults.string(forKey: "look.banner") ?? ""
        bannerColor = defaults.integer(forKey: "look.bannerColor")
        if defaults.object(forKey: "look.badge") != nil { badge = defaults.bool(forKey: "look.badge") }
        brbText = defaults.string(forKey: "look.brb") ?? "Be right back"
        if let png = try? Data(contentsOf: Self.logoFile), let img = UIImage(data: png) {
            logoImage = img
            logoBase64 = png.base64EncodedString()
        }
    }

    var quality: Quality { Quality.find(qualityID) }
    var liveReady: Bool { config(for: .live).publishURL != nil }

    func config(for mode: MirrorMode) -> LiveConfig {
        var c = LiveConfig()
        c.mode = mode == .live ? "live" : "mirror"
        c.platform = platform.rawValue
        c.server = server
        c.key = key
        let q = quality
        c.width = portrait ? q.height : q.width
        c.height = portrait ? q.width : q.height
        c.fps = q.fps
        c.bitrate = q.bitrate
        c.micVolume = micVolume
        c.gameVolume = gameVolume
        c.privacy = privacy
        c.micMuted = micMuted
        c.appMic = appMic
        c.fit = fit
        c.logo = logoBase64
        c.logoCorner = logoCorner
        c.logoScale = logoScale
        c.banner = banner.trimmingCharacters(in: .whitespacesAndNewlines)
        c.bannerColor = bannerColor
        c.badge = badge
        c.brbText = brbText
        return c
    }

    /// The page for the comments panel. A pasted YouTube video link becomes
    /// its clean live-chat page.
    var commentsPage: (url: URL, desktop: Bool)? {
        let link = commentsLink.trimmingCharacters(in: .whitespacesAndNewlines)
        if link.isEmpty {
            guard let u = URL(string: platform.commentsDefault) else { return nil }
            return (u, platform.commentsDesktop)
        }
        let full = link.lowercased().hasPrefix("http") ? link : "https://" + link
        guard let u = URL(string: full) else { return nil }
        if let id = Self.youTubeID(u) {
            return (URL(string: "https://www.youtube.com/live_chat?is_popout=1&v=\(id)")!, false)
        }
        let host = u.host?.lowercased() ?? ""
        let desktop = host.contains("studio.youtube") || u.path.contains("/live/producer")
        return (u, desktop)
    }

    private static func youTubeID(_ u: URL) -> String? {
        let host = u.host?.lowercased() ?? ""
        if host.contains("youtu.be") { return u.pathComponents.dropFirst().first }
        guard host.contains("youtube.com"), !host.contains("studio") else { return nil }
        if let v = URLComponents(url: u, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "v" })?.value {
            return v
        }
        let parts = u.pathComponents
        if let i = parts.firstIndex(where: { $0 == "live" || $0 == "shorts" }), i + 1 < parts.count { return parts[i + 1] }
        return nil
    }

    // MARK: start/stop background services

    func start() {
        startConfigServer()
        MicSender.shared.listen()          // your voice for Go Live (starts only when a live starts)
        startBonjour()
        refreshAddresses()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshAddresses()
                self?.connectStatusIfNeeded()
            }
        }
    }

    /// Serves the settings to the broadcast extension (loopback only).
    private func startConfigServer() {
        guard configListener == nil else { return }
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: NWEndpoint.Port(rawValue: LinkPorts.config)!)
        guard let l = try? NWListener(using: params) else { return }
        l.newConnectionHandler = { [weak self] conn in
            Task { @MainActor in
                guard let self = self else { conn.cancel(); return }
                let data = self.config(for: self.armedMode).encoded()
                conn.start(queue: .main)
                conn.send(content: data, isComplete: true, completion: .contentProcessed { _ in conn.cancel() })
            }
        }
        l.stateUpdateHandler = { [weak self] state in
            if case .failed = state {
                Task { @MainActor in
                    self?.configListener?.cancel(); self?.configListener = nil
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    self?.startConfigServer()
                }
            }
        }
        l.start(queue: .main)
        configListener = l
    }

    /// Advertising a Bonjour service makes iOS ask for Local Network access
    /// (needed once for Wi-Fi mirroring).
    private func startBonjour() {
        guard bonjour == nil, let l = try? NWListener(using: .tcp) else { return }
        l.service = NWListener.Service(name: "Den-Dev Live", type: "_usbmirror._tcp")
        l.newConnectionHandler = { $0.cancel() }
        l.start(queue: .main)
        bonjour = l
    }

    // MARK: live status (127.0.0.1:2347, only while a live runs)

    private func connectStatusIfNeeded() {
        guard statusConn == nil else { return }
        let c = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: LinkPorts.status)!, using: .tcp)
        statusConn = c
        statusBuffer = Data()
        c.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self = self, self.statusConn === c else { return }
                switch state {
                case .ready:
                    self.liveConnected = true
                    self.readStatus(c)
                case .failed, .cancelled, .waiting:
                    self.dropStatus(c)
                default: break
                }
            }
        }
        c.start(queue: .main)
    }

    private func dropStatus(_ c: NWConnection) {
        c.cancel()
        if statusConn === c { statusConn = nil }
        if liveConnected {
            liveConnected = false
            if live.state == "live" || live.state == "connecting" || live.state == "reconnecting" { live.state = "ended" }
            privacy = false
        }
    }

    private func readStatus(_ c: NWConnection) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, complete, error in
            Task { @MainActor in
                guard let self = self, self.statusConn === c else { return }
                if let data = data { self.statusBuffer.append(data); self.parsePackets() }
                if complete || error != nil { self.dropStatus(c) } else { self.readStatus(c) }
            }
        }
    }

    /// Same 14-byte header as the USB protocol: type, flags, ts(8), length(4).
    private func parsePackets() {
        while statusBuffer.count >= 14 {
            let b = [UInt8](statusBuffer.prefix(14))
            let len = Int(b[10]) << 24 | Int(b[11]) << 16 | Int(b[12]) << 8 | Int(b[13])
            guard statusBuffer.count >= 14 + len else { return }
            let payload = statusBuffer.subdata(in: statusBuffer.startIndex + 14 ..< statusBuffer.startIndex + 14 + len)
            statusBuffer.removeFirst(14 + len)
            if b[0] == 6, let s = try? JSONDecoder().decode(LiveStatus.self, from: payload) {
                live = s
            }
        }
    }

    // MARK: Wi-Fi address

    func refreshAddresses() {
        var found: [String] = []
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return }
        defer { freeifaddrs(ifaddr) }
        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let ifa = ptr.pointee
            guard let sa = ifa.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET) else { continue }
            let name = String(cString: ifa.ifa_name)
            guard name == "en0" || name.hasPrefix("bridge") else { continue }   // Wi-Fi / Hotspot
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count),
                           nil, 0, NI_NUMERICHOST) == 0 {
                found.append(String(cString: host))
            }
        }
        if found != addresses { addresses = found }
    }
}

/// Small helpers
enum Haptics {
    static func tap() { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
}

func formatDuration(_ s: Int) -> String {
    let h = s / 3600, m = s / 60 % 60, sec = s % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%02d:%02d", m, sec)
}
