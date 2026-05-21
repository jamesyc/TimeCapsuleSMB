import Foundation

typealias InstallPlanRow = PresentationRow

struct InstallPlanSection: Equatable, Identifiable {
    let title: String
    let rows: [InstallPlanRow]

    var id: String { title }
}

struct InstallPlanPresentation: Equatable {
    let title: String
    let sections: [InstallPlanSection]
    let warnings: [String]

    init(plan: DeployPlanPayload, profile: DeviceProfile, hostWarning: HostCompatibilityWarning? = nil) {
        self.title = plan.netbsd4
            ? L10n.string("install.plan.title.netbsd4")
            : L10n.string("install.plan.title.standard")
        self.sections = [
            InstallPlanSection(title: L10n.string("install.plan.section.target"), rows: [
                InstallPlanRow(label: L10n.string("deploy.presentation.row.target"), value: profile.title),
                InstallPlanRow(label: L10n.string("deploy.presentation.row.host"), value: plan.host),
                InstallPlanRow(label: L10n.string("deploy.presentation.row.payload"), value: plan.payloadFamily ?? profile.payloadFamily ?? L10n.string("value.unknown"))
            ]),
            InstallPlanSection(title: L10n.string("install.plan.section.files"), rows: [
                InstallPlanRow(label: L10n.string("install.plan.row.disk"), value: plan.volumeRoot ?? L10n.string("value.unknown")),
                InstallPlanRow(label: L10n.string("deploy.presentation.row.payload_directory"), value: plan.payloadDir),
                InstallPlanRow(label: L10n.string("install.plan.row.uploads"), value: "\(plan.uploads.count)")
            ]),
            InstallPlanSection(title: L10n.string("install.plan.section.device_actions"), rows: [
                InstallPlanRow(label: L10n.string("deploy.presentation.row.reboot"), value: plan.requiresReboot ? L10n.string("value.required") : L10n.string("value.not_required")),
                InstallPlanRow(label: L10n.string("install.plan.row.expected_downtime"), value: Self.expectedDowntime(plan: plan)),
                InstallPlanRow(label: L10n.string("install.plan.row.remote_actions"), value: "\(plan.preUploadActions.count + plan.postUploadActions.count + plan.activationActions.count)"),
                InstallPlanRow(label: L10n.string("deploy.presentation.row.post_install_checks"), value: "\(plan.postDeployChecks.count)")
            ])
        ]
        var warnings: [String] = []
        if plan.netbsd4 {
            warnings.append(L10n.string("deploy.presentation.warning.netbsd4_activation"))
        }
        if let hostWarning {
            warnings.append(hostWarning.message)
        }
        self.warnings = warnings
    }

    private static func expectedDowntime(plan: DeployPlanPayload) -> String {
        if plan.requiresReboot {
            return L10n.string("install.plan.downtime.reboot")
        }
        if plan.netbsd4 {
            return L10n.string("install.plan.downtime.netbsd4")
        }
        return L10n.string("install.plan.downtime.none")
    }
}

enum InstallUserAction: String, Equatable, Identifiable {
    case createPlan
    case regeneratePlan
    case installUpdate
    case openFinder
    case runCheckup
    case viewDiagnostics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .createPlan:
            return L10n.string("install.action.create_plan")
        case .regeneratePlan:
            return L10n.string("install.action.regenerate_plan")
        case .installUpdate:
            return L10n.string("install.action.install_update")
        case .openFinder:
            return L10n.string("dashboard.action.open_finder")
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .viewDiagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        }
    }

    var systemImage: String {
        switch self {
        case .createPlan, .regeneratePlan:
            return "doc.text.magnifyingglass"
        case .installUpdate:
            return "square.and.arrow.up"
        case .openFinder:
            return "folder"
        case .runCheckup:
            return "stethoscope"
        case .viewDiagnostics:
            return "wrench.and.screwdriver"
        }
    }
}

struct InstallTimelinePresentation: Equatable {
    let items: [OperationTimelineItem]

