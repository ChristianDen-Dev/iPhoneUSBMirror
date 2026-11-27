// "Go Live" screen: stream from this iPhone to Facebook / YouTube / TikTok /
// any RTMP server, no laptop (like PRISM Live Studio, with a stream key).

import SwiftUI
import UIKit

struct GoLiveView: View {
    @EnvironmentObject var model: AppModel
    @State private var showKey = false
    @State private var showAdvanced = false
    @FocusState private var keyFocused: Bool

    private let colors = Theme.live
    private var onAir: Bool {
        model.liveConnected && ["live", "connecting", "reconnecting"].contains(model.live.state)
    }

    var body: some View {
        ZStack {
            GlowBackground(colors: colors)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if model.liveConnected || model.live.state == "error" || model.live.state == "ended" {
                        LiveDashboard(status: model.live, connected: model.liveConnected)
                        if onAir { liveControls }
                    } else {
                        hero
                    }
                    studioLink
                    platformCard
                    keyCard
                    qualityCard
                    soundCard
                    checklist
                    BroadcastButton(title: onAir ? "Stop / change broadcast" : "GO LIVE",
                                    subtitle: model.liveReady ? "\(model.platform.name) \u{00b7} \(model.quality.name) \u{00b7} \(model.portrait ? "Portrait" : "Landscape")"
                                                              : "Add your stream key first",
                                    symbol: "dot.radiowaves.left.and.right", colors: model.liveReady ? colors : [.gray, .gray])
                        .disabled(!model.liveReady)
                        .opacity(model.liveReady ? 1 : 0.6)
                    tips
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .navigationTitle("Go Live")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.armedMode = .live }
    }

    // MARK: sections

