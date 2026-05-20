// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "TimeCapsuleSMBMac",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "TimeCapsuleSMB", targets: ["TimeCapsuleSMBApp"])
    ],
    targets: [
        .executableTarget(name: "TimeCapsuleSMBApp")
    ]
)

