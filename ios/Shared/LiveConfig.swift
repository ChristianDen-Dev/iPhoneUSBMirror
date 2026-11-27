// Settings shared by the app and the broadcast extension.
//
// How they get from the app to the extension (no App Group needed, so it
// works with a free Apple ID + Sideloadly):
//   - the app serves its current settings as JSON on 127.0.0.1:2399 while it
//     is open (you start the broadcast from inside the app);
//   - the extension asks for them when the broadcast starts and keeps a copy,
//     so a later start from Control Center uses the last settings.
// Live status goes the other way: the extension sends JSON status packets on
// 127.0.0.1:2347, the app shows them.

import Foundation
import Network

enum LinkPorts {
    static let config: UInt16 = 2399     // app -> extension: settings
    static let status: UInt16 = 2347     // extension -> app: live status
}

/// What the broadcast should do.
struct LiveConfig: Codable, Equatable {
    /// "mirror" = send to the laptop (USB or Wi-Fi), "live" = the phone streams by itself.
    var mode: String = "mirror"
    var platform: String = "facebook"
    var server: String = ""
    var key: String = ""
    var width: Int = 1280
    var height: Int = 720
    var fps: Int = 30
    var bitrate: Int = 3_500_000          // video, bits per second (audio adds 128 kbps)
    var micVolume: Double = 1.0
    var gameVolume: Double = 1.0
    // live controls (the extension asks for these again every second)
    var privacy: Bool = false             // viewers see "Be right back" instead of the screen
    var micMuted: Bool = false            // your voice is not sent

    var isLive: Bool { mode == "live" }

    /// rtmp(s)://server/app + "/" + key
    var publishURL: URL? {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        let k = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty, !k.isEmpty else { return nil }
        while s.hasSuffix("/") { s.removeLast() }
        return URL(string: s + "/" + k)
    }

    func encoded() -> Data { (try? JSONEncoder().encode(self)) ?? Data() }
    static func decode(_ data: Data) -> LiveConfig? { try? JSONDecoder().decode(LiveConfig.self, from: data) }
}

/// Live status sent by the extension once per second.
struct LiveStatus: Codable, Equatable {
    var state: String = "idle"            // connecting / live / reconnecting / error / ended
    var seconds: Int = 0                  // time on air
    var kbps: Int = 0                     // real upload speed
    var targetKbps: Int = 0               // encoder bitrate now (adapts to the network)
    var fps: Int = 0
    var width: Int = 0
    var height: Int = 0
    var reconnects: Int = 0
    var mic: Bool = false
    var micMuted: Bool = false
    var privacy: Bool = false
    var message: String = ""
}
