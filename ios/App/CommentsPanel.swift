// Live comments, TikTok style.
//
//  - An arrow handle sits on the screen edge. Drag it anywhere (it snaps to
//    the nearest side). Tap it -> comments float on screen, see-through,
//    no box (like TikTok LIVE). Tap anywhere else -> they hide again.
//  - The feed itself can be dragged anywhere and made more / less see-through.
//  - "Full chat" opens the platform's chat page (sign in there once; reply there).
//  - "Float over game" shows the comments in a small Picture-in-Picture
//    window ON TOP of the game (iOS only allows that for PiP). Swipe the PiP
//    window to the screen edge to hide it behind an arrow; tap the arrow to
//    bring it back.
//
// Comments are read from the chat page with a small script (YouTube live
// chat, TikTok LIVE web, Facebook comments). Nothing is sent anywhere.

import AVKit
import SwiftUI
import WebKit

struct ChatMessage: Identifiable, Equatable {
    let id: Int
    let author: String
    let text: String
}

// MARK: - the hidden chat page + comment reader

@MainActor
final class CommentsBrowser: NSObject, ObservableObject, WKNavigationDelegate {
    static let shared = CommentsBrowser()
    @Published var loading = false
    @Published var messages: [ChatMessage] = []
    let webView: WKWebView
    private var loaded: URL?
    private var loadedDesktop = false
    private var seen = Set<String>()
    private var nextID = 0
    private var timer: Timer?

    private static let desktopAgent =
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"

    /// Reads [author, text] pairs from the page (newest last).
    private static let readScript = """
    (function(){
      var out=[];
      function add(a,t){ t=(t||'').trim(); if(t) out.push([(a||'').trim(), t.slice(0,200)]); }
      document.querySelectorAll('yt-live-chat-text-message-renderer').forEach(function(e){
        var a=e.querySelector('#author-name'), m=e.querySelector('#message'); add(a&&a.innerText, m&&m.innerText); });
      if(!out.length) document.querySelectorAll('[data-e2e="chat-message"]').forEach(function(e){
        var a=e.querySelector('[data-e2e="message-owner-name"]'); var t=e.innerText||'';
        if(a){ t=t.replace(a.innerText,''); } add(a&&a.innerText, t.replace(/^\\s*:\\s*/,'')); });
      if(!out.length) document.querySelectorAll('[role="article"][aria-label]').forEach(function(e){
        var l=e.getAttribute('aria-label')||''; if(!/comment/i.test(l)) return;
        var name=l.replace(/^(Comment|Reply) by /i,'').replace(/\\s+\\d+\\s*\\w+\\s*ago$/i,'').replace(/ (?:a|an|one) \\w+ ago$/i,'');
        var parts=[]; e.querySelectorAll('div[dir="auto"]').forEach(function(d){ if(d.innerText) parts.push(d.innerText); });
        add(name, parts.join(' ')); });
      return JSON.stringify(out.slice(-40));
    })()
    """

