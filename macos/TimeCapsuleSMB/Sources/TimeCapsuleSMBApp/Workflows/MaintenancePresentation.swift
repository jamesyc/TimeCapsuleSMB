import Foundation

enum MaintenanceUserAction: String, Equatable, Identifiable {
    case planActivation
    case runActivation
    case planUninstall
    case runUninstall
    case findVolumes
    case planFsck
    case runFsck
    case scanMetadata
    case repairMetadata
    case viewDiagnostics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .planActivation:
            return L10n.string("maintenance.action.plan_start_smb")
        case .runActivation:
            return L10n.string("maintenance.action.start_smb")
        case .planUninstall:
            return L10n.string("maintenance.action.plan_uninstall")
        case .runUninstall:
            return L10n.string("maintenance.action.uninstall")
        case .findVolumes:
            return L10n.string("maintenance.action.find_volumes")
        case .planFsck:
            return L10n.string("maintenance.action.plan_disk_repair")
        case .runFsck:
            return L10n.string("maintenance.action.run_disk_repair")
        case .scanMetadata:
            return L10n.string("maintenance.action.scan_metadata")
        case .repairMetadata:
            return L10n.string("maintenance.action.repair_metadata")
        case .viewDiagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        }
    }

    var systemImage: String {
        switch self {
        case .planActivation, .planUninstall, .planFsck:
            return "doc.text.magnifyingglass"
        case .runActivation:
            return "play.circle"
        case .runUninstall:
            return "trash"
        case .findVolumes:
            return "externaldrive"
        case .runFsck:
            return "externaldrive.badge.exclamationmark"
        case .scanMetadata:
            return "magnifyingglass"
        case .repairMetadata:
            return "tag"
        case .viewDiagnostics:
            return "wrench.and.screwdriver"
        }
    }
}

struct MaintenanceWorkflowCardPresentation: Equatable, Identifiable {
    let workflow: MaintenanceWorkflow
    let title: String
    let subtitle: String
    let stateTitle: String
    let isSelected: Bool

    var id: MaintenanceWorkflow.ID { workflow.id }
}

struct MaintenancePlanPresentation: Equatable {
    let title: String
    let rows: [PresentationRow]
    let warnings: [String]
}

struct MaintenanceCompletionPresentation: Equatable {
    let title: String
    let rows: [PresentationRow]
}

struct MaintenanceTimelinePresentation: Equatable {
    let items: [OperationTimelineItem]

