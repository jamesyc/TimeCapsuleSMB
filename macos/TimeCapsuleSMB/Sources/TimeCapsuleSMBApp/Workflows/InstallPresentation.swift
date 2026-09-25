import Foundation

enum InstallUserAction: String, Equatable, Identifiable {
    case installUpdate
    case reinstall
    case openFinder
    case runCheckup
    case viewCheckup
    case viewDiagnostics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .installUpdate:
            return L10n.string("install.action.install_update")
        case .reinstall:
            return L10n.string("install.action.reinstall")
        case .openFinder:
            return L10n.string("dashboard.action.open_finder")
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .viewCheckup:
            return L10n.string("dashboard.action.view_checkup")
        case .viewDiagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        }
    }

    var systemImage: String {
        switch self {
        case .installUpdate:
            return "square.and.arrow.down.on.square"
        case .reinstall:
            return "arrow.clockwise"
        case .openFinder:
            return "folder"
        case .runCheckup:
            return "stethoscope"
        case .viewCheckup:
            return "list.bullet.clipboard"
        case .viewDiagnostics:
            return "wrench.and.screwdriver"
        }
    }
}

enum InstallCompletionActionPolicy {
    static func actions(isCheckupRunning: Bool) -> [InstallUserAction] {
        [.reinstall, .openFinder, isCheckupRunning ? .viewCheckup : .runCheckup, .viewDiagnostics]
    }
}

enum InstallActionAvailabilityPolicy {
    @MainActor
    static func isEnabled(
        _ action: InstallUserAction,
        store: DeployWorkflowStore,
        isDeviceBusy: Bool = false
    ) -> Bool {
        switch action {
        case .reinstall:
            return !isDeviceBusy && !store.isBusy && store.hasValidOptions
        case .installUpdate:
            return !isDeviceBusy && store.canDeploy
        case .runCheckup:
            return !isDeviceBusy && !store.isBusy
        case .openFinder, .viewCheckup, .viewDiagnostics:
            return true
        }
    }
}

struct InstallTimelinePresentation: Equatable {
    let items: [OperationTimelineItem]

    init(restoredDeploySuccess snapshot: DeviceDeployStateSnapshot) {
        self.items = [
            OperationTimelineItem(
                id: "restored:deploy:result",
                operation: "deploy",
                title: L10n.string("timeline.result.done"),
                detail: Self.restoredDeploySuccessDetail(snapshot),
                state: .succeeded,
                risk: nil,
                cancellable: nil
            )
        ]
    }

    init(
        events: [BackendEvent],
        currentStage: OperationStageState?,
        fallbackState: OperationTimelineItem.State = .running
    ) {
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == "deploy" }
        if items.isEmpty, let currentStage {
            items = [
                OperationTimelineItem(
                    id: "current:\(currentStage.operation):\(currentStage.stage)",
                    operation: currentStage.operation,
                    title: OperationTimelineBuilder.stageTitle(for: currentStage.operation, stage: currentStage.stage),
                    detail: OperationTimelineBuilder.stageDetail(
                        for: currentStage.operation,
                        stage: currentStage.stage,
                        fallback: currentStage.description
                    ),
                    state: fallbackState,
                    risk: currentStage.risk,
                    cancellable: currentStage.cancellable
                )
            ]
        }
        self.items = items
    }

    private static func restoredDeploySuccessDetail(_ snapshot: DeviceDeployStateSnapshot) -> String {
        BackendSummary.saved(snapshot.summaryRef, text: snapshot.summary)?.localized
            ?? L10n.string("timeline.deploy.result.completed")
    }
}

enum DeployFailureGuidancePolicy {
    static func guidance(for error: BackendErrorViewModel?) -> String? {
        guard let error, error.operation == "deploy" else {
            return nil
        }
        switch error.code {
        case "auth_failed", "validation_failed", "confirmation_required", "confirmation_cancelled":
            return nil
        default:
            if error.recovery?.hasGuidanceText == true {
                return nil
            }
            return L10n.string("deploy.failure.reboot_guidance")
        }
    }
}