    override init() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()            // keeps you signed in
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = .all   // never plays your own live (echo)
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 700), configuration: cfg)
        webView.allowsBackForwardNavigationGestures = true
        super.init()
        webView.navigationDelegate = self
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.readComments() }
        }
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

    private func readComments() {
        guard loaded != nil, !loading else { return }
        webView.evaluateJavaScript(Self.readScript) { [weak self] result, _ in
            guard let self = self, let json = result as? String, let data = json.data(using: .utf8),
                  let pairs = try? JSONSerialization.jsonObject(with: data) as? [[String]] else { return }
            var added: [ChatMessage] = []
            for p in pairs where p.count == 2 {
                let key = p[0] + "\u{1}" + p[1]
                if self.seen.insert(key).inserted {
                    self.nextID += 1
                    added.append(ChatMessage(id: self.nextID, author: p[0], text: p[1]))
                }
            }
            guard !added.isEmpty else { return }
            self.messages = Array((self.messages + added).suffix(60))
            ChatPiP.shared.refresh()
        }
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

/// Holds the one shared web view. It moves to whichever host is on screen.
struct WebViewHost: UIViewRepresentable {
    func makeUIView(context: Context) -> UIView {
        let box = UIView()
        adopt(box)
        return box
    }
    func updateUIView(_ box: UIView, context: Context) { adopt(box) }
    static func dismantleUIView(_ box: UIView, coordinator: ()) {
        let web = CommentsBrowser.shared.webView
        if web.superview === box { web.removeFromSuperview() }
    }
    private func adopt(_ box: UIView) {
        DispatchQueue.main.async {
            let web = CommentsBrowser.shared.webView
            guard web.superview !== box else { return }
            web.removeFromSuperview()
            web.frame = box.bounds
            web.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            box.addSubview(web)
        }
    }
}

// MARK: - floating comments (TikTok style)

struct FloatingComments: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject private var browser = CommentsBrowser.shared
    @ObservedObject private var pip = ChatPiP.shared
    @State private var showFullChat = false
    @AppStorage("chat.handleRight") private var handleRight = true
    @AppStorage("chat.handleY") private var handleY = 0.55        // 0...1 of the screen height
    @AppStorage("chat.feedX") private var feedX = 0.0             // feed centre offset from default spot
    @AppStorage("chat.feedY") private var feedY = 0.0
    @AppStorage("chat.opacity") private var feedOpacity = 0.9
    @GestureState private var handleDrag: CGSize = .zero
    @GestureState private var feedDrag: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            let size = geo.size
            ZStack(alignment: .topLeading) {
                // keeps the chat page alive (hidden behind the app) when the full chat is closed
                if !showFullChat {
                    WebViewHost().frame(width: 390, height: 700).opacity(0.01).allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
                PiPLayerHost().frame(width: 2, height: 2).allowsHitTesting(false).accessibilityHidden(true)

                if model.commentsOpen {
                    // tap anywhere else = hide (back to what you were doing)
                    Color.black.opacity(0.001).ignoresSafeArea()
                        .onTapGesture { close() }
                    feed(size: size)
                        .transition(.opacity.combined(with: .move(edge: handleRight ? .trailing : .leading)))
                }
                handle(size: size)
            }
        }
        .sheet(isPresented: $showFullChat) { FullChatSheet().environmentObject(model) }
        .onAppear(perform: loadPage)
        .onChange(of: model.commentsLink) { _ in loadPage() }
        .onChange(of: model.platform) { _ in loadPage() }
    }

    private func loadPage() {
        if let page = model.commentsPage { browser.show(page.url, desktop: page.desktop) }
    }

    private func close() {
        withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) { model.commentsOpen = false }
    }

    // the arrow tab: tap = show/hide, drag = move to any side
    private func handle(size: CGSize) -> some View {
        let baseX = handleRight ? size.width - 18 : 18
        let baseY = min(max(handleY * size.height, 60), size.height - 60)
        return Button {
            Haptics.tap()
            withAnimation(.spring(response: 0.35, dampingFraction: 0.85)) { model.commentsOpen.toggle() }
        } label: {
            VStack(spacing: 6) {
                Image(systemName: (model.commentsOpen == handleRight) ? "chevron.right" : "chevron.left")
                    .font(.system(size: 15, weight: .heavy))
                Image(systemName: "text.bubble.fill").font(.system(size: 12, weight: .bold))
                if !model.commentsOpen && !browser.messages.isEmpty {
                    Text("\(min(browser.messages.count, 99))").font(.system(size: 9, weight: .heavy))
                }
            }
            .foregroundColor(.white)
            .frame(width: 34, height: 84)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 17, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 17, style: .continuous).stroke(Color.white.opacity(0.25), lineWidth: 1))
            .opacity(0.85)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(model.commentsOpen ? "Hide comments" : "Show comments")
        .position(x: baseX + handleDrag.width, y: baseY + handleDrag.height)
        .simultaneousGesture(
            DragGesture(minimumDistance: 8)
                .updating($handleDrag) { v, s, _ in s = v.translation }
                .onEnded { v in
                    let x = baseX + v.translation.width
                    handleRight = x > size.width / 2            // snap to the nearest side
                    handleY = Double(min(max((baseY + v.translation.height) / size.height, 0.08), 0.92))
                }
        )
    }

    // the see-through comment list
    private func feed(size: CGSize) -> some View {
        let w = min(size.width * 0.78, 330)
        let h = min(size.height * 0.42, 340)
        let baseX = (handleRight ? 16 + w / 2 : size.width - 16 - w / 2) + feedX
        let baseY = size.height * 0.62 + feedY
        return VStack(alignment: .leading, spacing: 8) {
            feedToolbar
            ScrollViewReader { proxy in
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 6) {
                        if browser.messages.isEmpty {
                            bubble(author: "Den-Dev Live", text: model.commentsPage == nil
                                   ? "Tap Full chat and paste your live link to see comments here."
                                   : "Waiting for comments\u{2026} (open Full chat once to sign in)", system: true)
                        }
                        ForEach(browser.messages.suffix(30)) { m in
                            bubble(author: m.author, text: m.text).id(m.id)
                                .transition(.move(edge: .bottom).combined(with: .opacity))
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: browser.messages.last?.id) { id in
                    if let id = id { withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(id, anchor: .bottom) } }
                }
                .onAppear { if let id = browser.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) } }
            }
            // older comments fade out at the top, like TikTok
            .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.25),
                                         .init(color: .black, location: 1)], startPoint: .top, endPoint: .bottom))
        }
        .frame(width: w, height: h)
        .opacity(feedOpacity)
        .position(x: baseX + feedDrag.width, y: baseY + feedDrag.height)
        .gesture(
            DragGesture(minimumDistance: 12)
                .updating($feedDrag) { v, s, _ in s = v.translation }
                .onEnded { v in
                    feedX += v.translation.width
                    feedY += v.translation.height
                    // keep it on screen
                    let x = (handleRight ? 16 + w / 2 : size.width - 16 - w / 2) + feedX
                    let y = size.height * 0.62 + feedY
                    if x < w / 2 { feedX += w / 2 - x }
                    if x > size.width - w / 2 { feedX -= x - (size.width - w / 2) }
                    if y < h / 2 + 40 { feedY += h / 2 + 40 - y }
                    if y > size.height - h / 2 { feedY -= y - (size.height - h / 2) }
                }
        )
    }

    private var feedToolbar: some View {
        HStack(spacing: 8) {
            pill("Full chat", "bubble.left.and.text.bubble.right.fill") { showFullChat = true }
            if pip.supported {
                pill(pip.active ? "Stop float" : "Float over game", "pip.enter") {
                    pip.active ? pip.stop() : pip.start()
                }
            }
            Menu {
                Section("See-through") {
                    ForEach([1.0, 0.8, 0.6, 0.4], id: \.self) { v in
                        Button("\(Int(v * 100))%\(feedOpacity == v ? "  \u{2713}" : "")") { feedOpacity = v }
                    }
                }
                Button("Reset position") { feedX = 0; feedY = 0 }
                Button("Clear comments") { browser.messages = [] }
            } label: {
                Image(systemName: "slider.horizontal.3").font(.caption.weight(.bold)).foregroundColor(.white)
                    .frame(width: 30, height: 30).background(Circle().fill(Color.black.opacity(0.35)))
            }
        }
    }

    private func pill(_ title: String, _ symbol: String, _ action: @escaping () -> Void) -> some View {
        Button { Haptics.tap(); action() } label: {
            Label(title, systemImage: symbol).font(.caption.weight(.bold)).foregroundColor(.white)
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(Capsule().fill(Color.black.opacity(0.35)))
        }
        .buttonStyle(.plain)
    }

    /// One comment: light dark bubble + text shadow, readable over any picture.
    private func bubble(author: String, text: String, system: Bool = false) -> some View {
        (Text(author.isEmpty ? "" : author + "  ").font(.subheadline.weight(.heavy))
            .foregroundColor(system ? Theme.live[1] : Color(red: 0.62, green: 0.85, blue: 1.0))
         + Text(text).font(.subheadline.weight(.semibold)).foregroundColor(.white))
            .shadow(color: .black.opacity(0.8), radius: 2, y: 1)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.black.opacity(0.22)))
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - full chat page (sign in, read everything, reply)

