import Foundation

enum DashboardSecondaryAction: String, CaseIterable, Equatable, Hashable, Identifiable {
    case refreshStatus
    case runCheckup
    case installUpdate
    case openFinder
    case replacePassword
    case viewCheckup
    case startSMB
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .refreshStatus:
            return L10n.string("dashboard.action.refresh_status")
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
        case .settings:
            return L10n.string("dashboard.action.settings")
        }
    }

    var systemImage: String {
        switch self {
        case .refreshStatus:
            return "arrow.clockwise"
        case .runCheckup:
            return "stethoscope"
        case .installUpdate:
            return "square.and.arrow.down.on.square"
        case .openFinder:
            return "folder"
        case .replacePassword:
            return "key"
        case .viewCheckup:
            return "list.bullet.clipboard"
        case .startSMB:
            return "play.circle"
        case .settings:
            return "gearshape"
        }
    }
}

struct DeviceDashboardHeaderPresentation: Equatable {
    let title: String
    let connectionTarget: String
    let addressSummary: String
    let status: DeviceDisplayStatus
    let lastChecked: String
    let rows: [PresentationRow]

    init(summary: DeviceDashboardSummary) {
        let profile = summary.profile
        self.title = profile.title
        self.connectionTarget = profile.displayTarget
        self.addressSummary = profile.addressSummary
        self.status = summary.displayStatus
        let lastCheckedValue = profile.lastCheckup
            .map { Self.formattedDate($0.checkedAt) }
            ?? L10n.string("value.never")
        self.lastChecked = L10n.format("dashboard.header.last_checked_value", lastCheckedValue)
        self.rows = [
            PresentationRow(label: L10n.string("dashboard.overview.connection_target"), value: profile.connectionTarget),
            PresentationRow(label: L10n.string("dashboard.overview.addresses"), value: profile.addressSummary.isEmpty ? L10n.string("value.unknown") : profile.addressSummary),
            PresentationRow(label: L10n.string("dashboard.overview.model"), value: profile.model ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("dashboard.overview.generation"), value: Self.generationValue(for: profile)),
            PresentationRow(label: L10n.string("dashboard.overview.payload"), value: profile.payloadFamily ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("dashboard.overview.password"), value: summary.passwordState.title),
            PresentationRow(label: L10n.string("dashboard.overview.last_install"), value: Self.lastInstallValue(for: profile))
        ]
    }

    private static func lastInstallValue(for profile: DeviceProfile) -> String {
        profile.lastDeployState?.localizedSummary ?? L10n.string("value.never")
    }

    private static func generationValue(for profile: DeviceProfile) -> String {
        if let syapGeneration = generationFromSyAP(profile.syap) {
            return syapGeneration
        }
        if let modelGeneration = generationFromModel(profile.model) {
            return modelGeneration
        }
        if let coarseGeneration = generationFromCoarseValue(profile.deviceGeneration) {
            return coarseGeneration
        }
        return L10n.string("value.unknown")
    }

    private static func generationFromSyAP(_ syap: String?) -> String? {
        guard let syap = syap?.trimmingCharacters(in: .whitespacesAndNewlines), !syap.isEmpty else {
            return nil
        }
        return [
            "104": generationLabel(1),
            "105": generationLabel(2),
            "106": generationLabel(1),
            "108": generationLabel(3),
            "109": generationLabel(2),
            "113": generationLabel(3),
            "114": generationLabel(4),
            "116": generationLabel(4),
            "117": generationLabel(5),
            "119": generationLabel(5),
            "120": generationLabel(6)
        ][syap]
    }

    private static func generationFromModel(_ model: String?) -> String? {
        guard let model else {
            return nil
        }
        let pattern = #"([0-9]+)(?:st|nd|rd|th) generation"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else {
            return nil
        }
        let range = NSRange(model.startIndex..<model.endIndex, in: model)
        guard let match = regex.firstMatch(in: model, range: range),
              let matchRange = Range(match.range(at: 1), in: model),
              let generation = Int(model[matchRange]) else {
            return nil
        }
        return generationLabel(generation)
    }

    private static func generationFromCoarseValue(_ value: String?) -> String? {
        let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch normalized {
        case "gen1", "tc_gen1":
            return generationLabel(1)
        case "gen2", "tc_gen2":
            return generationLabel(2)
        case "gen3", "tc_gen3":
            return generationLabel(3)
        case "gen4", "tc_gen4":
            return generationLabel(4)
        case "gen5", "tc_gen5":
            return generationLabel(5)
        case "gen6", "tc_gen6":
            return generationLabel(6)
        default:
            return nil
        }
    }

    private static func generationLabel(_ generation: Int) -> String {
        L10n.string("dashboard.generation.\(generation)")
    }

    private static func formattedDate(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = L10n.currentLanguage.locale
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: date)
    }
}

enum DashboardHealthDomain: String, CaseIterable, Equatable, Identifiable {
    case connection
    case runtime
    case checkup

