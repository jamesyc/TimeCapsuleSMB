import Foundation

enum DashboardSecondaryAction: String, Equatable, Hashable, Identifiable {
    case runCheckup
    case installUpdate
    case openFinder
    case replacePassword
    case viewCheckup
    case startSMB
    case advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .installUpdate:
            return L10n.string("dashboard.action.install_update_smb")
        case .openFinder:
            return L10n.string("dashboard.action.open_finder")
        case .replacePassword:
            return L10n.string("dashboard.action.replace_password")
        case .viewCheckup:
            return L10n.string("dashboard.action.view_checkup")
        case .startSMB:
            return L10n.string("dashboard.action.start_smb")
        case .advanced:
            return L10n.string("dashboard.action.advanced")
        }
    }

    var systemImage: String {
        switch self {
        case .runCheckup:
            return "stethoscope"
        case .installUpdate:
            return "square.and.arrow.up"
        case .openFinder:
            return "folder"
        case .replacePassword:
            return "key"
        case .viewCheckup:
            return "list.bullet.clipboard"
        case .startSMB:
            return "play.circle"
        case .advanced:
            return "gearshape"
        }
    }
}

struct DeviceDashboardHeaderPresentation: Equatable {
    let title: String
    let host: String
    let status: DeviceDisplayStatus
    let lastChecked: String
    let rows: [PresentationRow]

    init(summary: DeviceDashboardSummary) {
        let profile = summary.profile
        self.title = profile.title
        self.host = profile.host
        self.status = summary.displayStatus
        self.lastChecked = profile.lastCheckup
            .map { Self.formattedDate($0.checkedAt) }
            ?? L10n.string("value.never")
        self.rows = [
            PresentationRow(label: L10n.string("dashboard.overview.model"), value: profile.model ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("dashboard.overview.generation"), value: profile.deviceGeneration ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("dashboard.overview.payload"), value: profile.payloadFamily ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("dashboard.overview.password"), value: summary.passwordState.title),
            PresentationRow(label: L10n.string("dashboard.overview.last_install"), value: profile.lastDeploy?.summary ?? L10n.string("value.never"))
        ]
    }

    private static func formattedDate(_ date: Date) -> String {
        DateFormatter.localizedString(from: date, dateStyle: .medium, timeStyle: .short)
    }
}

enum DashboardHealthDomain: String, CaseIterable, Equatable, Identifiable {
    case connection
    case runtime
    case finderBonjour
    case smbAuth
    case timeMachine

    var id: String { rawValue }

    var title: String {
        switch self {
        case .connection:
            return L10n.string("dashboard.health.connection")
        case .runtime:
            return L10n.string("dashboard.health.runtime")
        case .finderBonjour:
            return L10n.string("dashboard.health.finder_bonjour")
        case .smbAuth:
            return L10n.string("dashboard.health.smb_auth")
        case .timeMachine:
            return L10n.string("dashboard.health.time_machine")
        }
    }

    fileprivate var checkupDomains: Set<String> {
        switch self {
        case .connection:
            return ["connection", "device", "ssh"]
        case .runtime:
            return ["runtime", "process", "service"]
        case .finderBonjour:
            return ["bonjour", "finder", "advertising"]
        case .smbAuth:
            return ["smb", "smb auth", "auth"]
        case .timeMachine:
            return ["time machine", "timemachine"]
        }
    }
}

enum DashboardHealthStatus: String, Equatable {
    case unknown
    case good
    case warning
    case failed
    case running

    var title: String {
        switch self {
        case .unknown:
            return L10n.string("dashboard.health.status.unknown")
        case .good:
            return L10n.string("dashboard.health.status.good")
        case .warning:
            return L10n.string("dashboard.health.status.warning")
        case .failed:
            return L10n.string("dashboard.health.status.failed")
        case .running:
            return L10n.string("dashboard.health.status.running")
        }
    }

    var systemImage: String {
        switch self {
        case .unknown:
            return "questionmark.circle"
        case .good:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.triangle"
        case .failed:
            return "xmark.octagon"
        case .running:
            return "progress.indicator"
        }
    }
}

struct DashboardHealthRow: Equatable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let status: DashboardHealthStatus
    let action: DashboardSecondaryAction?

    init(
        id: String,
        title: String,
        detail: String,
        status: DashboardHealthStatus,
        action: DashboardSecondaryAction? = nil
    ) {
        self.id = id
        self.title = title
        self.detail = detail
        self.status = status
        self.action = action
    }
}