    init(events: [BackendEvent], currentStage: OperationStageState?) {
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == "deploy" }
        if items.isEmpty, let currentStage {
            items = [
                OperationTimelineItem(
                    id: "current:\(currentStage.operation):\(currentStage.stage)",
                    operation: currentStage.operation,
                    title: currentStage.stage,
                    detail: currentStage.description,
                    state: .running,
                    risk: currentStage.risk,
                    cancellable: currentStage.cancellable
                )
            ]
        }
        self.items = items
    }
}

struct InstallCompletionPresentation: Equatable {
    let title: String
    let rows: [PresentationRow]
    let warnings: [String]
    let actions: [InstallUserAction]

    init(result: DeployResultPayload) {
        self.title = result.verified == true
            ? L10n.string("install.completion.title.verified")
            : L10n.string("install.completion.title.finished")
        self.rows = [
            PresentationRow(label: L10n.string("deploy.result.verified"), value: result.verified == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.reboot_requested"), value: result.rebootRequested == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.message"), value: result.message ?? result.summary)
        ]
        var warnings: [String] = []
        if result.netbsd4 {
            warnings.append(L10n.string("install.completion.warning.netbsd4"))
        }
        self.warnings = warnings
        self.actions = [.openFinder, .runCheckup, .viewDiagnostics]
    }
}

struct InstallWorkflowPresentation: Equatable {
    let title: String
    let stateTitle: String
    let statusMessage: String
    let primaryAction: InstallUserAction?
    let notices: [String]
    let plan: InstallPlanPresentation?
    let timeline: InstallTimelinePresentation?
    let completion: InstallCompletionPresentation?

    init(
        state: DeployWorkflowState,
        plan: DeployPlanPayload?,
        result: DeployResultPayload?,
        error: BackendErrorViewModel?,
        events: [BackendEvent],
        currentStage: OperationStageState?,
        profile: DeviceProfile,
        hostWarning: HostCompatibilityWarning? = nil
    ) {
        self.title = L10n.string("dashboard.tab.install")
        self.stateTitle = state.title
        self.plan = plan.map { InstallPlanPresentation(plan: $0, profile: profile, hostWarning: hostWarning) }
        self.timeline = Self.timeline(for: state, events: events, currentStage: currentStage)
        self.completion = result.map(InstallCompletionPresentation.init)

        switch state {
        case .idle:
            self.statusMessage = L10n.string("install.state.idle")
            self.primaryAction = .createPlan
            self.notices = []
        case .planning:
            self.statusMessage = L10n.string("install.state.planning")
            self.primaryAction = nil
            self.notices = []
        case .planReady:
            self.statusMessage = L10n.string("install.state.plan_ready")
            self.primaryAction = plan == nil ? .createPlan : .installUpdate
            self.notices = []
        case .planStale:
            self.statusMessage = L10n.string("install.state.plan_stale")
            self.primaryAction = .regeneratePlan
            self.notices = [L10n.string("install.warning.plan_stale")]
        case .planFailed:
            self.statusMessage = error?.message ?? L10n.string("install.state.plan_failed")
            self.primaryAction = .createPlan
            self.notices = []
        case .deploying:
            self.statusMessage = L10n.string("install.state.deploying")
            self.primaryAction = nil
            self.notices = []
        case .awaitingConfirmation:
            self.statusMessage = L10n.string("install.state.awaiting_confirmation")
            self.primaryAction = nil
            self.notices = [L10n.string("install.warning.awaiting_confirmation")]
        case .deployed:
            self.statusMessage = L10n.string("install.state.deployed")
            self.primaryAction = nil
            self.notices = []
        case .deployFailed:
            self.statusMessage = error?.message ?? L10n.string("install.state.deploy_failed")
            self.primaryAction = .regeneratePlan
            self.notices = []
        }
    }

    private static func timeline(
        for state: DeployWorkflowState,
        events: [BackendEvent],
        currentStage: OperationStageState?
    ) -> InstallTimelinePresentation? {
        switch state {
        case .planning, .deploying, .awaitingConfirmation:
            return InstallTimelinePresentation(events: events, currentStage: currentStage)
        case .idle, .planReady, .planStale, .planFailed, .deployed, .deployFailed:
            return nil
        }
    }
}
