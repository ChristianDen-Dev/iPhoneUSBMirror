// Comments panel (like TikTok LIVE): a small arrow tab on the right edge.
// Tap it -> the comments slide in. Tap the arrow again -> they hide.
// The panel shows your platform's live chat in a built-in browser. You sign
// in to Facebook / YouTube / TikTok inside it yourself (once; it remembers).

import SwiftUI
import WebKit

/// One web view for the whole app, so the chat keeps its place and login
/// when the panel is hidden or you change screens.
@MainActor
final class CommentsBrowser: NSObject, ObservableObject, WKNavigationDelegate {
    static let shared = CommentsBrowser()
    @Published var loading = false
    let webView: WKWebView
    private var loaded: URL?
    private var loadedDesktop = false

    private static let desktopAgent =
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"

    override init() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()            // keeps you signed in
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = .all   // never plays your own live (echo)
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.backgroundColor = .clear
        webView.allowsBackForwardNavigationGestures = true
        super.init()
        webView.navigationDelegate = self
    }

    func show(_ url: URL, desktop: Bool, force: Bool = false) {
        guard force || url != loaded || desktop != loadedDesktop else { return }
        loaded = url
        loadedDesktop = desktop
        webView.customUserAgent = desktop ? Self.desktopAgent : nil
        webView.load(URLRequest(url: url))
    }

    func reload() {
        if webView.url == nil, let u = loaded { webView.load(URLRequest(url: u)) } else { webView.reload() }
    }

    nonisolated func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        Task { @MainActor in self.loading = true }
    }
    nonisolated func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        Task { @MainActor in self.loading = false }
    }
    nonisolated func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        Task { @MainActor in self.loading = false }
    }
    nonisolated func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        Task { @MainActor in self.loading = false }
    }
}

private struct WebViewHost: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { CommentsBrowser.shared.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

/// Edge tab + sliding panel. Put it on top of the whole app.
struct CommentsOverlay: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        GeometryReader { geo in
            let w = min(geo.size.width * 0.88, 430)
            HStack(spacing: 0) {
                Spacer(minLength: 0)
                EdgeTab(open: model.commentsOpen, unread: model.liveConnected && model.live.state == "live") {
                    Haptics.tap()
                    withAnimation(.spring(response: 0.38, dampingFraction: 0.86)) { model.commentsOpen.toggle() }
                }
                CommentsPanel()
                    .frame(width: w)
                    .frame(maxHeight: .infinity)
                    .simultaneousGesture(DragGesture(minimumDistance: 30).onEnded { g in   // swipe right to hide
                        if g.translation.width > 80 && abs(g.translation.height) < 60 {
                            withAnimation(.spring(response: 0.38, dampingFraction: 0.86)) { model.commentsOpen = false }
                        }
                    })
            }
            .offset(x: model.commentsOpen ? 0 : w)
        }
    }
}

/// The little arrow handle on the screen edge.
private struct EdgeTab: View {
    let open: Bool
    let unread: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: open ? "chevron.right" : "chevron.left")
                    .font(.system(size: 17, weight: .heavy))
                Image(systemName: "bubble.left.and.bubble.right.fill")
                    .font(.system(size: 13, weight: .bold))
                if unread && !open {
                    Circle().fill(Color.red).frame(width: 7, height: 7)
                }
            }
            .foregroundColor(.white)
            .frame(width: 36, height: 96)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(LinearGradient(colors: Theme.live, startPoint: .top, endPoint: .bottom))
                    .padding(.trailing, -14)                  // square on the screen side
            )
            .clipped()
            .shadow(color: Theme.live[0].opacity(0.5), radius: 10, x: -2)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(open ? "Hide comments" : "Show comments")
    }
}

