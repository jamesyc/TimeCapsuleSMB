// swift-tools-version: 5.9

import Foundation
import PackageDescription

let developerDir = ProcessInfo.processInfo.environment["DEVELOPER_DIR"] ?? "/Applications/Xcode.app/Contents/Developer"
let xcodeFrameworkPath = "\(developerDir)/Platforms/MacOSX.platform/Developer/Library/Frameworks"
let xcodeFrameworkFlags = FileManager.default.fileExists(atPath: xcodeFrameworkPath)
    ? ["-F", xcodeFrameworkPath]
    : []
let xcodeSwiftSettings: [SwiftSetting] = xcodeFrameworkFlags.isEmpty ? [] : [.unsafeFlags(xcodeFrameworkFlags)]
let xcodeLinkerSettings: [LinkerSetting] = xcodeFrameworkFlags.isEmpty ? [] : [.unsafeFlags(xcodeFrameworkFlags)]

let package = Package(
    name: "TimeCapsuleSMBMac",
    defaultLocalization: "en",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "TimeCapsuleSMB", targets: ["TimeCapsuleSMBExecutable"]),
        .executable(name: "tcapsule", targets: ["TimeCapsuleSMBHelper"])
    ],
    targets: [
        .target(
            name: "TimeCapsuleSMBApp",
            path: "Sources/TimeCapsuleSMBApp",
            resources: [.process("Resources")]
        ),
        .executableTarget(
            name: "TimeCapsuleSMBExecutable",
            dependencies: ["TimeCapsuleSMBApp"],
            path: "Sources/TimeCapsuleSMBExecutable"
        ),
        .executableTarget(
            name: "TimeCapsuleSMBHelper",
            path: "Sources/TimeCapsuleSMBHelper"
        ),
        .testTarget(
            name: "TimeCapsuleSMBAppTests",
            dependencies: ["TimeCapsuleSMBApp"],
            path: "Tests/TimeCapsuleSMBAppTests",
            swiftSettings: xcodeSwiftSettings,
            linkerSettings: xcodeLinkerSettings
        )
    ]
)
