import Foundation

// The backend tags every check with details["domain"]: "computer" for this
// Mac's own setup and its view of the device over the network, "device" for
// what it measured on the device over SSH. An untagged check is the device's.
enum DoctorCheckDomain: String, CaseIterable, Equatable, Hashable, Identifiable {
    case device
    case thisMac

    var id: String { rawValue }

    var title: String {
        switch self {
        case .device:
            return L10n.string("doctor.domain.device")
        case .thisMac:
            return L10n.string("doctor.domain.this_mac")
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
        return normalized == "computer" ? .thisMac : .device
    }

    static func domain(for check: DoctorCheckPayload) -> DoctorCheckDomain {
        domain(for: check.details.stringValue(for: "domain"))
    }

    static func signals(from checks: [DoctorCheckPayload]) -> [DoctorDomainSignal] {
        let grouped = Dictionary(grouping: checks, by: domain(for:))
        return grouped
            .map { signal(domain: $0.key, checks: $0.value) }
            .sorted { left, right in
                left.severity == right.severity
                    ? left.domain.title < right.domain.title
                    : left.severity < right.severity
            }
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
