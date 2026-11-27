// Look & feel: dark glass cards on a deep gradient with soft color glows.

import ReplayKit
import SwiftUI
import UIKit

enum Theme {
    static let bgTop = Color(red: 0.05, green: 0.06, blue: 0.12)
    static let bgBottom = Color(red: 0.01, green: 0.01, blue: 0.03)
    static let card = Color.white.opacity(0.06)
    static let cardStroke = Color.white.opacity(0.10)
    static let text = Color.white
    static let dim = Color.white.opacity(0.62)
    static let faint = Color.white.opacity(0.38)

    static let usb = [Color(red: 0.20, green: 0.48, blue: 1.0), Color(red: 0.45, green: 0.30, blue: 1.0)]
    static let wifi = [Color(red: 0.0, green: 0.78, blue: 0.70), Color(red: 0.10, green: 0.55, blue: 0.95)]
    static let live = [Color(red: 1.0, green: 0.22, blue: 0.36), Color(red: 1.0, green: 0.48, blue: 0.20)]

    static func colors(_ m: MirrorMode) -> [Color] {
        switch m {
        case .usb: return usb
        case .wifi: return wifi
        case .live: return live
        }
    }
}

/// Deep background with two blurred color glows.
struct GlowBackground: View {
    var colors: [Color] = Theme.usb

    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.bgTop, Theme.bgBottom], startPoint: .top, endPoint: .bottom)
            Circle().fill(colors[0].opacity(0.35)).frame(width: 360).blur(radius: 120).offset(x: -140, y: -300)
            Circle().fill(colors[1].opacity(0.25)).frame(width: 320).blur(radius: 120).offset(x: 160, y: 320)
        }
        .ignoresSafeArea()
    }
}

/// Rounded glass card.
struct Card<Content: View>: View {
    var padding: CGFloat = 18
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(Theme.card))
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(Theme.cardStroke, lineWidth: 1))
    }
}

/// Small colored capsule label.
struct Badge: View {
    let text: String
    var color: Color = .white

    var body: some View {
        Text(text)
            .font(.caption2.weight(.bold))
            .textCase(.uppercase)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(color.opacity(0.18)))
            .foregroundColor(color)
    }
}

/// Numbered step row.
struct StepRow: View {
    let number: Int
    let text: String
    var color: Color = .white

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(number)")
                .font(.footnote.weight(.bold))
                .frame(width: 24, height: 24)
                .background(Circle().fill(color.opacity(0.22)))
                .foregroundColor(color)
            Text(text)
                .font(.subheadline)
                .foregroundColor(Theme.text)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - the system broadcast button, dressed up

/// RPSystemBroadcastPickerView is the ONLY way to start a broadcast. We make
/// its button invisible and stretch it over our own design, so a tap anywhere
/// on our big button opens the iOS "Start Broadcast" sheet.
struct BroadcastButton: View {
    let title: String
    let subtitle: String
    let symbol: String
    let colors: [Color]

    var body: some View {
        ZStack {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 26, weight: .bold))
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline.weight(.heavy))
                    Text(subtitle).font(.caption).opacity(0.85)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.headline.weight(.bold)).opacity(0.8)
            }
            .foregroundColor(.white)
            .padding(.horizontal, 20)
            .frame(height: 74)
            .background(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(LinearGradient(colors: colors, startPoint: .leading, endPoint: .trailing))
            )
            .shadow(color: colors[0].opacity(0.45), radius: 18, y: 8)

            PickerOverlay()
        }
        .frame(height: 74)
    }
}

private struct PickerOverlay: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.preferredExtension = Self.findExtensionBundleID()
        picker.showsMicrophoneButton = true
        picker.backgroundColor = .clear
        return picker
    }

    func updateUIView(_ picker: RPSystemBroadcastPickerView, context: Context) {
        // invisible button that covers the whole area
        DispatchQueue.main.async {
            for case let button as UIButton in picker.subviews {
                button.setImage(UIImage(), for: .normal)
                button.frame = picker.bounds
                button.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            }
        }
    }

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

/// Pulsing red "LIVE" dot.
struct PulsingDot: View {
    var color: Color = .red
    @State private var on = false

    var body: some View {
        ZStack {
            Circle().fill(color.opacity(0.35)).frame(width: 22, height: 22).scaleEffect(on ? 1.2 : 0.6).opacity(on ? 0 : 1)
            Circle().fill(color).frame(width: 10, height: 10)
        }
        .onAppear {
            withAnimation(.easeOut(duration: 1.2).repeatForever(autoreverses: false)) { on = true }
        }
    }
}