struct InstallCompletionPresentation: Equatable {
    let title: String
    let rows: [PresentationRow]
    let warnings: [String]
    let actions: [InstallUserAction]

    init(result: DeployResultPayload, isCheckupRunning: Bool = false) {
        self.init(
            verified: result.verified,
            rebootRequested: result.rebootRequested,
            message: result.localizedSummary,
            netbsd4: result.netbsd4,
            isCheckupRunning: isCheckupRunning
        )
    }

    init(snapshot: DeviceDeployStateSnapshot, profile: DeviceProfile, isCheckupRunning: Bool = false) {
        self.init(
            verified: snapshot.verified,
            rebootRequested: snapshot.rebootRequested,
            message: snapshot.localizedSummary,
            netbsd4: Self.isNetBSD4(snapshot: snapshot, profile: profile),
            isCheckupRunning: isCheckupRunning
        )
    }

    private init(verified: Bool?, rebootRequested: Bool?, message: String, netbsd4: Bool, isCheckupRunning: Bool) {
        self.title = verified == true
            ? L10n.string("install.completion.title.verified")
            : L10n.string("install.completion.title.finished")
        self.rows = [
            PresentationRow(label: L10n.string("deploy.result.verified"), value: verified == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.reboot_requested"), value: rebootRequested == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.message"), value: message)
        ]
        var warnings: [String] = []
        if netbsd4 {
            warnings.append(L10n.string("install.completion.warning.netbsd4"))
        }
        self.warnings = warnings
        self.actions = InstallCompletionActionPolicy.actions(isCheckupRunning: isCheckupRunning)
    }

    private static func isNetBSD4(snapshot: DeviceDeployStateSnapshot, profile: DeviceProfile) -> Bool {
        snapshot.payloadFamily?.localizedCaseInsensitiveContains("netbsd4") == true || profile.traits.isNetBSD4
    }
}

struct InstallProgressPresentation: Equatable, BlockingProgressPresenting {
    let title: String
    let message: String
    let detail: String?

    init?(state: DeployWorkflowState, currentStage: OperationStageState?) {
        switch state {
        case .deploying:
            self.title = L10n.string("install.progress.deploying.title")
            self.message = L10n.string("install.progress.deploying.message")
        case .idle,
             .awaitingConfirmation,
             .deployed,
             .deployFailed:
            return nil
        }
        if let currentStage {
            self.detail = OperationTimelineBuilder.stageDetail(
                for: currentStage.operation,
                stage: currentStage.stage,
                fallback: currentStage.description ?? currentStage.stage
            )
        } else {
            self.detail = nil
        }
    }
}

struct InstallWorkflowPresentation: Equatable {
    let title: String
    let stateTitle: String
    let statusMessage: String
    let actions: [InstallUserAction]
    let notices: [String]
    let timeline: InstallTimelinePresentation?
    let completion: InstallCompletionPresentation?
    let error: BackendErrorViewModel?
    let failureGuidance: String?

