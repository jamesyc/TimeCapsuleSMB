import Foundation

struct HostCompatibilityWarning: Equatable {
    let title: String
    let message: String
}

enum HostCompatibilityPolicy {
    static func warning(for version: OperatingSystemVersion = ProcessInfo.processInfo.operatingSystemVersion) -> HostCompatibilityWarning? {
        guard version.majorVersion == 15 || version.majorVersion == 26 else {
            return nil
        }
        if version.majorVersion == 15 && version.minorVersion == 7 && [5, 6, 7].contains(version.patchVersion) {
            return timeMachineWarning(version: version)
        }
        if version.majorVersion == 26 && version.minorVersion == 4 {
            return timeMachineWarning(version: version)
        }
        return nil
    }

    private static func timeMachineWarning(version: OperatingSystemVersion) -> HostCompatibilityWarning {
        HostCompatibilityWarning(
            title: "macOS Time Machine Warning",
            message: "macOS \(version.majorVersion).\(version.minorVersion).\(version.patchVersion) has known Time Machine network backup issues. SMB may work, but backup reliability can be affected by the host OS."
        )
    }
}