    private var hero: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: "dot.radiowaves.left.and.right")
                .font(.system(size: 40, weight: .bold))
                .foregroundStyle(LinearGradient(colors: colors, startPoint: .leading, endPoint: .trailing))
            Text("Stream from this iPhone").font(.title.weight(.heavy)).foregroundColor(.white)
            Text("Your game screen, game sound and your voice go straight to your live. No laptop needed.")
                .font(.subheadline).foregroundColor(Theme.dim)
        }
        .padding(.vertical, 6)
    }

    /// Quick buttons while live: mic, privacy screen, comments.
    private var liveControls: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                LiveToggle(title: model.micMuted ? "Mic off" : "Mic on",
                           symbol: model.micMuted ? "mic.slash.fill" : "mic.fill",
                           on: !model.micMuted) { model.micMuted.toggle() }
                LiveToggle(title: model.privacy ? "Screen hidden" : "Screen shown",
                           symbol: model.privacy ? "eye.slash.fill" : "eye.fill",
                           on: !model.privacy, offColor: .orange) { model.privacy.toggle() }
            }
            LiveToggle(title: "Show comments", symbol: "bubble.left.and.bubble.right.fill", on: true, color: colors[0]) {
                withAnimation(.spring(response: 0.38, dampingFraction: 0.86)) { model.commentsOpen = true }
            }
            if model.privacy {
                Text("Viewers now see \"Be right back\". Your sound still plays. Tap Screen hidden to show the game again.")
                    .font(.caption).foregroundColor(.orange).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Opens the Studio (what viewers see).
    private var studioLink: some View {
        let fitName = model.fit == "blur" ? "Fit + blur" : (model.fit == "fit" ? "Fit" : "Fill")
        var summary = (model.portrait ? "Portrait" : "Landscape") + " \u{00b7} " + fitName
        if model.logoImage != nil { summary += " \u{00b7} logo" }
        if !model.banner.isEmpty { summary += " \u{00b7} banner" }
        return NavigationLink {
            StudioView()
        } label: {
            HStack(spacing: 14) {
                ZStack {
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(LinearGradient(colors: [Color.purple, colors[0]], startPoint: .topLeading, endPoint: .bottomTrailing))
                        .frame(width: 48, height: 48)
                    Image(systemName: "wand.and.stars").font(.title3.weight(.bold)).foregroundColor(.white)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("Studio \u{00b7} stream look").font(.headline.weight(.heavy)).foregroundColor(.white)
                    Text(summary).font(.caption).foregroundColor(Theme.dim)
                }
                Spacer()
                Image(systemName: "chevron.right").foregroundColor(Theme.faint)
            }
            .padding(16)
            .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(Theme.card))
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(colors[0].opacity(0.5), lineWidth: 1))
        }
        .buttonStyle(.plain)
    }

    private var platformCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Platform").font(.headline).foregroundColor(.white)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(Platform.allCases) { p in
                        Button {
                            Haptics.tap()
                            model.platform = p
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: p.symbol).font(.title3.weight(.bold)).foregroundColor(p.color)
                                Text(p.name).font(.subheadline.weight(.bold)).foregroundColor(.white)
                                Spacer(minLength: 0)
                                if model.platform == p {
                                    Image(systemName: "checkmark.circle.fill").foregroundColor(p.color)
                                }
                            }
                            .padding(12)
                            .background(RoundedRectangle(cornerRadius: 14).fill(model.platform == p ? p.color.opacity(0.18) : Color.white.opacity(0.04)))
                            .overlay(RoundedRectangle(cornerRadius: 14).stroke(model.platform == p ? p.color : Theme.cardStroke, lineWidth: 1))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private var keyCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Stream key").font(.headline).foregroundColor(.white)
                    Spacer()
                    Label("Saved in Keychain", systemImage: "lock.fill").font(.caption2).foregroundColor(Theme.faint)
                }
                HStack(spacing: 8) {
                    Group {
                        if showKey {
                            TextField("Paste your stream key", text: $model.key)
                        } else {
                            SecureField("Paste your stream key", text: $model.key)
                        }
                    }
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($keyFocused)
                    .font(.body.monospaced())
                    .foregroundColor(.white)
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.07)))

                    iconButton(showKey ? "eye.slash" : "eye") { showKey.toggle() }
                    iconButton("doc.on.clipboard") {
                        if let s = UIPasteboard.general.string?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
                            model.key = s
                            Haptics.success()
                        }
                    }
                }
                if model.platform == .tiktok || model.platform == .custom || showAdvanced {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Server URL").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
                        TextField("rtmp://...  or  rtmps://...", text: $model.server)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                            .font(.callout.monospaced())
                            .foregroundColor(.white)
                            .padding(12)
                            .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.07)))
                        if model.platform.server != "" && model.server != model.platform.server {
                            Button("Reset to the \(model.platform.name) server") { model.server = model.platform.server }
                                .font(.caption.weight(.semibold)).foregroundColor(colors[0])
                        }
                    }
                } else {
                    Button(showAdvanced ? "" : "Change server (advanced)") { showAdvanced = true }
                        .font(.caption).foregroundColor(Theme.faint)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text(model.platform.keyHelp).font(.caption).foregroundColor(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                    if let url = model.platform.keyHelpURL {
                        Link(destination: url) {
                            Label("Where do I get my key?", systemImage: "arrow.up.right.square")
                                .font(.caption.weight(.bold)).foregroundColor(model.platform.color)
                        }
                    }
                }
            }
        }
    }

    private var qualityCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Quality").font(.headline).foregroundColor(.white)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(Quality.all) { q in
                        Button {
                            Haptics.tap()
                            model.qualityID = q.id
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(q.name).font(.subheadline.weight(.heavy)).foregroundColor(.white)
                                Text(q.detail).font(.caption).foregroundColor(Theme.dim)
                                Text("\(String(format: "%.1f", Double(q.bitrate) / 1_000_000)) Mbps upload")
                                    .font(.caption2).foregroundColor(Theme.faint)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(RoundedRectangle(cornerRadius: 14).fill(model.qualityID == q.id ? colors[0].opacity(0.2) : Color.white.opacity(0.04)))
                            .overlay(RoundedRectangle(cornerRadius: 14).stroke(model.qualityID == q.id ? colors[0] : Theme.cardStroke, lineWidth: 1))
                        }
                        .buttonStyle(.plain)
                    }
                }
                Picker("Orientation", selection: $model.portrait) {
                    Label("Landscape", systemImage: "rectangle").tag(false)
                    Label("Portrait", systemImage: "rectangle.portrait").tag(true)
                }
                .pickerStyle(.segmented)
                Text(model.portrait ? "Vertical video (good for TikTok / Reels style)." : "Wide video (best for Mobile Legends and most games).")
                    .font(.caption).foregroundColor(Theme.faint)
            }
        }
    }

    private var soundCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Sound").font(.headline).foregroundColor(.white)
                volumeRow("Game sound", symbol: "gamecontroller.fill", value: $model.gameVolume)
                volumeRow("Your voice (mic)", symbol: "mic.fill", value: $model.micVolume)
                Text("Your voice is only included if you tap Microphone ON in the Start Broadcast sheet.")
                    .font(.caption).foregroundColor(Theme.faint)
            }
        }
    }

    private var checklist: some View {
        let hasKey = !model.key.trimmingCharacters(in: .whitespaces).isEmpty
        let hasServer = model.config(for: .live).publishURL != nil || !hasKey
        return Card {
            VStack(alignment: .leading, spacing: 8) {
                check("Stream key added", hasKey)
                check("Server address OK", hasServer && !model.server.isEmpty)
                check("Live set up on \(model.platform.name) (Live Producer / Studio open)", nil)
                check("Do Not Disturb ON (no calls or pop-ups on stream)", nil)
            }
        }
    }

    private var tips: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Start the broadcast from THIS screen so it uses these settings.", systemImage: "info.circle")
            Label("Test first with privacy \"Only me\" on Facebook.", systemImage: "eye.slash")
            Label("Long lives make the phone warm: Data saver or HD keeps it cooler.", systemImage: "thermometer.medium")
        }
        .font(.footnote).foregroundColor(Theme.faint)
    }

    // MARK: helpers

    private func iconButton(_ symbol: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.body.weight(.semibold)).foregroundColor(.white)
                .frame(width: 44, height: 44)
                .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
    }

    private func volumeRow(_ title: String, symbol: String, value: Binding<Double>) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundColor(colors[0]).frame(width: 22)
            Text(title).font(.subheadline).foregroundColor(.white).frame(width: 120, alignment: .leading)
            Slider(value: value, in: 0...2, step: 0.05).tint(colors[0])
            Text("\(Int(value.wrappedValue * 100))%").font(.caption.monospacedDigit()).foregroundColor(Theme.dim).frame(width: 44)
        }
    }

    private func check(_ text: String, _ ok: Bool?) -> some View {
        HStack(spacing: 10) {
            Image(systemName: ok == nil ? "circle.dashed" : (ok! ? "checkmark.circle.fill" : "xmark.circle.fill"))
                .foregroundColor(ok == nil ? Theme.faint : (ok! ? .green : .orange))
            Text(text).font(.subheadline).foregroundColor(ok == nil ? Theme.dim : .white)
        }
    }
}

