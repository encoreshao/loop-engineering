import SwiftUI
import AppKit
import LoopXKit

@MainActor
final class SettingsModel: ObservableObject {
    @Published var displayMode: DisplayMode { didSet { store.displayMode = displayMode; onChange() } }
    @Published var hotkey: HotkeyPreset { didSet { store.hotkey = hotkey; onChange() } }
    @Published var launchAtLogin: Bool
    @Published var loginNeedsApproval: Bool
    @Published var error: String?
    private let store: SettingsStore
    private let login: LaunchAtLogin
    var onChange: () -> Void = {}

    init(store: SettingsStore, login: LaunchAtLogin) {
        self.store = store; self.login = login
        displayMode = store.displayMode; hotkey = store.hotkey
        launchAtLogin = login.isEnabled; loginNeedsApproval = login.needsApproval
    }

    func setLaunchAtLogin(_ on: Bool) {
        do { try login.setEnabled(on); error = nil }
        catch { self.error = error.localizedDescription }
        launchAtLogin = login.isEnabled; loginNeedsApproval = login.needsApproval
    }
}

struct SettingsView: View {
    @ObservedObject var settings: SettingsModel
    @ObservedObject var app: AppModel

    var body: some View {
        Form {
            Section("General") {
                Toggle("Launch LoopX at login", isOn: Binding(get: { settings.launchAtLogin }, set: settings.setLaunchAtLogin))
                if settings.loginNeedsApproval {
                    Text("Approve LoopX in System Settings > General > Login Items.").font(.caption).foregroundStyle(.orange)
                }
                if let e = settings.error { Text(e).font(.caption).foregroundStyle(.red) }
            }
            Section("Display") {
                Picker("Show status as", selection: $settings.displayMode) {
                    Text("Notch").tag(DisplayMode.notch)
                    Text("Floating HUD").tag(DisplayMode.hud)
                    Text("Menu bar only").tag(DisplayMode.off)
                }
                Text("Notch pins to the camera housing and falls back to a draggable HUD on screens without one.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Hide / show hotkey", selection: $settings.hotkey) {
                    ForEach(HotkeyPreset.presets) { Text($0.label).tag($0) }
                    Text("None").tag(HotkeyPreset.none)
                }
            }
            Section("Backend") {
                LabeledContent("Status", value: app.connection.text)
                if let l = app.lock { LabeledContent("Address", value: "127.0.0.1:\(l.port)  (v\(l.version), pid \(l.pid))") }
                if let offer = app.takeoverOffer {
                    Text("A backend v\(offer.runningVersion) is running; this app bundles v\(offer.bundledVersion).").font(.caption)
                    HStack {
                        Button("Replace it") { app.acceptTakeover() }
                        Button("Keep it") { app.declineTakeover() }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 440, height: 430)
    }
}
