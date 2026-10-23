import AppKit
import SwiftUI
import LoopXKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model: AppModel
    let notch: NotchController
    let status: StatusItemController
    let dashboard = DashboardWindowController()
    let hotkey = HotkeyManager()
    let settingsModel: SettingsModel
    private var settingsWindow: NSWindow?

    override init() {
        let settings = SettingsStore()
        model = AppModel(settings: settings)
        notch = NotchController(model: model)
        status = StatusItemController(model: model)
        settingsModel = SettingsModel(store: settings, login: LaunchAtLogin(service: SMAppServiceAdapter(.mainApp)))
        super.init()
        wire()
    }

    private func wire() {
        dashboard.baseURLProvider = { [weak self] in self?.model.lock?.baseURL }
        model.openDashboard = { [weak self] in self?.dashboard.show() }
        let previous = model.onNotchChange
        model.onNotchChange = { [weak self] old, new in
            previous(old, new)
            self?.status.update(new)
            self?.dashboard.backendChanged()
        }
        status.onOpenDashboard = { [weak self] in self?.dashboard.show() }
        status.onOpenSettings = { [weak self] in self?.showSettings() }
        status.onToggleHidden = { [weak self] in self?.notch.toggleHidden() }
        status.isHidden = { [weak self] in self?.notch.isHidden ?? false }
        status.onModeChange = { [weak self] m in self?.settingsModel.displayMode = m }
        settingsModel.onChange = { [weak self] in
            guard let self else { return }
            self.notch.refresh()
            self.hotkey.register(self.settingsModel.hotkey)
        }
        hotkey.onPress = { [weak self] in self?.notch.toggleHidden() }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        notch.refresh()
        hotkey.register(settingsModel.hotkey)
        model.start()
        // The takeover prompt is modal-ish: ask once, as soon as discovery finds an older backend.
        Task { @MainActor in
            for await _ in model.$takeoverOffer.values {
                guard let offer = model.takeoverOffer else { continue }
                askTakeover(offer)
            }
        }
        Task { @MainActor in
            for await _ in model.$lock.values { dashboard.backendChanged() }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        hotkey.unregister(); model.stop()   // the backend keeps running: it is its own LaunchAgent
    }

    private func askTakeover(_ offer: TakeoverOffer) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "A newer loopx backend is bundled"
        a.informativeText = "loopx \(offer.runningVersion) is already running, but this app includes \(offer.bundledVersion). Replace the running backend? Loops in progress will be interrupted."
        a.addButton(withTitle: "Replace")
        a.addButton(withTitle: "Keep Running Version")
        if a.runModal() == .alertFirstButtonReturn { model.acceptTakeover() } else { model.declineTakeover() }
    }

    func showSettings() {
        if settingsWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 430),
                             styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "LoopX Settings"
            w.contentView = NSHostingView(rootView: SettingsView(settings: settingsModel, app: model))
            w.isReleasedWhenClosed = false
            w.center()
            settingsWindow = w
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    /// `LOOPX_APP_SELFTEST=1`: build the whole UI without a backend, lay it out, report, exit.
    func selfTest() -> Bool {
        var lines: [String] = []
        let screen = ScreenInfo(id: 1, frame: CGRect(x: 0, y: 0, width: 1512, height: 982), safeAreaTop: 32,
                                auxiliaryTopLeft: CGRect(x: 0, y: 950, width: 663, height: 32),
                                auxiliaryTopRight: CGRect(x: 849, y: 950, width: 663, height: 32))
        notch.screensProvider = { [screen] in [screen] }
        notch.refresh()
        lines.append("panel=\(NSStringFromRect(notch.panel.frame))")
        notch.setExpanded(true)
        notch.host_layoutForSelfTest()
        lines.append("expanded=\(NSStringFromRect(notch.panel.frame))")
        status.update(NotchState.idle)
        let menuOK = status.item.menu != nil
        dashboard.ensureWindow()
        let webOK = dashboard.webView != nil
        let settingsHost = NSHostingView(rootView: SettingsView(settings: settingsModel, app: model))
        settingsHost.layoutSubtreeIfNeeded()
        let panelLevel = notch.panel.level.rawValue
        let all = notch.panel.collectionBehavior.contains(.canJoinAllSpaces) && notch.panel.collectionBehavior.contains(.fullScreenAuxiliary)
        lines.append("backend=\(model.backendConfig.starter is SMAppServiceStarter ? "bundled-agent" : "process") bundledVersion=\(model.backendConfig.discovery.bundledVersion)")
        lines.append("level=\(panelLevel) spaces=\(all) menu=\(menuOK) web=\(webOK) settings=\(settingsHost.fittingSize)")
        print("LoopX selftest ok: " + lines.joined(separator: " "))
        return menuOK && webOK && all
    }
}

extension NotchController {
    func host_layoutForSelfTest() { panel.contentView?.layoutSubtreeIfNeeded() }
}
