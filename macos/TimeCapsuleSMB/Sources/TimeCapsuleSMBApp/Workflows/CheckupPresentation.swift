import Foundation

enum CheckupUserAction: String, Equatable, Identifiable {
    case runCheckup
    case installUpdate
    case startSMB
    case replacePassword
    case openFinder
    case viewDiagnostics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .installUpdate:
            return L10n.string("dashboard.action.install_update_smb")
        case .startSMB:
            return L10n.string("dashboard.action.start_smb")
        case .replacePassword:
            return L10n.string("dashboard.action.replace_password")
        case .openFinder:
            return L10n.string("dashboard.action.open_finder")
        case .viewDiagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        }
    }

    var systemImage: String {
        switch self {
        case .runCheckup:
            return "stethoscope"
        case .installUpdate:
            return "square.and.arrow.down.on.square"
        case .startSMB:
            return "play.circle"
        case .replacePassword:
            return "key"
        case .openFinder:
            return "folder"
        case .viewDiagnostics:
            return "wrench.and.screwdriver"
        }
    }
}

enum CheckupStatusPresentation: String, Equatable {
    case passed
    case warning
    case failed
    case info
    case unknown
    /// A device section nothing ran on (SSH skipped or refused).
    case notChecked

    init(status: String) {
        switch status.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() {
        case "PASS":
            self = .passed
        case "WARN":
            self = .warning
        case "FAIL":
            self = .failed
        case "INFO":
            self = .info
        default:
            self = .unknown
        }
    }

    init(severity: DoctorCheckSeverity) {
        switch severity {
        case .failed:
            self = .failed
        case .warning:
            self = .warning
        case .passed:
            self = .passed
        case .unknown:
            self = .unknown
        }
    }

    init(section: DoctorSectionStatus) {
        switch section {
        case .pass:
            self = .passed
        case .warn:
            self = .warning
        case .fail:
            self = .failed
        case .notChecked:
            self = .notChecked
        }
    }

    var title: String {
        switch self {
        case .passed:
            return L10n.string("checkup.status.passed")
        case .warning:
            return L10n.string("checkup.status.warning")
        case .failed:
            return L10n.string("checkup.status.failed")
        case .info:
            return L10n.string("checkup.status.info")
        case .unknown:
            return L10n.string("checkup.status.unknown")
        case .notChecked:
            return L10n.string("checkup.status.not_checked")
        }
    }

    var systemImage: String {
        switch self {
        case .passed:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.triangle"
        case .failed:
            return "xmark.octagon"
        case .info:
            return "info.circle"
        case .unknown:
            return "questionmark.circle"
        case .notChecked:
            return "minus.circle"
        }
    }
}

struct CheckupRowPresentation: Equatable, Identifiable {
    let id: String
    let status: CheckupStatusPresentation
    let statusText: String
    let message: String

    init(index: Int, check: DoctorCheckPayload) {
        self.id = "\(index):\(check.status):\(check.code ?? check.message)"
        self.status = CheckupStatusPresentation(status: check.status)
        self.statusText = check.status
        self.message = check.localizedMessage
    }
}

struct CheckupDomainPresentation: Equatable, Identifiable {
    let domain: DoctorCheckDomain
    let status: CheckupStatusPresentation
    let countSummary: String
    let rows: [CheckupRowPresentation]

    var id: String { domain.rawValue }
    var title: String { domain.title }

    /// The group's status is its section's, as the helper decided it; the
    /// checks' own severity covers a Checkup still running.
    init(signal: DoctorDomainSignal, section: DoctorSection? = nil) {
        self.domain = signal.domain
        self.status = section.map { CheckupStatusPresentation(section: $0.status) }
            ?? CheckupStatusPresentation(severity: signal.severity)
        self.countSummary = signal.countSummary
        self.rows = signal.checks.enumerated().map { CheckupRowPresentation(index: $0.offset, check: $0.element) }
    }

    /// A section with no checks to list: the device when SSH was skipped.
    init(notChecked domain: DoctorCheckDomain) {
        self.domain = domain
        self.status = .notChecked
        self.countSummary = CheckupStatusPresentation.notChecked.title
        self.rows = []
    }
}

struct CheckupPresentation: Equatable {
    let title: String
    let stateTitle: String
    let headline: String
    let primaryAction: CheckupUserAction?
    let summaryRows: [PresentationRow]
    let domains: [CheckupDomainPresentation]
    let timeline: [OperationTimelineItem]
    let hostWarning: HostCompatibilityWarning?

    init(
        summary: DoctorSummary?,
        state: DoctorWorkflowState,
        events: [BackendEvent] = [],
        currentStage: OperationStageState? = nil,
        hostWarning: HostCompatibilityWarning? = nil
    ) {
        self.title = L10n.string("dashboard.tab.checkup")
        self.stateTitle = state.title
        self.headline = Self.headline(for: state)
        self.primaryAction = state == .running ? nil : .runCheckup
        self.summaryRows = summary.map(Self.summaryRows) ?? []
        // While it runs, the checks reported so far, as Install shows its steps.
        let checks = summary?.checks ?? (state == .running ? DoctorSummary.liveChecks(from: events) : [])
        var domains = DoctorCheckDomainPolicy.signals(from: checks).map { signal in
            CheckupDomainPresentation(signal: signal, section: summary?.sections[signal.domain])
        }
        // The device still gets its group when nothing ran on it, so the tab
        // says what the dashboard says.
        if summary?.sections[.device]?.status == .notChecked, !domains.contains(where: { $0.domain == .device }) {
            domains.append(CheckupDomainPresentation(notChecked: .device))
        }
        self.domains = domains
        self.timeline = Self.timeline(events: events, currentStage: currentStage, state: state)
        self.hostWarning = hostWarning
    }

    private static func headline(for state: DoctorWorkflowState) -> String {
        switch state {
        case .passed:
            return L10n.string("checkup.presentation.headline.passed")
        case .warning:
            return L10n.string("checkup.presentation.headline.warning")
        case .failed:
            return L10n.string("checkup.presentation.headline.failed")
        case .runFailed:
            return L10n.string("checkup.presentation.headline.run_failed")
        case .idle:
            return L10n.string("checkup.presentation.headline.idle")
        case .running:
            return L10n.string("checkup.presentation.headline.running")
        }
    }

    private static func summaryRows(_ summary: DoctorSummary) -> [PresentationRow] {
        [
            PresentationRow(label: L10n.string("checkup.presentation.row.pass"), value: "\(summary.passCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.warning"), value: "\(summary.warnCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.fail"), value: "\(summary.failCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.info"), value: "\(summary.infoCount)")
        ]
    }

    private static func timeline(
        events: [BackendEvent],
        currentStage: OperationStageState?,
        state: DoctorWorkflowState
    ) -> [OperationTimelineItem] {
        guard state == .running else {
            return []
        }
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == "doctor" }
        if items.isEmpty, let currentStage {
            items = [
                OperationTimelineBuilder.currentStageItem(currentStage)
            ]
        }
        return items
    }
}

struct CheckupProgressPresentation: Equatable, BlockingProgressPresenting {
    let title: String
    let message: String
    let detail: String?

    init?(state: DoctorWorkflowState, currentStage: OperationStageState?) {
        guard state == .running else {
            return nil
        }
        self.title = L10n.string("checkup.progress.running.title")
        self.message = L10n.string("checkup.progress.running.message")
        self.detail = nil
    }
}
