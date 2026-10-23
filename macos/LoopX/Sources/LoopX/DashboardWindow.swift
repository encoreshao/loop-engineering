import AppKit
import WebKit
import LoopXKit

/// The dashboard: a plain WKWebView on the backend's own pages. Same server a browser tab would use,
/// so having both open at once is fine.
@MainActor
final class DashboardWindowController: NSObject, WKNavigationDelegate, WKUIDelegate {
    private(set) var window: NSWindow?
    private(set) var webView: WKWebView?
    private var loadedBase: URL?
    var baseURLProvider: () -> URL? = { nil }

    func show() {
        ensureWindow()
        guard let base = baseURLProvider() else {
            webView?.loadHTMLString(Self.placeholder("The loopx backend is not running yet."), baseURL: nil)
            loadedBase = nil
            present(); return
        }
        if loadedBase?.port != base.port {
            webView?.load(URLRequest(url: base)); loadedBase = base
        }
        present()
    }

    private func present() {
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func ensureWindow() {
        guard window == nil else { return }
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.navigationDelegate = self
        wv.uiDelegate = self
        wv.allowsBackForwardNavigationGestures = true
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = "LoopX"
        w.contentView = wv
        w.isReleasedWhenClosed = false
        w.setFrameAutosaveName("LoopXDashboard")
        w.center()
        window = w; webView = wv
    }

    /// Reload after the backend moved to another port.
    func backendChanged() {
        guard window != nil, let base = baseURLProvider(), loadedBase?.port != base.port else { return }
        webView?.load(URLRequest(url: base)); loadedBase = base
    }

    // Only the local backend loads in-window; every other link goes to the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = action.request.url
        let local = url?.host == "127.0.0.1" || url?.host == "localhost" || url?.scheme == "about"
        if local { decisionHandler(.allow); return }
        if let url, ["http", "https"].contains(url.scheme?.lowercased()) { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                             for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https"].contains(url.scheme?.lowercased()) { NSWorkspace.shared.open(url) }
        return nil
    }

    static func placeholder(_ text: String) -> String {
        "<html><body style='font:14px -apple-system;color:#888;display:flex;align-items:center;justify-content:center;height:100vh'>\(text)</body></html>"
    }
}
