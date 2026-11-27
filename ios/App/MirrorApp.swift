// Host app. Home screen with three ways to use the broadcast:
//   USB      - mirror to the Windows laptop with the cable (lowest delay)
//   Wi-Fi    - mirror to the laptop without a cable
//   Go Live  - the phone streams by itself to Facebook / YouTube / TikTok
// The real work happens in the MirrorBroadcast extension.

import SwiftUI

@main
struct MirrorApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ZStack {
                HomeView()
                // TikTok-style floating comments (Go Live only)
                if model.armedMode == .live || model.liveConnected {
                    FloatingComments().transition(.opacity)
                }
            }
                .environmentObject(model)
                .preferredColorScheme(.dark)
                .onAppear { model.start() }
        }
    }
}

struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @State private var path: [MirrorMode] = []

    var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                GlowBackground(colors: Theme.colors(model.armedMode))
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        header
                        if model.liveConnected && model.live.state != "ended" {
                            LiveMiniBar(status: model.live).onTapGesture { path.append(.live) }
                        }
                        ModeCard(mode: .usb, title: "USB", subtitle: "Mirror to your laptop with the cable",
                                 symbol: "cable.connector", badge: "Lowest delay", armed: model.armedMode == .usb) { path.append(.usb) }
                        ModeCard(mode: .wifi, title: "Wi-Fi", subtitle: "Mirror to your laptop, no cable",
                                 symbol: "wifi", badge: "Wireless", armed: model.armedMode == .wifi) { path.append(.wifi) }
                        ModeCard(mode: .live, title: "Go Live", subtitle: "Stream from this iPhone to Facebook, YouTube...",
                                 symbol: "dot.radiowaves.left.and.right", badge: "No PC needed",
                                 armed: model.armedMode == .live) { path.append(.live) }
                        footer
                    }
                    .padding(20)
                }
            }
            .navigationDestination(for: MirrorMode.self) { mode in
                switch mode {
                case .usb: MirrorModeView(mode: .usb)
                case .wifi: MirrorModeView(mode: .wifi)
                case .live: GoLiveView()
                }
            }
        }
        .tint(.white)
    }

    private var header: some View {
        HStack(spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(LinearGradient(colors: Theme.colors(model.armedMode), startPoint: .topLeading, endPoint: .bottomTrailing))
                    .frame(width: 56, height: 56)
                Image(systemName: "iphone.radiowaves.left.and.right").font(.system(size: 26, weight: .bold)).foregroundColor(.white)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text("USB Mirror").font(.largeTitle.weight(.heavy)).foregroundColor(.white)
                Text("Mirror \u{00b7} Stream \u{00b7} Go Live").font(.subheadline.weight(.medium)).foregroundColor(Theme.dim)
            }
        }
        .padding(.top, 12)
        .padding(.bottom, 6)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Tip: keep AssistiveTouch ON for a sharp, full-resolution game picture.", systemImage: "sparkles")
            Label("Free Apple ID: reinstall with Sideloadly every 7 days.", systemImage: "clock.arrow.circlepath")
        }
        .font(.footnote)
        .foregroundColor(Theme.faint)
        .padding(.top, 8)
    }
}

/// One of the three big buttons on the home screen.
struct ModeCard: View {
    let mode: MirrorMode
    let title: String
    let subtitle: String
    let symbol: String
    let badge: String
    let armed: Bool
    let action: () -> Void

    var body: some View {
        Button {
            Haptics.tap()
            action()
        } label: {
            HStack(spacing: 16) {
                ZStack {
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(LinearGradient(colors: Theme.colors(mode), startPoint: .topLeading, endPoint: .bottomTrailing))
                        .frame(width: 64, height: 64)
                        .shadow(color: Theme.colors(mode)[0].opacity(0.5), radius: 12, y: 6)
                    Image(systemName: symbol).font(.system(size: 28, weight: .bold)).foregroundColor(.white)
                }
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Text(title).font(.title2.weight(.heavy)).foregroundColor(.white)
                        Badge(text: badge, color: Theme.colors(mode)[0])
                    }
                    Text(subtitle).font(.subheadline).foregroundColor(Theme.dim).multilineTextAlignment(.leading)
                    if armed {
                        Label("Selected for the next broadcast", systemImage: "checkmark.circle.fill")
                            .font(.caption.weight(.semibold)).foregroundColor(Theme.colors(mode)[0])
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").foregroundColor(Theme.faint)
            }
            .padding(18)
            .background(RoundedRectangle(cornerRadius: 26, style: .continuous).fill(Theme.card))
            .overlay(
                RoundedRectangle(cornerRadius: 26, style: .continuous)
                    .stroke(armed ? AnyShapeStyle(LinearGradient(colors: Theme.colors(mode), startPoint: .leading, endPoint: .trailing))
                                  : AnyShapeStyle(Theme.cardStroke), lineWidth: armed ? 2 : 1)
            )
        }
        .buttonStyle(.plain)
    }
}