    var id: String { rawValue }

    var title: String {
        switch self {
        case .connection:
            return L10n.string("dashboard.health.connection")
        case .runtime:
            return L10n.string("dashboard.health.runtime")
        case .checkup:
            return L10n.string("dashboard.health.checkup")
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
    let isPrimaryActionEnabled: Bool
    let secondaryActions: [DashboardSecondaryAction]
    let disabledSecondaryActions: Set<DashboardSecondaryAction>
    let healthSections: [DashboardHealthSection]
    let hostWarning: HostCompatibilityWarning?
    let requiresPasswordReplacement: Bool

    init(
        summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary? = nil,
        reachabilitySnapshot: DeviceReachabilitySnapshot? = nil,
        isReachabilityRunning: Bool = false
    ) {
        let secondaryActions = DashboardActionPolicy.secondaryActions(for: summary)
        self.header = DeviceDashboardHeaderPresentation(summary: summary)
        self.primaryAction = summary.primaryAction
        self.isPrimaryActionEnabled = DashboardActionPolicy.isEnabled(summary.primaryAction, for: summary)
            && !(isReachabilityRunning && summary.primaryAction.isMutatingOverviewAction)
        self.secondaryActions = secondaryActions
        self.disabledSecondaryActions = Set(DashboardSecondaryAction.allCases.filter {
            !DashboardActionPolicy.isEnabled($0, for: summary)
                || (isReachabilityRunning && $0.isMutatingOverviewAction)
        })
        self.healthSections = Self.healthSections(
            for: summary,
            currentCheckupSummary: currentCheckupSummary,
            reachabilitySnapshot: reachabilitySnapshot,
            isReachabilityRunning: isReachabilityRunning
        )
        self.hostWarning = summary.hostWarning
        self.requiresPasswordReplacement = DashboardActionPolicy.requiresPasswordReplacement(summary.passwordState)
    }

    func isEnabled(_ action: DashboardSecondaryAction) -> Bool {
        !disabledSecondaryActions.contains(action)
    }

    private static func healthSections(
        for summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?,
        reachabilitySnapshot: DeviceReachabilitySnapshot?,
        isReachabilityRunning: Bool
    ) -> [DashboardHealthSection] {
        [
            DashboardHealthSection(domain: .connection, rows: [
                connectionRow(
                    for: summary,
                    reachabilitySnapshot: reachabilitySnapshot,
                    isReachabilityRunning: isReachabilityRunning
                )
            ]),
            DashboardHealthSection(domain: .runtime, rows: [runtimeRow(for: summary, currentCheckupSummary: currentCheckupSummary)]),
            DashboardHealthSection(domain: .checkup, rows: [
                checkupRow(summary: summary, currentCheckupSummary: currentCheckupSummary)
            ])
        ]
    }

    private static func connectionRow(
        for summary: DeviceDashboardSummary,
        reachabilitySnapshot: DeviceReachabilitySnapshot?,
        isReachabilityRunning: Bool
    ) -> DashboardHealthRow {
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

        if isReachabilityRunning {
            return DashboardHealthRow(
                id: "connection-refreshing",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.refreshing"),
                status: .running
            )
        }

        switch summary.passwordState {
        case .invalid:
            return passwordIssueRow(id: "connection-password-invalid", detailKey: "dashboard.health.connection.password_invalid")
        case .keychainUnavailable:
            return passwordIssueRow(id: "connection-keychain-unavailable", detailKey: "dashboard.health.connection.keychain_unavailable")
        case .available, .unknown, .missing:
            break
        }

        if let reachabilitySnapshot {
            return reachabilityRow(from: reachabilitySnapshot)
        }

        switch summary.passwordState {
        case .available, .unknown, .missing:
            return DashboardHealthRow(
                id: "connection-not-refreshed",
                title: DashboardHealthDomain.connection.title,
                detail: L10n.string("dashboard.health.connection.not_refreshed"),
                status: .unknown,
                action: .refreshStatus
            )
        case .invalid:
            return passwordIssueRow(id: "connection-password-invalid", detailKey: "dashboard.health.connection.password_invalid")
        case .keychainUnavailable:
            return passwordIssueRow(id: "connection-keychain-unavailable", detailKey: "dashboard.health.connection.keychain_unavailable")
        }
    }

    private static func passwordIssueRow(id: String, detailKey: String) -> DashboardHealthRow {
        DashboardHealthRow(
            id: id,
            title: DashboardHealthDomain.connection.title,
            detail: L10n.string(detailKey),
            status: .failed,
            action: .replacePassword
        )
    }

    private static func reachabilityRow(from snapshot: DeviceReachabilitySnapshot) -> DashboardHealthRow {
        let status: DashboardHealthStatus
        switch snapshot.payload.status.lowercased() {
        case "reachable":
            status = .good
        case "partial":
            status = .warning
        case "unreachable":
            status = .failed
        default:
            status = .unknown
        }
        return DashboardHealthRow(
            id: "connection-reachability-\(snapshot.payload.status.lowercased())",
            title: DashboardHealthDomain.connection.title,
            detail: snapshot.payload.localizedSummary,
            status: status,
            action: .refreshStatus
        )
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
                detail: signal.countSummary,
                status: dashboardStatus(signal.severity),
                action: dashboardStatus(signal.severity) == .good ? nil : .viewCheckup
            )
        }
        guard let runtimeState = summary.profile.runtimeState else {
            return runtimeNotInstalledRow()
        }
        switch runtimeState.state {
        case .unknown:
            return runtimeNotInstalledRow()
        case .installing:
            return DashboardHealthRow(
                id: "runtime-installing-stored",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .running
            )
        case .installedVerified:
            return DashboardHealthRow(
                id: "runtime-installed",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .good,
                action: .openFinder
            )
        case .installedUnverified:
            return DashboardHealthRow(
                id: "runtime-installed-unverified",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .warning,
                action: .runCheckup
            )
        case .installFailed, .installInterrupted:
            return DashboardHealthRow(
                id: "runtime-install-failed",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .failed,
                action: .installUpdate
            )
        case .activationNeeded:
            return DashboardHealthRow(
                id: "runtime-activation-needed",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .warning,
                action: .startSMB
            )
        case .unhealthy:
            return DashboardHealthRow(
                id: "runtime-unhealthy",
                title: DashboardHealthDomain.runtime.title,
                detail: runtimeState.localizedSummary,
                status: .failed,
                action: .viewCheckup
            )
        }
    }

