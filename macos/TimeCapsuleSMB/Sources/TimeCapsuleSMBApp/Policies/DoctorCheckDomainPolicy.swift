import Foundation

enum DoctorCheckDomain: String, CaseIterable, Equatable, Hashable, Identifiable {
    case connection
    case runtime
    case finderBonjour
    case smbAuth
    case timeMachine
    case disk
    case metadata
    case general

    var id: String { rawValue }

    var title: String {
        switch self {
        case .connection:
            return L10n.string("doctor.domain.connection")
        case .runtime:
            return L10n.string("doctor.domain.runtime")
        case .finderBonjour:
            return L10n.string("doctor.domain.finder_bonjour")
        case .smbAuth:
            return L10n.string("doctor.domain.smb_auth")
        case .timeMachine:
            return L10n.string("doctor.domain.time_machine")
        case .disk:
            return L10n.string("doctor.domain.disk")
        case .metadata:
            return L10n.string("doctor.domain.metadata")
        case .general:
            return L10n.string("doctor.domain.general")
        }
    }
}

enum DoctorCheckSeverity: Int, Equatable, Comparable {
    case failed = 0
    case warning = 1
    case passed = 2
    case unknown = 3

    static func < (left: DoctorCheckSeverity, right: DoctorCheckSeverity) -> Bool {
        left.rawValue < right.rawValue
    }
}

struct DoctorDomainSignal: Equatable {
    let domain: DoctorCheckDomain
    let checks: [DoctorCheckPayload]
    let passCount: Int
    let warnCount: Int
    let failCount: Int
    let infoCount: Int

    var severity: DoctorCheckSeverity {
        if failCount > 0 {
            return .failed
        }
        if warnCount > 0 {
            return .warning
        }
        if passCount > 0 {
            return .passed
        }
        return .unknown
    }

    var countSummary: String {
        L10n.format("dashboard.health.check_counts", passCount, warnCount, failCount)
    }
}

enum DoctorCheckDomainPolicy {
    static func domain(for rawDomain: String?) -> DoctorCheckDomain {
        let normalized = rawDomain?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() ?? ""
        switch normalized {
        case "connection", "device", "ssh":
            return .connection
        case "runtime", "process", "service":
            return .runtime
        case "bonjour", "finder", "advertising", "discovery":
            return .finderBonjour
        case "smb", "smb auth", "auth":
            return .smbAuth
        case "time machine", "timemachine":
            return .timeMachine
        case "disk", "storage", "volume", "fsck":
            return .disk
        case "metadata", "xattrs", "xattr", "repair-xattrs":
            return .metadata
        default:
            return .general
        }
    }

    static func domain(for check: DoctorCheckPayload) -> DoctorCheckDomain {
        domain(for: check.details.stringValue(for: "domain"))
    }

    static func signals(from summary: DoctorSummary) -> [DoctorDomainSignal] {
        let grouped = Dictionary(grouping: summary.groups.flatMap(\.checks), by: domain(for:))
        return grouped
            .map { signal(domain: $0.key, checks: $0.value) }
            .sorted { left, right in
                left.severity == right.severity
                    ? left.domain.title < right.domain.title
                    : left.severity < right.severity
            }
    }

    static func signal(for domain: DoctorCheckDomain, summary: DoctorSummary?) -> DoctorDomainSignal? {
        guard let summary else {
            return nil
        }
        let checks = summary.groups
            .flatMap(\.checks)
            .filter { self.domain(for: $0) == domain }
        guard !checks.isEmpty else {
            return nil
        }
        return signal(domain: domain, checks: checks)
    }

    private static func signal(domain: DoctorCheckDomain, checks: [DoctorCheckPayload]) -> DoctorDomainSignal {
        DoctorDomainSignal(
            domain: domain,
            checks: checks,
            passCount: checks.filter { normalizedStatus($0.status) == "PASS" }.count,
            warnCount: checks.filter { normalizedStatus($0.status) == "WARN" }.count,
            failCount: checks.filter { normalizedStatus($0.status) == "FAIL" }.count,
            infoCount: checks.filter { normalizedStatus($0.status) == "INFO" }.count
        )
    }

    private static func normalizedStatus(_ status: String) -> String {
        status.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
    }
}
