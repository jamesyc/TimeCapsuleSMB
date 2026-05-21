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
            return "square.and.arrow.up"
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
        }
    }
}

struct CheckupRowPresentation: Equatable, Identifiable {
    let id: String
    let status: CheckupStatusPresentation
    let statusText: String
    let message: String

    init(index: Int, check: DoctorCheckPayload) {
        self.id = "\(index):\(check.status):\(check.message)"
        self.status = CheckupStatusPresentation(status: check.status)
        self.statusText = check.status
        self.message = check.message
    }
}

struct CheckupDomainPresentation: Equatable, Identifiable {
    let domain: DoctorCheckDomain
    let status: CheckupStatusPresentation
    let countSummary: String
    let rows: [CheckupRowPresentation]

    var id: String { domain.rawValue }
    var title: String { domain.title }

    init(signal: DoctorDomainSignal) {
        self.domain = signal.domain
        self.status = CheckupStatusPresentation(severity: signal.severity)
        self.countSummary = signal.countSummary
        self.rows = signal.checks.enumerated().map { CheckupRowPresentation(index: $0.offset, check: $0.element) }
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
        self.domains = summary.map { DoctorCheckDomainPolicy.signals(from: $0).map(CheckupDomainPresentation.init) } ?? []
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
                OperationTimelineItem(
                    id: "current:\(currentStage.operation):\(currentStage.stage)",
                    operation: currentStage.operation,
                    title: OperationTimelineBuilder.operationTitle(currentStage.operation),
                    detail: currentStage.description,
                    state: .running,
                    risk: currentStage.risk,
                    cancellable: currentStage.cancellable
                )
            ]
        }
        return items
    }
}
