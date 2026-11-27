// Host app: one screen with the system "Start Broadcast" button.
// All the real work happens in the MirrorBroadcast extension.

import SwiftUI
import ReplayKit

@main
struct MirrorApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    var body: some View {
        VStack(spacing: 28) {
            Text("iPhone USB Mirror")
                .font(.largeTitle.bold())

            BroadcastPicker()
                .frame(width: 140, height: 140)
                .background(Circle().fill(Color.red.opacity(0.15)))

            VStack(alignment: .leading, spacing: 10) {
                Text("1. Plug the iPhone into the laptop (USB).")
                Text("2. Start the receiver on Windows.")
                Text("3. Tap the button above → choose **iPhone USB Mirror** → **Start Broadcast**.")
                Text("4. Open your game. To stop, tap the red status pill / Control Center.")
            }
            .font(.body)
            .padding(.horizontal, 24)

            Text("Mic is not captured — only game sound.")
                .font(.footnote)
                .foregroundColor(.secondary)
        }
        .padding()
    }
}

/// Wraps RPSystemBroadcastPickerView (the only way to start a broadcast).
struct BroadcastPicker: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 140, height: 140))
        picker.preferredExtension = Self.findExtensionBundleID()
        picker.showsMicrophoneButton = false

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