/// Big status panel while a live runs.
struct LiveDashboard: View {
    let status: LiveStatus
    let connected: Bool

    private var title: String {
        switch status.state {
        case "live": return "LIVE"
        case "connecting": return "Connecting..."
        case "reconnecting": return "Reconnecting..."
        case "error": return "Stopped"
        case "ended": return "Live ended"
        default: return status.state.capitalized
        }
    }
    private var color: Color {
        switch status.state {
        case "live": return .red
        case "connecting", "reconnecting": return .orange
        default: return .gray
        }
    }
    /// Stream health from real upload vs target.
    private var health: (String, Color) {
        guard status.state == "live", status.targetKbps > 0 else { return ("-", Theme.faint) }
        let ratio = Double(status.kbps) / Double(status.targetKbps + 128)
        if ratio > 0.75 { return ("Excellent", .green) }
        if ratio > 0.45 { return ("Good", .yellow) }
        return ("Weak internet", .orange)
    }

    var body: some View {
        Card(padding: 20) {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 12) {
                    if status.state == "live" { PulsingDot() } else { Circle().fill(color).frame(width: 10, height: 10) }
                    Text(title).font(.title.weight(.heavy)).foregroundColor(.white)
                    Spacer()
                    Text(formatDuration(status.seconds))
                        .font(.system(size: 30, weight: .heavy, design: .rounded).monospacedDigit())
                        .foregroundColor(.white)
                }
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                    stat("Upload", "\(status.kbps)", "kbps")
                    stat("Quality", "\(status.targetKbps)", "kbps")
                    stat("Picture", "\(status.fps)", "fps")
                    stat("Size", "\(status.height)p", status.width > status.height ? "wide" : "tall")
                    stat("Mic", status.micMuted ? "Muted" : (status.mic ? "On" : "Off"), status.privacy ? "screen hidden" : "")
                    stat("Drops", "\(status.reconnects)", "reconnects")
                }
                HStack {
                    Text("Stream health").font(.subheadline).foregroundColor(Theme.dim)
                    Spacer()
                    Text(health.0).font(.subheadline.weight(.bold)).foregroundColor(health.1)
                }
                if !status.message.isEmpty {
                    Text(status.message).font(.footnote).foregroundColor(.orange).fixedSize(horizontal: false, vertical: true)
                }
                if !connected && status.state != "error" {
                    Text("The broadcast is not running.").font(.footnote).foregroundColor(Theme.faint)
                }
            }
        }
    }

    private func stat(_ title: String, _ value: String, _ unit: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2.weight(.semibold)).foregroundColor(Theme.faint)
            Text(value).font(.headline.weight(.heavy).monospacedDigit()).foregroundColor(.white)
            Text(unit).font(.caption2).foregroundColor(Theme.dim)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