struct FullChatSheet: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject private var browser = CommentsBrowser.shared
    @Environment(\.dismiss) private var dismiss
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: model.platform.symbol).foregroundColor(model.platform.color).font(.title3.weight(.bold))
                Text("\(model.platform.name) chat").font(.headline.weight(.heavy)).foregroundColor(.white)
                if browser.loading { ProgressView().tint(.white) }
                Spacer()
                round("arrow.clockwise") { browser.reload() }
                round(editing ? "xmark" : "link") { draft = model.commentsLink; withAnimation { editing.toggle() } }
                round("chevron.down") { dismiss() }
            }
            .padding(14)
            if editing {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Paste your live video link (best). Empty = the \(model.platform.name) dashboard.")
                        .font(.caption).foregroundColor(Theme.dim)
                    HStack(spacing: 8) {
                        TextField("https://...", text: $draft)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                            .foregroundColor(.white).padding(10)
                            .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.08)))
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
            WebViewHost()
            HStack(spacing: 10) {
                LiveToggle(title: model.micMuted ? "Mic off" : "Mic on", symbol: model.micMuted ? "mic.slash.fill" : "mic.fill",
                           on: !model.micMuted) { model.micMuted.toggle() }
                LiveToggle(title: model.privacy ? "Screen hidden" : "Screen shown",
                           symbol: model.privacy ? "eye.slash.fill" : "eye.fill",
                           on: !model.privacy, offColor: .orange) { model.privacy.toggle() }
            }
            .padding(12)
        }
        .background(Theme.bgTop.ignoresSafeArea())
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private func round(_ symbol: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.subheadline.weight(.bold)).foregroundColor(.white)
                .frame(width: 34, height: 34).background(Circle().fill(Color.white.opacity(0.1)))
        }
        .buttonStyle(.plain)
    }
}

