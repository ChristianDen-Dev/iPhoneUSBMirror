// Studio: choose what your viewers see - landscape / portrait, how the game
// fills the picture, your logo, a banner text, the LIVE timer and the
// "Be right back" screen. Changes reach a running live within ~1 second.

import PhotosUI
import SwiftUI

struct StudioView: View {
    @EnvironmentObject var model: AppModel
    @State private var photo: PhotosPickerItem?
    @State private var previewBRB = false

    private let colors = Theme.live
    static let bannerSwatches: [Color] = [Color(red: 0.92, green: 0.16, blue: 0.30), Color(red: 0.15, green: 0.40, blue: 0.95),
                                          Color(white: 0.08), Color(red: 0.50, green: 0.25, blue: 0.95)]

    var body: some View {
        ZStack {
            GlowBackground(colors: colors)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    preview
                    if model.liveConnected && model.live.state == "live" {
                        Label("You are live: changes show on the stream in about 1 second.", systemImage: "dot.radiowaves.left.and.right")
                            .font(.caption.weight(.semibold)).foregroundColor(.green)
                    }
                    layoutCard
                    logoCard
                    textCard
                }
                .padding(20)
            }
        }
        .navigationTitle("Studio")
        .navigationBarTitleDisplayMode(.inline)
        .onChange(of: photo) { item in
            Task {
                if let data = try? await item?.loadTransferable(type: Data.self) {
                    model.setLogo(data)
                    Haptics.success()
                }
            }
        }
    }

    // MARK: live preview (what viewers see)

    private var preview: some View {
        let ratio: CGFloat = model.portrait ? 9.0 / 16.0 : 16.0 / 9.0
        return VStack(spacing: 10) {
            GeometryReader { geo in
                let W = geo.size.width, H = geo.size.height
                ZStack {
                    if previewBRB {
                        LinearGradient(colors: [Color(red: 0.10, green: 0.05, blue: 0.22), Color(red: 0.50, green: 0.08, blue: 0.24)],
                                       startPoint: .topLeading, endPoint: .bottomTrailing)
                        VStack(spacing: 4) {
                            Text(model.brbText.isEmpty ? "Be right back" : model.brbText)
                                .font(.system(size: min(W, H) * 0.11, weight: .heavy)).foregroundColor(.white)
                            Text("Stay tuned \u{2014} the stream is still on").font(.system(size: min(W, H) * 0.045, weight: .semibold))
                                .foregroundColor(.white.opacity(0.75))
                        }
                        .multilineTextAlignment(.center).padding(.horizontal, 8)
                    } else {
                        gamePicture(W: W, H: H)
                        if !model.banner.trimmingCharacters(in: .whitespaces).isEmpty {
                            Text(model.banner)
                                .font(.system(size: min(W, H) * 0.055, weight: .heavy)).foregroundColor(.white).lineLimit(1)
                                .padding(.horizontal, min(W, H) * 0.04).padding(.vertical, min(W, H) * 0.02)
                                .background(RoundedRectangle(cornerRadius: min(W, H) * 0.03).fill(Self.bannerSwatches[model.bannerColor].opacity(0.92)))
                                .frame(maxWidth: W * 0.9)
                                .frame(maxHeight: .infinity, alignment: .bottom)
                                .padding(.bottom, H * 0.05)
                        }
                    }
                    overlays(W: W, H: H)
                }
                .frame(width: W, height: H)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            .aspectRatio(ratio, contentMode: .fit)
            .frame(maxWidth: model.portrait ? 230 : .infinity)
            .frame(maxWidth: .infinity)
            .shadow(color: .black.opacity(0.5), radius: 16, y: 8)

            HStack {
                Text("Preview \u{00b7} what viewers see").font(.caption.weight(.semibold)).foregroundColor(Theme.dim)
                Spacer()
                Toggle("Show BRB", isOn: $previewBRB).toggleStyle(.button).font(.caption.weight(.bold)).tint(colors[0])
            }
        }
    }

    /// Stand-in for the game picture, fitted like the real stream.
    private func gamePicture(W: CGFloat, H: CGFloat) -> some View {
        // the iPhone 14 Pro Max screen is 19.5:9; games run sideways
        let src: CGFloat = 19.5 / 9.0
        let fitW = min(W, H * src), fitH = fitW / src
        let fillW = max(W, H * src), fillH = fillW / src
        let game = LinearGradient(colors: [Color(red: 0.10, green: 0.35, blue: 0.25), Color(red: 0.15, green: 0.20, blue: 0.45),
                                           Color(red: 0.45, green: 0.18, blue: 0.30)], startPoint: .topLeading, endPoint: .bottomTrailing)
            .overlay(Image(systemName: "gamecontroller.fill").font(.system(size: min(fitW, fitH) * 0.3)).foregroundColor(.white.opacity(0.35)))
        return ZStack {
            Color.black
            switch model.fit {
            case "fill": game.frame(width: fillW, height: fillH)
            case "fit": game.frame(width: fitW, height: fitH)
            default:
                game.frame(width: fillW, height: fillH).blur(radius: 14).brightness(-0.18)
                game.frame(width: fitW, height: fitH)
            }
        }
        .frame(width: W, height: H)
        .clipped()
    }

    private func overlays(W: CGFloat, H: CGFloat) -> some View {
        let m = H * 0.035
        return ZStack {
            if let logo = model.logoImage {
                Image(uiImage: logo).resizable().scaledToFit()
                    .frame(height: H * model.logoScale)
                    .frame(maxWidth: .infinity, maxHeight: .infinity,
                           alignment: [Alignment.topLeading, .topTrailing, .bottomLeading, .bottomTrailing][min(max(model.logoCorner, 0), 3)])
                    .padding(m)
            }
            if model.badge {
                HStack(spacing: 4) {
                    Circle().fill(Color.white).frame(width: min(W, H) * 0.018, height: min(W, H) * 0.018)
                    Text("LIVE  03:25").font(.system(size: min(W, H) * 0.04, weight: .heavy).monospacedDigit())
                }
                .foregroundColor(.white)
                .padding(.horizontal, min(W, H) * 0.025).padding(.vertical, min(W, H) * 0.012)
                .background(Capsule().fill(Color(red: 1, green: 0.17, blue: 0.33)))
                .frame(maxWidth: .infinity, maxHeight: .infinity,
                       alignment: (model.logoImage != nil && model.logoCorner == 0) ? .topTrailing : .topLeading)
                .padding(m)
            }
        }
    }

    // MARK: settings

    private var layoutCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Layout").font(.headline).foregroundColor(.white)
                Picker("Orientation", selection: $model.portrait) {
                    Label("Landscape", systemImage: "rectangle").tag(false)
                    Label("Portrait", systemImage: "rectangle.portrait").tag(true)
                }
                .pickerStyle(.segmented)
                Text("Game picture").font(.subheadline.weight(.semibold)).foregroundColor(Theme.dim)
                Picker("Fit", selection: $model.fit) {
                    Text("Fit + blur").tag("blur")
                    Text("Fit").tag("fit")
                    Text("Fill (zoom)").tag("fill")
                }
                .pickerStyle(.segmented)
                Text(model.fit == "blur" ? "Whole game, with a soft blurred copy instead of black bars (looks pro)."
                     : model.fit == "fit" ? "Whole game with black bars." : "Fills the picture; the edges are cut off.")
                    .font(.caption).foregroundColor(Theme.faint)
                if model.liveConnected {
                    Text("Landscape / Portrait changes start with the next live.").font(.caption).foregroundColor(.orange)
                }
            }
        }
    }

    private var logoCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Logo / picture").font(.headline).foregroundColor(.white)
                    Spacer()
                    if model.logoImage != nil {
                        Button("Remove") { model.setLogo(nil) }.font(.caption.weight(.bold)).foregroundColor(.orange)
                    }
                }
                PhotosPicker(selection: $photo, matching: .images) {
                    Label(model.logoImage == nil ? "Choose from Photos" : "Change picture", systemImage: "photo.on.rectangle.angled")
                        .font(.subheadline.weight(.bold)).foregroundColor(.white)
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.08)))
                }
                if model.logoImage != nil {
                    HStack(spacing: 8) {
                        ForEach(0..<4) { i in
                            Button {
                                Haptics.tap(); model.logoCorner = i
                            } label: {
                                Image(systemName: ["arrow.up.left", "arrow.up.right", "arrow.down.left", "arrow.down.right"][i])
                                    .font(.subheadline.weight(.bold)).foregroundColor(.white)
                                    .frame(maxWidth: .infinity).padding(.vertical, 10)
                                    .background(RoundedRectangle(cornerRadius: 10)
                                        .fill(model.logoCorner == i ? colors[0].opacity(0.35) : Color.white.opacity(0.06)))
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel(["Top left", "Top right", "Bottom left", "Bottom right"][i])
                        }
                    }
                    HStack {
                        Text("Size").font(.subheadline).foregroundColor(Theme.dim)
                        Slider(value: $model.logoScale, in: 0.06...0.35).tint(colors[0])
                    }
                }
                Text("Tip: a PNG with a see-through background looks best.").font(.caption).foregroundColor(Theme.faint)
            }
        }
    }

    private var textCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Text & screens").font(.headline).foregroundColor(.white)
                Text("Banner (bottom)").font(.subheadline.weight(.semibold)).foregroundColor(Theme.dim)
                TextField("e.g. Ranked grind \u{2022} Follow for more!", text: $model.banner)
                    .foregroundColor(.white).padding(12)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.07)))
                HStack(spacing: 12) {
                    ForEach(0..<Self.bannerSwatches.count, id: \.self) { i in
                        Circle().fill(Self.bannerSwatches[i]).frame(width: 30, height: 30)
                            .overlay(Circle().stroke(Color.white, lineWidth: model.bannerColor == i ? 3 : 0))
                            .onTapGesture { Haptics.tap(); model.bannerColor = i }
                            .accessibilityLabel(["Red", "Blue", "Black", "Purple"][i])
                    }
                    Spacer()
                    if !model.banner.isEmpty {
                        Button("Clear") { model.banner = "" }.font(.caption.weight(.bold)).foregroundColor(.orange)
                    }
                }
                Toggle(isOn: $model.badge) {
                    Label("LIVE badge with timer", systemImage: "record.circle").foregroundColor(.white)
                }
                .tint(colors[0])
                Text("\"Be right back\" screen text").font(.subheadline.weight(.semibold)).foregroundColor(Theme.dim)
                TextField("Be right back", text: $model.brbText)
                    .foregroundColor(.white).padding(12)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.07)))
                Text("Shown when you tap Screen hidden (e.g. \"Starting soon\", \"Break - 5 min\").")
                    .font(.caption).foregroundColor(Theme.faint)
            }
        }
    }
}