struct DashboardHealthSection: Equatable, Identifiable {
    let domain: DashboardHealthDomain
    let rows: [DashboardHealthRow]

    var id: String { domain.rawValue }
    var title: String { domain.title }
}

struct DeviceDashboardOverviewPresentation: Equatable {
    let header: DeviceDashboardHeaderPresentation
    let primaryAction: DashboardPrimaryAction
    let secondaryActions: [DashboardSecondaryAction]
    let healthSections: [DashboardHealthSection]
    let hostWarning: HostCompatibilityWarning?
    let requiresPasswordReplacement: Bool

    init(summary: DeviceDashboardSummary, currentCheckupSummary: DoctorSummary? = nil) {
        self.header = DeviceDashboardHeaderPresentation(summary: summary)
        self.primaryAction = summary.primaryAction
        self.secondaryActions = Self.secondaryActions(for: summary)
        self.healthSections = Self.healthSections(for: summary, currentCheckupSummary: currentCheckupSummary)
        self.hostWarning = summary.hostWarning
        self.requiresPasswordReplacement = Self.requiresPasswordReplacement(summary.passwordState)
    }

    private static func secondaryActions(for summary: DeviceDashboardSummary) -> [DashboardSecondaryAction] {
        var actions: [DashboardSecondaryAction] = []
        switch summary.primaryAction {
        case .replacePassword:
            actions.append(.runCheckup)
        case .runCheckup:
            actions.append(.installUpdate)
        case .installSMB:
            actions.append(.runCheckup)
        case .viewCheckup:
            actions.append(.runCheckup)
        case .openSMB:
            actions.append(.runCheckup)
        }
        if summary.profile.lastDeploy != nil && summary.primaryAction != .openSMB {
            actions.append(.openFinder)
        }
        if !requiresPasswordReplacement(summary.passwordState) {
            actions.append(.replacePassword)
        }
        actions.append(.advanced)
        return actions.removingDuplicates()
    }

    private static func requiresPasswordReplacement(_ passwordState: DevicePasswordState) -> Bool {
        switch passwordState {
        case .unknown, .missing, .invalid, .keychainUnavailable:
            return true
        case .available:
            return false
        }
    }

    private static func healthSections(
        for summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?
    ) -> [DashboardHealthSection] {
        [
            DashboardHealthSection(domain: .connection, rows: [connectionRow(for: summary)]),
            DashboardHealthSection(domain: .runtime, rows: [runtimeRow(for: summary, currentCheckupSummary: currentCheckupSummary)]),
            DashboardHealthSection(domain: .finderBonjour, rows: [
                domainRow(domain: .finderBonjour, summary: summary, currentCheckupSummary: currentCheckupSummary)
            ]),
            DashboardHealthSection(domain: .smbAuth, rows: [
                domainRow(domain: .smbAuth, summary: summary, currentCheckupSummary: currentCheckupSummary)
            ]),
            DashboardHealthSection(domain: .timeMachine, rows: [
                timeMachineRow(for: summary, currentCheckupSummary: currentCheckupSummary)
            ])
        ]
    }

    private static func connectionRow(for summary: DeviceDashboardSummary) -> DashboardHealthRow {
        switch summary.displayStatus {
        case .checking, .installing, .maintaining:
            return DashboardHealthRow(
                id: "connection-running",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.running"),
                status: .running,
                action: .viewCheckup
            )
        default:
            break
        }

        switch summary.passwordState {
        case .available:
            return DashboardHealthRow(
                id: "connection-password-available",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.password_available"),
                status: .good
            )
        case .unknown, .missing:
            return DashboardHealthRow(
                id: "connection-password-missing",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.password_missing"),
                status: .warning,
                action: .replacePassword
            )
        case .invalid:
            return DashboardHealthRow(
                id: "connection-password-invalid",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.password_invalid"),
                status: .failed,
                action: .replacePassword
            )
        case .keychainUnavailable:
            return DashboardHealthRow(
                id: "connection-keychain-unavailable",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.keychain_unavailable"),
                status: .failed,
                action: .replacePassword
            )
        }
    }