    init(events: [BackendEvent], currentStage: OperationStageState?, workflow: MaintenanceWorkflow) {
        let operation = workflow.operationName
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == operation }
        if items.isEmpty, let currentStage, currentStage.operation == operation {
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

struct MaintenanceActionContext: Equatable {
    let workflow: MaintenanceWorkflow
    let state: MaintenanceOperationState
    let hasSelectedFsckTarget: Bool
    let canRepairXattrs: Bool
}

enum MaintenanceActionPolicy {
    static func primaryAction(for context: MaintenanceActionContext) -> MaintenanceUserAction? {
        switch context.workflow {
        case .activate:
            switch context.state {
            case .idle, .failed, .succeeded:
                return .planActivation
            case .planReady:
                return .runActivation
            default:
                return nil
            }
        case .uninstall:
            switch context.state {
            case .idle, .failed, .succeeded, .planStale:
                return .planUninstall
            case .planReady:
                return .runUninstall
            default:
                return nil
            }
        case .fsck:
            switch context.state {
            case .idle, .failed, .succeeded:
                return .findVolumes
            case .listReady:
                return context.hasSelectedFsckTarget ? .planFsck : nil
            case .planStale:
                return .planFsck
            case .planReady:
                return .runFsck
            default:
                return nil
            }
        case .repairXattrs:
            switch context.state {
            case .idle, .failed, .repaired, .scanStale:
                return .scanMetadata
            case .scanReady:
                return context.canRepairXattrs ? .repairMetadata : .scanMetadata
            default:
                return nil
            }
        }
    }

    static func secondaryActions(workflow: MaintenanceWorkflow, state: MaintenanceOperationState) -> [MaintenanceUserAction] {
        switch workflow {
        case .activate:
            return state == .planReady ? [.planActivation] : []
        case .uninstall:
            return state == .planReady ? [.planUninstall] : []
        case .fsck:
            return state == .planReady ? [.planFsck, .findVolumes] : state == .listReady ? [.findVolumes] : []
        case .repairXattrs:
            return state == .scanReady ? [.scanMetadata] : []
        }
    }
}

extension MaintenanceOperationState {
    func maintenanceStatusMessage(for workflow: MaintenanceWorkflow) -> String {
        switch (workflow, self) {
        case (_, .idle):
            return L10n.string("maintenance.state.idle")
        case (_, .loading):
            return L10n.string("maintenance.state.loading")
        case (.fsck, .listReady):
            return L10n.string("maintenance.state.fsck_list_ready")
        case (_, .planning):
            return L10n.string("maintenance.state.planning")
        case (_, .planReady):
            return L10n.string("maintenance.state.plan_ready")
        case (_, .planStale):
            return L10n.string("maintenance.state.plan_stale")
        case (.repairXattrs, .scanning):
            return L10n.string("maintenance.state.scanning")
        case (.repairXattrs, .scanReady):
            return L10n.string("maintenance.state.scan_ready")
        case (.repairXattrs, .scanStale):
            return L10n.string("maintenance.state.scan_stale")
        case (_, .awaitingConfirmation):
            return L10n.string("maintenance.state.awaiting_confirmation")
        case (_, .running), (_, .repairing):
            return L10n.string("maintenance.state.running")
        case (_, .succeeded), (_, .repaired):
            return L10n.string("maintenance.state.succeeded")
        case (_, .failed):
            return L10n.string("maintenance.state.failed")
        default:
            return title
        }
    }
}

struct MaintenanceWorkflowDetailPresentation: Equatable {
    let workflow: MaintenanceWorkflow
    let title: String
    let subtitle: String
    let risk: String
    let stateTitle: String
    let statusMessage: String
    let primaryAction: MaintenanceUserAction?
    let secondaryActions: [MaintenanceUserAction]
    let plan: MaintenancePlanPresentation?
    let completion: MaintenanceCompletionPresentation?
    let timeline: MaintenanceTimelinePresentation?

    @MainActor
    init(store: MaintenanceStore, profile: DeviceProfile, workflow selectedWorkflow: MaintenanceWorkflow? = nil) {
        let workflow = selectedWorkflow ?? store.selectedWorkflow
        let legacy = MaintenanceWorkflowPresentation.presentation(for: workflow)
        let state = store.state(for: workflow)
        self.workflow = workflow
        self.title = legacy.title
        self.subtitle = legacy.subtitle
        self.risk = legacy.risk
        self.stateTitle = state.title
        self.statusMessage = state.maintenanceStatusMessage(for: workflow)
        self.primaryAction = MaintenanceActionPolicy.primaryAction(for: MaintenanceActionContext(
            workflow: workflow,
            state: state,
            hasSelectedFsckTarget: store.selectedFsckTarget != nil,
            canRepairXattrs: store.canRepairXattrs
        ))
        self.secondaryActions = MaintenanceActionPolicy.secondaryActions(workflow: workflow, state: state)
        self.plan = Self.plan(workflow: workflow, store: store, profile: profile)
        self.completion = Self.completion(workflow: workflow, store: store)
        self.timeline = Self.timeline(workflow: workflow, state: state, store: store)
    }

    @MainActor
    private static func plan(
        workflow: MaintenanceWorkflow,
        store: MaintenanceStore,
        profile: DeviceProfile
    ) -> MaintenancePlanPresentation? {
        switch workflow {
        case .activate:
            guard let plan = store.activationPlan else { return nil }
            return MaintenancePlanPresentation(
                title: L10n.string("maintenance.plan.activate"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.device"), value: profile.title),
                    PresentationRow(label: L10n.string("maintenance.plan.row.actions"), value: "\(plan.actions.count)"),
                    PresentationRow(label: L10n.string("maintenance.plan.row.post_checks"), value: "\(plan.postActivationChecks.count)")
                ],
                warnings: []
            )
        case .uninstall:
            guard let plan = store.uninstallPlan else { return nil }
            return MaintenancePlanPresentation(
                title: L10n.string("maintenance.plan.uninstall"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.host"), value: plan.host),
                    PresentationRow(label: L10n.string("maintenance.plan.row.payload_dirs"), value: "\(plan.payloadDirs.count)"),
                    PresentationRow(label: L10n.string("maintenance.plan.row.remote_actions"), value: "\(plan.remoteActions.count)"),
                    PresentationRow(label: L10n.string("maintenance.plan.row.reboot"), value: plan.requiresReboot ? L10n.string("value.required") : L10n.string("value.not_required")),
                    PresentationRow(label: L10n.string("maintenance.plan.row.post_checks"), value: "\(plan.postUninstallChecks.count)")
                ],
                warnings: [L10n.string("maintenance.warning.destructive_uninstall")]
            )
        case .fsck:
            guard let plan = store.fsckPlan else { return nil }
            return MaintenancePlanPresentation(
                title: L10n.string("maintenance.plan.fsck"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.device"), value: plan.device),
                    PresentationRow(label: L10n.string("maintenance.plan.row.mountpoint"), value: plan.mountpoint),
                    PresentationRow(label: L10n.string("maintenance.plan.row.reboot"), value: plan.rebootRequired ? L10n.string("value.required") : L10n.string("value.not_required")),
                    PresentationRow(label: L10n.string("maintenance.plan.row.wait_after_reboot"), value: plan.waitAfterReboot ? L10n.string("value.yes") : L10n.string("value.no"))
                ],
                warnings: [L10n.string("maintenance.warning.destructive_fsck")]
            )
        case .repairXattrs:
            guard let scan = store.repairScan else { return nil }
            return MaintenancePlanPresentation(
                title: L10n.string("maintenance.plan.repair_xattrs"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.path"), value: scan.root ?? L10n.string("value.unknown")),
                    PresentationRow(label: L10n.string("maintenance.plan.row.findings"), value: "\(scan.findingCount)"),
                    PresentationRow(label: L10n.string("maintenance.plan.row.repairable"), value: "\(scan.repairableCount)")
                ],
                warnings: scan.repairableCount > 0 ? [L10n.string("maintenance.warning.local_metadata_repair")] : []
            )
        }
    }

    @MainActor
    private static func completion(
        workflow: MaintenanceWorkflow,
        store: MaintenanceStore
    ) -> MaintenanceCompletionPresentation? {
        switch workflow {
        case .activate:
            guard let result = store.activationResult else { return nil }
            return MaintenanceCompletionPresentation(
                title: L10n.string("maintenance.completion.activate"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.result.already_active"), value: result.alreadyActive ? L10n.string("value.yes") : L10n.string("value.no")),
                    PresentationRow(label: L10n.string("deploy.result.message"), value: result.message ?? result.summary)
                ]
            )
        case .uninstall:
            guard let result = store.uninstallResult else { return nil }
            return MaintenanceCompletionPresentation(title: L10n.string("maintenance.completion.uninstall"), rows: resultRows(result))
        case .fsck:
            guard let result = store.fsckResult else { return nil }
            return MaintenanceCompletionPresentation(
                title: L10n.string("maintenance.completion.fsck"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.device"), value: result.device),
                    PresentationRow(label: L10n.string("maintenance.result.returncode"), value: result.returncode.map(String.init) ?? L10n.string("value.unknown")),
                    PresentationRow(label: L10n.string("deploy.result.verified"), value: result.verified == true ? L10n.string("value.yes") : L10n.string("value.no"))
                ]
            )
        case .repairXattrs:
            guard let result = store.repairResult else { return nil }
            return MaintenanceCompletionPresentation(
                title: L10n.string("maintenance.completion.repair_xattrs"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.plan.row.findings"), value: "\(result.findingCount)"),
                    PresentationRow(label: L10n.string("maintenance.plan.row.repairable"), value: "\(result.repairableCount)"),
                    PresentationRow(label: L10n.string("maintenance.result.returncode"), value: result.returncode.map(String.init) ?? L10n.string("value.unknown"))
                ]
            )
        }
    }

    private static func resultRows(_ result: MaintenanceResultPayload) -> [PresentationRow] {
        [
            PresentationRow(label: L10n.string("deploy.result.reboot_requested"), value: result.rebootRequested == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.verified"), value: result.verified == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.message"), value: result.message ?? result.summary)
        ]
    }

    @MainActor
    private static func timeline(
        workflow: MaintenanceWorkflow,
        state: MaintenanceOperationState,
        store: MaintenanceStore
    ) -> MaintenanceTimelinePresentation? {
        switch state {
        case .loading, .planning, .scanning, .awaitingConfirmation, .running, .repairing:
            return MaintenanceTimelinePresentation(
                events: store.events,
                currentStage: store.currentStage,
                workflow: workflow
            )
        default:
            return nil
        }
    }
}

enum MaintenanceWorkflowAvailability {
    static func workflows(for profile: DeviceProfile) -> [MaintenanceWorkflow] {
        MaintenanceWorkflow.allCases.filter { workflow in
            workflow != .activate || profile.traits.needsActivationAfterReboot
        }
    }
}

struct MaintenanceDashboardPresentation: Equatable {
    let cards: [MaintenanceWorkflowCardPresentation]
    let detail: MaintenanceWorkflowDetailPresentation