    init(
        state: DeployWorkflowState,
        result: DeployResultPayload?,
        error: BackendErrorViewModel?,
        events: [BackendEvent],
        currentStage: OperationStageState?,
        profile: DeviceProfile,
        isCheckupRunning: Bool = false
    ) {
        let restoredFailure = Self.restoredDeployFailure(state: state, result: result, error: error, profile: profile)
        let restoredSuccess = Self.restoredDeploySuccess(state: state, result: result, error: error, profile: profile)
        let effectiveState = restoredFailure == nil ? state : DeployWorkflowState.deployFailed
        let effectiveError = error ?? restoredFailure.map { BackendErrorViewModel(operation: "deploy", deployState: $0) }
        self.title = L10n.string("dashboard.tab.install")
        self.timeline = Self.timeline(
            for: effectiveState,
            events: events,
            currentStage: currentStage,
            restoredFailure: restoredFailure,
            restoredSuccess: restoredSuccess
        )
        let persistedCompletion = restoredSuccess.map {
            InstallCompletionPresentation(snapshot: $0, profile: profile, isCheckupRunning: isCheckupRunning)
        }
        self.completion = result.map { InstallCompletionPresentation(result: $0, isCheckupRunning: isCheckupRunning) }
            ?? persistedCompletion
        self.stateTitle = persistedCompletion == nil ? effectiveState.title : DeployWorkflowState.deployed.title
        self.error = effectiveError
        self.failureGuidance = DeployFailureGuidancePolicy.guidance(for: effectiveError)

        switch effectiveState {
        case .idle:
            if persistedCompletion == nil {
                self.statusMessage = L10n.string("install.state.idle")
                self.actions = Self.deployActions()
            } else {
                self.statusMessage = L10n.string("install.state.deployed")
                self.actions = []
            }
            self.notices = []
        case .deploying:
            self.statusMessage = L10n.string("install.state.deploying")
            self.actions = Self.deployActions()
            self.notices = []
        case .awaitingConfirmation:
            self.statusMessage = L10n.string("install.state.awaiting_confirmation")
            self.actions = Self.deployActions()
            self.notices = [L10n.string("install.warning.awaiting_confirmation")]
        case .deployed:
            self.statusMessage = L10n.string("install.state.deployed")
            self.actions = []
            self.notices = []
        case .deployFailed:
            self.statusMessage = effectiveError?.message ?? L10n.string("install.state.deploy_failed")
            self.actions = Self.deployActions()
            self.notices = []
        }
    }

    private static func restoredDeployFailure(
        state: DeployWorkflowState,
        result: DeployResultPayload?,
        error: BackendErrorViewModel?,
        profile: DeviceProfile
    ) -> DeviceDeployStateSnapshot? {
        guard state == .idle, result == nil, error == nil,
              let deployState = profile.lastDeployState,
              deployState.status.isFailure else {
            return nil
        }
        return deployState
    }

    private static func restoredDeploySuccess(
        state: DeployWorkflowState,
        result: DeployResultPayload?,
        error: BackendErrorViewModel?,
        profile: DeviceProfile
    ) -> DeviceDeployStateSnapshot? {
        guard state == .idle, result == nil, error == nil,
              let deployState = profile.lastDeployState,
              deployState.status == .succeeded else {
            return nil
        }
        return deployState
    }

    private static func deployActions() -> [InstallUserAction] {
        [.installUpdate]
    }

    private static func timeline(
        for state: DeployWorkflowState,
        events: [BackendEvent],
        currentStage: OperationStageState?,
        restoredFailure: DeviceDeployStateSnapshot?,
        restoredSuccess: DeviceDeployStateSnapshot?
    ) -> InstallTimelinePresentation? {
        let hasDeployEvents = events.contains { $0.operation == "deploy" }
        switch state {
        case .idle:
            guard let restoredSuccess, currentStage == nil, !hasDeployEvents else {
                return nil
            }
            return InstallTimelinePresentation(restoredDeploySuccess: restoredSuccess)
        case .deploying, .awaitingConfirmation, .deployFailed, .deployed:
            if let restoredFailure, currentStage == nil, !hasDeployEvents {
                return InstallTimelinePresentation(
                    events: [],
                    currentStage: restoredFailure.stage.map {
                        OperationStageState(operation: "deploy", stage: $0, risk: nil, cancellable: nil, description: nil)
                    },
                    fallbackState: .failed
                )
            }
            let presentation = InstallTimelinePresentation(
                events: events,
                currentStage: currentStage,
                fallbackState: state == .deployed ? .succeeded : .running
            )
            return state == .deployed && presentation.items.isEmpty ? nil : presentation
        }
    }
}