    private static func runtimeRow(
        for summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?
    ) -> DashboardHealthRow {
        if summary.displayStatus == .installing {
            return DashboardHealthRow(
                id: "runtime-installing",
                title: DashboardHealthDomain.runtime.title,
                detail: L10n.string("dashboard.health.runtime.installing"),
                status: .running
            )
        }
        if summary.displayStatus == .activationNeeded {
            return DashboardHealthRow(
                id: "runtime-activation-needed",
                title: DashboardHealthDomain.runtime.title,
                detail: L10n.string("dashboard.health.runtime.activation_needed"),
                status: .warning,
                action: .startSMB
            )
        }
        if let signal = checkupSignal(for: .runtime, summary: currentCheckupSummary) {
            return DashboardHealthRow(
                id: "runtime-checkup",
                title: DashboardHealthDomain.runtime.title,
                detail: signal.detail,
                status: signal.status,
                action: signal.status == .good ? nil : .viewCheckup
            )
        }
        guard let lastDeploy = summary.profile.lastDeploy else {
            return DashboardHealthRow(
                id: "runtime-not-installed",
                title: DashboardHealthDomain.runtime.title,
                detail: L10n.string("dashboard.health.runtime.not_installed"),
                status: .warning,
                action: .installUpdate
            )
        }
        if lastDeploy.verified == true {
            return DashboardHealthRow(
                id: "runtime-installed",
                title: DashboardHealthDomain.runtime.title,
                detail: lastDeploy.summary,
                status: .good,
                action: .openFinder
            )
        }
        return DashboardHealthRow(
            id: "runtime-installed-unverified",
            title: DashboardHealthDomain.runtime.title,
            detail: lastDeploy.summary,
            status: .warning,
            action: .runCheckup
        )
    }

    private static func domainRow(
        domain: DashboardHealthDomain,
        summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?
    ) -> DashboardHealthRow {
        if let signal = checkupSignal(for: domain, summary: currentCheckupSummary) {
            return DashboardHealthRow(
                id: "\(domain.rawValue)-current-checkup",
                title: domain.title,
                detail: signal.detail,
                status: signal.status,
                action: signal.status == .good ? nil : .viewCheckup
            )
        }
        guard let lastCheckup = summary.profile.lastCheckup else {
            return DashboardHealthRow(
                id: "\(domain.rawValue)-unchecked",
                title: domain.title,
                detail: L10n.string("dashboard.health.unchecked"),
                status: .unknown,
                action: .runCheckup
            )
        }
        return DashboardHealthRow(
            id: "\(domain.rawValue)-snapshot",
            title: domain.title,
            detail: lastCheckup.summary,
            status: snapshotStatus(lastCheckup),
            action: snapshotStatus(lastCheckup) == .good ? nil : .viewCheckup
        )
    }

    private static func timeMachineRow(
        for summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?
    ) -> DashboardHealthRow {
        if let hostWarning = summary.hostWarning {
            return DashboardHealthRow(
                id: "time-machine-host-warning",
                title: DashboardHealthDomain.timeMachine.title,
                detail: hostWarning.message,
                status: .warning
            )
        }
        return domainRow(domain: .timeMachine, summary: summary, currentCheckupSummary: currentCheckupSummary)
    }

    private static func checkupSignal(
        for domain: DashboardHealthDomain,
        summary: DoctorSummary?
    ) -> (status: DashboardHealthStatus, detail: String)? {
        guard let summary else {
            return nil
        }
        let groups = summary.groups.filter { group in
            domain.checkupDomains.contains(group.domain.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
        }
        guard !groups.isEmpty else {
            return nil
        }
        let checks = groups.flatMap(\.checks)
        let passCount = checks.filter { $0.status == "PASS" }.count
        let warnCount = checks.filter { $0.status == "WARN" }.count
        let failCount = checks.filter { $0.status == "FAIL" }.count
        let status: DashboardHealthStatus
        if failCount > 0 {
            status = .failed
        } else if warnCount > 0 {
            status = .warning
        } else if passCount > 0 {
            status = .good
        } else {
            status = .unknown
        }
        return (
            status: status,
            detail: L10n.format("dashboard.health.check_counts", passCount, warnCount, failCount)
        )
    }

    private static func snapshotStatus(_ snapshot: DeviceCheckupSnapshot) -> DashboardHealthStatus {
        if snapshot.failCount > 0 || snapshot.state == .failed || snapshot.state == .runFailed {
            return .failed
        }
        if snapshot.warnCount > 0 || snapshot.state == .warning {
            return .warning
        }
        if snapshot.passCount > 0 || snapshot.state == .passed {
            return .good
        }
        return .unknown
    }
}

private extension Array where Element: Hashable {
    func removingDuplicates() -> [Element] {
        var seen = Set<Element>()
        return filter { seen.insert($0).inserted }
    }
}