    @MainActor
    init(store: MaintenanceStore, profile: DeviceProfile) {
        let workflows = MaintenanceWorkflowAvailability.workflows(for: profile)
        let selectedWorkflow = workflows.contains(store.selectedWorkflow)
            ? store.selectedWorkflow
            : workflows.first ?? store.selectedWorkflow
        self.cards = workflows.map { workflow in
            let legacy = MaintenanceWorkflowPresentation.presentation(for: workflow)
            return MaintenanceWorkflowCardPresentation(
                workflow: workflow,
                title: legacy.title,
                subtitle: legacy.subtitle,
                stateTitle: store.state(for: workflow).title,
                isSelected: workflow == selectedWorkflow
            )
        }
        self.detail = MaintenanceWorkflowDetailPresentation(store: store, profile: profile, workflow: selectedWorkflow)
    }
}

extension MaintenanceWorkflow {
    var operationName: String {
        switch self {
        case .activate:
            return "activate"
        case .uninstall:
            return "uninstall"
        case .fsck:
            return "fsck"
        case .repairXattrs:
            return "repair-xattrs"
        }
    }
}

extension MaintenanceStore {
    func state(for workflow: MaintenanceWorkflow) -> MaintenanceOperationState {
        switch workflow {
        case .activate:
            return activateState
        case .uninstall:
            return uninstallState
        case .fsck:
            return fsckState
        case .repairXattrs:
            return repairState
        }
    }
}
