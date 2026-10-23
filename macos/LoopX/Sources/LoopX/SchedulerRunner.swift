import Foundation
import LoopXKit

/// The app-driven scheduler: while "Run scheduled loops" is on, `loopx scheduler` runs on launch and
/// every 15 minutes. It stands down when the installed launchd job (`com.hermes.loop-engineering`)
/// is loaded, so a due loop is never started twice.
@MainActor
final class SchedulerRunner {
    nonisolated static let legacyLabel = "com.hermes.loop-engineering"
    private let binary: URL?
    private let environment: [String: String]
    private lazy var poller = SchedulerPoller(poll: { [binary, environment] in
        guard let binary, !Self.legacyJobLoaded() else { return }
        await Self.runOnce(binary, environment)
    })

    init(binary: URL?, environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.binary = binary; self.environment = environment
    }

    var legacyJobActive: Bool { Self.legacyJobLoaded() }

    func setEnabled(_ on: Bool) { on ? poller.start() : poller.stop() }

    nonisolated static func legacyJobLoaded() -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["print", "gui/\(getuid())/\(legacyLabel)"]
        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    nonisolated static func runOnce(_ binary: URL, _ environment: [String: String]) async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            let p = Process()
            p.executableURL = binary
            p.arguments = ["scheduler"]
            var env = environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            p.standardInput = FileHandle.nullDevice
            p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
            p.terminationHandler = { _ in cont.resume() }
            do { try p.run() } catch { cont.resume() }
        }
    }
}