    private static func runtimeNotInstalledRow() -> DashboardHealthRow {
        DashboardHealthRow(
            id: "runtime-not-installed",
            title: DashboardHealthDomain.runtime.title,
            detail: L10n.string("dashboard.health.runtime.not_installed"),
            status: .warning,
            action: .installUpdate
        )
    }

    private static func checkupRow(
        summary: DeviceDashboardSummary,
        currentCheckupSummary: DoctorSummary?
    ) -> DashboardHealthRow {
        if summary.displayStatus == .checking {
            return DashboardHealthRow(
                id: "checkup-running",
                title: DashboardHealthDomain.checkup.title,
                detail: L10n.string("checkup.presentation.headline.running"),
                status: .running,
                action: .viewCheckup
            )
        }
        if let signal = serviceCheckupSignal(summary: currentCheckupSummary) {
            let status = dashboardStatus(signal.severity)
            return DashboardHealthRow(
                id: "checkup-current",
                title: DashboardHealthDomain.checkup.title,
                detail: signal.countSummary,
                status: status,
                action: status == .good ? nil : .viewCheckup
            )
        }
        if let hostWarning = summary.hostWarning {
            return DashboardHealthRow(
                id: "checkup-host-warning",
                title: DashboardHealthDomain.checkup.title,
                detail: hostWarning.message,
                status: .warning
            )
        }
        guard let lastCheckup = summary.profile.lastCheckup else {
            return DashboardHealthRow(
                id: "checkup-unchecked",
                title: DashboardHealthDomain.checkup.title,
                detail: L10n.string("dashboard.health.unchecked"),
                status: .unknown,
                action: DashboardActionPolicy.checkupAction(for: summary)
            )
        }
        return DashboardHealthRow(
            id: "checkup-snapshot",
            title: DashboardHealthDomain.checkup.title,
            detail: lastCheckup.localizedSummary,
            status: snapshotStatus(lastCheckup),
            action: snapshotStatus(lastCheckup) == .good ? nil : .viewCheckup
        )
    }

    private static func checkupSignal(
        for domain: DoctorCheckDomain,
        summary: DoctorSummary?
    ) -> DoctorDomainSignal? {
        DoctorCheckDomainPolicy.signal(for: domain, summary: summary)
    }

    private static func serviceCheckupSignal(summary: DoctorSummary?) -> DoctorDomainSignal? {
        let domains: [DoctorCheckDomain] = [.finderBonjour, .smbAuth, .timeMachine]
        let signals = domains.compactMap { checkupSignal(for: $0, summary: summary) }
        guard !signals.isEmpty else {
            return nil
        }
        return DoctorDomainSignal(
            domain: .general,
            checks: signals.flatMap(\.checks),
            passCount: signals.map(\.passCount).reduce(0, +),
            warnCount: signals.map(\.warnCount).reduce(0, +),
            failCount: signals.map(\.failCount).reduce(0, +),
            infoCount: signals.map(\.infoCount).reduce(0, +)
        )
    }

    private static func dashboardStatus(_ severity: DoctorCheckSeverity) -> DashboardHealthStatus {
        switch severity {
        case .failed:
            return .failed
        case .warning:
            return .warning
        case .passed:
            return .good
        case .unknown:
            return .unknown
        }
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
