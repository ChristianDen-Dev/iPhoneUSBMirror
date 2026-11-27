// Host app: one screen with the system "Start Broadcast" button.
// All the real work happens in the MirrorBroadcast extension.

import SwiftUI
import ReplayKit
import Network

@main
struct MirrorApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @StateObject private var net = WiFiInfo()

    var body: some View {
        VStack(spacing: 24) {
            Text("iPhone USB Mirror")
                .font(.largeTitle.bold())

            BroadcastPicker()
                .frame(width: 140, height: 140)
                .background(Circle().fill(Color.red.opacity(0.15)))

            VStack(alignment: .leading, spacing: 10) {
                Text("1. USB: plug the iPhone into the laptop. Wi-Fi: same Wi-Fi as the laptop (or the laptop on this iPhone's Hotspot).")
                Text("2. Start the receiver on Windows.")
                Text("3. Tap the button above → choose **iPhone USB Mirror** → **Start Broadcast**.")
                Text("4. Open your game. To stop, tap the red status pill / Control Center.")
            }
            .font(.body)
            .padding(.horizontal, 24)

            VStack(spacing: 4) {
                Text("Wi-Fi address of this iPhone").font(.footnote).foregroundColor(.secondary)
                Text(net.addresses.isEmpty ? "not on Wi-Fi" : net.addresses.joined(separator: "   "))
                    .font(.title3.monospacedDigit().bold())
                Text("The receiver finds it by itself; type it only if it asks.")
                    .font(.footnote).foregroundColor(.secondary)
            }

            Text("Mic: turn on Microphone in the broadcast sheet only if you want the iPhone mic.")
                .font(.footnote)
                .foregroundColor(.secondary)
        }
        .padding()
        .onAppear { net.start() }
    }
}

/// Shows the iPhone's Wi-Fi / Hotspot IPv4 address and triggers the
/// "Local Network" permission prompt once (needed for Wi-Fi mirroring:
/// iOS only lets the broadcast accept Wi-Fi connections after this).
final class WiFiInfo: ObservableObject {
    @Published var addresses: [String] = []
    private var listener: NWListener?
    private var timer: Timer?

    func start() {
        refresh()
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refresh() }
        }
        // Advertising a Bonjour service makes iOS ask for Local Network access.
        if listener == nil, let l = try? NWListener(using: .tcp) {
            l.service = NWListener.Service(name: "iPhone USB Mirror", type: "_usbmirror._tcp")
            l.newConnectionHandler = { $0.cancel() }
            l.stateUpdateHandler = { _ in }
            l.start(queue: .main)
            listener = l
        }
    }

    private func refresh() {
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

/// Wraps RPSystemBroadcastPickerView (the only way to start a broadcast).
struct BroadcastPicker: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 140, height: 140))
        picker.preferredExtension = Self.findExtensionBundleID()
        picker.showsMicrophoneButton = true    // lets you send the iPhone mic (optional)

        // Make the tiny default button big.
        if let button = picker.subviews.compactMap({ $0 as? UIButton }).first {
            let config = UIImage.SymbolConfiguration(pointSize: 90, weight: .regular)
            button.setImage(UIImage(systemName: "record.circle", withConfiguration: config), for: .normal)
            button.tintColor = .systemRed
        }
        return picker
    }

    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}

    /// Sideloadly may rename bundle IDs when it re-signs, so we look the
    /// extension up at runtime instead of hard-coding its ID.
    static func findExtensionBundleID() -> String? {
        guard let plugIns = Bundle.main.builtInPlugInsURL,
              let items = try? FileManager.default.contentsOfDirectory(
                at: plugIns, includingPropertiesForKeys: nil) else { return nil }
        for url in items where url.pathExtension == "appex" {
            guard let bundle = Bundle(url: url),
                  let ext = bundle.infoDictionary?["NSExtension"] as? [String: Any],
                  ext["NSExtensionPointIdentifier"] as? String == "com.apple.broadcast-services-upload"
            else { continue }
            return bundle.bundleIdentifier
        }
        return nil
    }
}