/// Small "on air" bar on the home screen while a live runs.
struct LiveMiniBar: View {
    let status: LiveStatus

    var body: some View {
        HStack(spacing: 10) {
            PulsingDot()
            Text(status.state == "live" ? "LIVE" : status.state.capitalized).font(.headline.weight(.heavy))
            Text(formatDuration(status.seconds)).font(.headline.monospacedDigit())
            Spacer()
            Text("\(status.kbps) kbps").font(.subheadline.monospacedDigit()).foregroundColor(Theme.dim)
        }
        .foregroundColor(.white)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 18).fill(Color.red.opacity(0.22)))
    }
}

// MARK: - USB / Wi-Fi screen

struct MirrorModeView: View {
    let mode: MirrorMode
    @EnvironmentObject var model: AppModel

    private var colors: [Color] { Theme.colors(mode) }

    var body: some View {
        ZStack {
            GlowBackground(colors: colors)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    hero
                    if mode == .wifi { wifiCard }
                    Card {
                        VStack(alignment: .leading, spacing: 14) {
                            Text("How to start").font(.headline).foregroundColor(.white)
                            if mode == .usb {
                                StepRow(number: 1, text: "Plug the iPhone into the laptop and unlock it.", color: colors[0])
                            } else {
                                StepRow(number: 1, text: "iPhone and laptop on the same Wi-Fi (5 GHz is best), or the laptop on this iPhone's Personal Hotspot.", color: colors[0])
                            }
                            StepRow(number: 2, text: "Open iPhone USB Mirror on the laptop (Start Mirror).", color: colors[0])
                            StepRow(number: 3, text: "Tap the button below \u{2192} Start Broadcast.", color: colors[0])
                            StepRow(number: 4, text: "Open your game. Stop any time from the red pill / Control Center.", color: colors[0])
                        }
                    }
                    BroadcastButton(title: "Start mirroring", subtitle: mode == .usb ? "Over the USB cable" : "Over Wi-Fi",
                                    symbol: mode == .usb ? "cable.connector" : "wifi", colors: colors)
                    tips
                }
                .padding(20)
            }
        }
        .navigationTitle(mode == .usb ? "USB" : "Wi-Fi")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.armedMode = mode }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: mode == .usb ? "cable.connector" : "wifi")
                .font(.system(size: 40, weight: .bold))
                .foregroundStyle(LinearGradient(colors: colors, startPoint: .leading, endPoint: .trailing))
            Text(mode == .usb ? "Mirror with the cable" : "Mirror over Wi-Fi")
                .font(.title.weight(.heavy)).foregroundColor(.white)
            Text(mode == .usb ? "Lowest delay, sharpest picture and steady sound. Best for ranked games and long streams."
                              : "No cable. The laptop finds this iPhone by itself. A little more delay than USB.")
                .font(.subheadline).foregroundColor(Theme.dim)
        }
        .padding(.vertical, 6)
    }

    private var wifiCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 6) {
                Text("This iPhone's Wi-Fi address").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
                HStack {
                    Text(model.addresses.first ?? "Not on Wi-Fi")
                        .font(.system(size: 30, weight: .heavy, design: .rounded).monospacedDigit())
                        .foregroundColor(.white)
                    Spacer()
                    Image(systemName: model.addresses.isEmpty ? "wifi.slash" : "checkmark.seal.fill")
                        .font(.title2).foregroundColor(model.addresses.isEmpty ? .orange : colors[0])
                }
                Text("If iOS asks to find devices on your local network, tap Allow (Settings \u{203a} Privacy \u{203a} Local Network \u{203a} USB Mirror).")
                    .font(.caption).foregroundColor(Theme.faint)
            }
        }
    }

    private var tips: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Tap Microphone ON in the broadcast sheet only if you want the iPhone mic in the stream.", systemImage: "mic")
            Label(mode == .usb ? "On the laptop: first toolbar button = Auto or USB."
                               : "On the laptop: first toolbar button = Auto or Wi-Fi. W searches now.",
                  systemImage: "laptopcomputer")
        }
        .font(.footnote).foregroundColor(Theme.faint)
    }
}
