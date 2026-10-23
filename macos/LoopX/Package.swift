// swift-tools-version:5.9
import Foundation
import PackageDescription

// Command Line Tools ship swift-testing's macro plugin outside the default plugin
// search path, so `swift test` cannot expand @Test/@Suite without a hint. Full Xcode
// toolchains find it on their own, in which case this flag list stays empty.
let cltTestingPlugins = "/Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing"
let testSwiftSettings: [SwiftSetting] =
    FileManager.default.fileExists(atPath: cltTestingPlugins + "/libTestingMacros.dylib")
    ? [.unsafeFlags(["-plugin-path", cltTestingPlugins])] : []

let package = Package(
    name: "LoopX",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "LoopXKit", targets: ["LoopXKit"]),
        .executable(name: "LoopX", targets: ["LoopX"]),
    ],
    targets: [
        .target(name: "LoopXKit"),
        .executableTarget(name: "LoopX", dependencies: ["LoopXKit"]),
        .testTarget(name: "LoopXKitTests", dependencies: ["LoopXKit"], swiftSettings: testSwiftSettings),
    ]
)