/// The panel itself.
struct CommentsPanel: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject private var browser = CommentsBrowser.shared
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        VStack(spacing: 0) {
            header
            if editing { linkEditor }
            ZStack {
                if model.commentsPage != nil {
                    WebViewHost()
                } else {
                    emptyState
                }
                if browser.loading {
                    ProgressView().tint(.white).padding(10)
                        .background(Circle().fill(Color.black.opacity(0.5)))
                }
            }
            .frame(maxHeight: .infinity)
            controls
        }
        .background(
            Rectangle().fill(.ultraThinMaterial)
                .overlay(Rectangle().fill(Color.black.opacity(0.55)))
                .ignoresSafeArea()
        )
        .overlay(alignment: .leading) {
            Rectangle().fill(LinearGradient(colors: Theme.live, startPoint: .top, endPoint: .bottom))
                .frame(width: 2).ignoresSafeArea()
        }
        .onAppear(perform: load)
        .onChange(of: model.commentsLink) { _ in load() }
        .onChange(of: model.platform) { _ in load() }
        .onChange(of: model.commentsOpen) { open in if open { load() } }
    }

    private func load() {
        if let page = model.commentsPage { browser.show(page.url, desktop: page.desktop) }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: model.platform.symbol).foregroundColor(model.platform.color).font(.title3.weight(.bold))
            VStack(alignment: .leading, spacing: 1) {
                Text("Comments").font(.headline.weight(.heavy)).foregroundColor(.white)
                if model.liveConnected && model.live.state == "live" {
                    HStack(spacing: 6) {
                        Circle().fill(Color.red).frame(width: 6, height: 6)
                        Text("LIVE \(formatDuration(model.live.seconds))").font(.caption2.weight(.bold).monospacedDigit())
                    }
                    .foregroundColor(.white)
                } else {
                    Text(model.platform.name).font(.caption2).foregroundColor(Theme.dim)
                }
            }
            Spacer()
            headerButton("arrow.clockwise") { browser.reload() }
            headerButton(editing ? "xmark" : "link") {
                draft = model.commentsLink
                withAnimation { editing.toggle() }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    private var linkEditor: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Paste your live video link (best), or leave empty for the \(model.platform.name) dashboard.")
                .font(.caption).foregroundColor(Theme.dim).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                TextField("https://...", text: $draft)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    .font(.callout).foregroundColor(.white)
                    .padding(10).background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.08)))
                Button("Paste") { draft = UIPasteboard.general.string ?? draft }
                    .font(.caption.weight(.bold)).foregroundColor(.white)
                Button("Open") {
                    model.commentsLink = draft
                    if let page = model.commentsPage { browser.show(page.url, desktop: page.desktop, force: true) }
                    withAnimation { editing = false }
                }
                .font(.caption.weight(.heavy)).foregroundColor(Theme.live[0])
            }
        }
        .padding(.horizontal, 14).padding(.bottom, 10)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "bubble.left.and.text.bubble.right").font(.system(size: 36)).foregroundColor(Theme.faint)
            Text("Tap the link button and paste your live page to see comments here.")
                .font(.subheadline).foregroundColor(Theme.dim).multilineTextAlignment(.center)
        }
        .padding(24)
    }

    /// Quick live buttons right under the chat.
    private var controls: some View {
        HStack(spacing: 10) {
            LiveToggle(title: model.micMuted ? "Mic off" : "Mic on",
                       symbol: model.micMuted ? "mic.slash.fill" : "mic.fill",
                       on: !model.micMuted, color: .green) { model.micMuted.toggle() }
            LiveToggle(title: model.privacy ? "Hidden" : "Screen",
                       symbol: model.privacy ? "eye.slash.fill" : "eye.fill",
                       on: !model.privacy, color: .green, offColor: .orange) { model.privacy.toggle() }
        }
        .padding(12)
    }

    private func headerButton(_ symbol: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.subheadline.weight(.bold)).foregroundColor(.white)
                .frame(width: 34, height: 34).background(Circle().fill(Color.white.opacity(0.1)))
        }
        .buttonStyle(.plain)
    }
}

/// Pill button for a live control (mic, privacy screen).
struct LiveToggle: View {
    let title: String
    let symbol: String
    let on: Bool
    var color: Color = .green
    var offColor: Color = .red
    let action: () -> Void

    var body: some View {
        Button {
            Haptics.tap()
            action()
        } label: {
            HStack(spacing: 8) {
                Image(systemName: symbol).font(.subheadline.weight(.bold))
                Text(title).font(.subheadline.weight(.heavy))
            }
            .foregroundColor(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill((on ? color : offColor).opacity(0.28)))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke((on ? color : offColor).opacity(0.7), lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}
