import Foundation

struct HostCompatibilityWarning: Equatable {
    let title: String
    let message: String
}

private struct KnownHostCompatibilityIssue {
    let majorVersion: Int
    let minorVersion: Int
    let patchVersions: Set<Int>?

    func matches(_ version: OperatingSystemVersion) -> Bool {
        guard version.majorVersion == majorVersion, version.minorVersion == minorVersion else {
            return false
        }
        guard let patchVersions else {
            return true
        }
        return patchVersions.contains(version.patchVersion)
    }
}

enum HostCompatibilityPolicy {
    // Product guidance tracks macOS 26.4.x separately from the 15.7 patch band.
    private static let knownTimeMachineIssues = [
        KnownHostCompatibilityIssue(majorVersion: 15, minorVersion: 7, patchVersions: [5, 6, 7]),
        KnownHostCompatibilityIssue(majorVersion: 26, minorVersion: 4, patchVersions: nil)
    ]

    static func warning(
        enabled: Bool = true,
        for version: OperatingSystemVersion = ProcessInfo.processInfo.operatingSystemVersion
    ) -> HostCompatibilityWarning? {
        guard enabled else {
            return nil
        }
        guard knownTimeMachineIssues.contains(where: { $0.matches(version) }) else {
            return nil
        }
        return timeMachineWarning(version: version)
    }

    private static func timeMachineWarning(version: OperatingSystemVersion) -> HostCompatibilityWarning {
        HostCompatibilityWarning(
            title: L10n.string("host_warning.time_machine.title"),
            message: L10n.format(
                "host_warning.time_machine.message",
                version.majorVersion,
                version.minorVersion,
                version.patchVersion
            )
        )
    }
}