// MARK: - comments over the game (Picture in Picture)

/// iOS lets only Picture-in-Picture windows float over other apps, so the
/// comments are drawn into a small "video" that plays in PiP.
@MainActor
final class ChatPiP: NSObject, ObservableObject, AVPictureInPictureControllerDelegate,
                     AVPictureInPictureSampleBufferPlaybackDelegate {
    static let shared = ChatPiP()
    @Published var active = false
    let displayLayer = AVSampleBufferDisplayLayer()
    private var controller: AVPictureInPictureController?
    private var timer: Timer?
    private var format: CMVideoFormatDescription?

    var supported: Bool { AVPictureInPictureController.isPictureInPictureSupported() }

    private func setUp() {
        guard controller == nil, supported else { return }
        // PiP needs an active playback audio session; mix so the game sound is not touched
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try? AVAudioSession.sharedInstance().setActive(true)
        displayLayer.videoGravity = .resizeAspect
        let source = AVPictureInPictureController.ContentSource(sampleBufferDisplayLayer: displayLayer, playbackDelegate: self)
        let c = AVPictureInPictureController(contentSource: source)
        c.delegate = self
        c.requiresLinearPlayback = true
        controller = c
    }

    func start() {
        setUp()
        refresh()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            Task { @MainActor in ChatPiP.shared.refresh() }
        }
        // the layer needs a frame before PiP can start
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.controller?.canStartPictureInPictureAutomaticallyFromInline = true   // also starts when you leave the app
            self?.controller?.startPictureInPicture()
        }
    }

    func stop() {
        controller?.canStartPictureInPictureAutomaticallyFromInline = false
        controller?.stopPictureInPicture()
        timer?.invalidate(); timer = nil
        active = false
    }

    /// Draw the newest comments and show them.
    func refresh() {
        guard controller != nil else { return }
        let msgs = Array(CommentsBrowser.shared.messages.suffix(6))
        let size = CGSize(width: 360, height: 400)
        let f = UIGraphicsImageRendererFormat()
        f.scale = 2
        f.opaque = true
        let img = UIGraphicsImageRenderer(size: size, format: f).image { ctx in
            UIColor(red: 0.04, green: 0.05, blue: 0.09, alpha: 1).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
            let name: [NSAttributedString.Key: Any] = [.font: UIFont.systemFont(ofSize: 17, weight: .heavy),
                                                       .foregroundColor: UIColor(red: 0.62, green: 0.85, blue: 1, alpha: 1)]
            let body: [NSAttributedString.Key: Any] = [.font: UIFont.systemFont(ofSize: 17, weight: .semibold),
                                                       .foregroundColor: UIColor.white]
            var y = size.height - 12
            let lines: [NSAttributedString] = msgs.isEmpty
                ? [NSAttributedString(string: "Comments will appear here", attributes: body)]
                : msgs.map { m in
                    let s = NSMutableAttributedString(string: m.author.isEmpty ? "" : m.author + "  ", attributes: name)
                    s.append(NSAttributedString(string: m.text, attributes: body))
                    return s
                }
            for line in lines.reversed() {
                let r = line.boundingRect(with: CGSize(width: size.width - 44, height: 200),
                                          options: [.usesLineFragmentOrigin], context: nil)
                y -= ceil(r.height) + 14
                if y < 4 { break }
                UIColor(white: 1, alpha: 0.08).setFill()
                UIBezierPath(roundedRect: CGRect(x: 12, y: y - 2, width: ceil(r.width) + 20, height: ceil(r.height) + 12),
                             cornerRadius: 12).fill()
                line.draw(with: CGRect(x: 22, y: y + 4, width: size.width - 44, height: ceil(r.height)),
                          options: [.usesLineFragmentOrigin], context: nil)
            }
        }
        guard let sb = Self.sampleBuffer(img, format: &format) else { return }
        if displayLayer.status == .failed { displayLayer.flush() }
        displayLayer.enqueue(sb)
    }

    private static func sampleBuffer(_ img: UIImage, format: inout CMVideoFormatDescription?) -> CMSampleBuffer? {
        guard let cg = img.cgImage else { return nil }
        var px: CVPixelBuffer?
        CVPixelBufferCreate(nil, cg.width, cg.height, kCVPixelFormatType_32BGRA,
                            [kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()] as CFDictionary, &px)
        guard let p = px else { return nil }
        CVPixelBufferLockBaseAddress(p, [])
        if let c = CGContext(data: CVPixelBufferGetBaseAddress(p), width: cg.width, height: cg.height, bitsPerComponent: 8,
                             bytesPerRow: CVPixelBufferGetBytesPerRow(p), space: CGColorSpaceCreateDeviceRGB(),
                             bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) {
            c.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
        }
        CVPixelBufferUnlockBaseAddress(p, [])
        if format == nil || !CMVideoFormatDescriptionMatchesImageBuffer(format!, imageBuffer: p) {
            CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: p, formatDescriptionOut: &format)
        }
        guard let f = format else { return nil }
        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: CMClockGetTime(CMClockGetHostTimeClock()),
                                        decodeTimeStamp: .invalid)
        var sb: CMSampleBuffer?
        CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: p, formatDescription: f,
                                                 sampleTiming: &timing, sampleBufferOut: &sb)
        if let sb = sb, let arr = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: true),
           CFArrayGetCount(arr) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(arr, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(dict, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        return sb
    }

    // PiP delegates
    nonisolated func pictureInPictureControllerDidStartPictureInPicture(_ c: AVPictureInPictureController) {
        Task { @MainActor in self.active = true }
    }
    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ c: AVPictureInPictureController) {
        Task { @MainActor in self.active = false }
    }
    nonisolated func pictureInPictureController(_ c: AVPictureInPictureController, setPlaying playing: Bool) {}
    nonisolated func pictureInPictureControllerTimeRangeForPlayback(_ c: AVPictureInPictureController) -> CMTimeRange {
        CMTimeRange(start: .negativeInfinity, duration: .positiveInfinity)     // live: no seek bar
    }
    nonisolated func pictureInPictureControllerIsPlaybackPaused(_ c: AVPictureInPictureController) -> Bool { false }
    nonisolated func pictureInPictureController(_ c: AVPictureInPictureController,
                                                didTransitionToRenderSize newRenderSize: CMVideoDimensions) {}
    nonisolated func pictureInPictureController(_ c: AVPictureInPictureController, skipByInterval skipInterval: CMTime,
                                                completion completionHandler: @escaping () -> Void) { completionHandler() }
}

/// Puts the PiP display layer into the screen (PiP needs it there).
private struct PiPLayerHost: UIViewRepresentable {
    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        let layer = ChatPiP.shared.displayLayer
        layer.frame = CGRect(x: 0, y: 0, width: 2, height: 2)
        v.layer.addSublayer(layer)
        return v
    }
    func updateUIView(_ uiView: UIView, context: Context) {}
}

// MARK: - pill button for a live control (mic, privacy screen)

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
