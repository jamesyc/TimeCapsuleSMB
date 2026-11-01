import Foundation

enum MaintenanceUserAction: String, Equatable, Identifiable {
    case checkSSHAccess
    case enableSSHAccess
    case runActivation
    case runUninstall
    case findVolumes
    case planFsck
    case runFsck
    case runWipeDisk
    case installSMB
    case viewDiagnostics
    case backUpAndInspectFirmware
    case planPatch
    case planRestore
    case checkAppleFirmware
    case validateAppleFirmware
    case writePatch
    case writeRestore

    var id: String { rawValue }

    var title: String {
        switch self {
        case .checkSSHAccess:
            return L10n.string("maintenance.action.check_ssh_access")
        case .enableSSHAccess:
            return L10n.string("maintenance.action.enable_ssh_access")
        case .runActivation:
            return L10n.string("maintenance.action.start_smb")
        case .runUninstall:
            return L10n.string("maintenance.action.uninstall")
        case .findVolumes:
            return L10n.string("maintenance.action.find_volumes")
        case .planFsck:
            return L10n.string("maintenance.action.plan_disk_repair")
        case .runFsck:
            return L10n.string("maintenance.action.run_disk_repair")
        case .runWipeDisk:
            return L10n.string("maintenance.action.wipe_disk")
        case .installSMB:
            // The one spelling of Install / Update Samba, as the text beside
            // it names it.
            return L10n.string("dashboard.action.install_update_smb")
        case .viewDiagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        case .backUpAndInspectFirmware:
            return L10n.string("flash.action.backup_inspect")
        case .planPatch:
            return L10n.string("flash.action.plan_patch")
        case .planRestore:
            return L10n.string("flash.action.plan_restore")
        case .checkAppleFirmware:
            return L10n.string("flash.action.check_apple")
        case .validateAppleFirmware:
            return L10n.string("flash.action.download_apple")
        case .writePatch:
            return L10n.string("flash.action.write_patch")
        case .writeRestore:
            return L10n.string("flash.action.write_restore")
        }
    }

    var systemImage: String {
        switch self {
        case .checkSSHAccess:
            return "network"
        case .enableSSHAccess:
            return "key"
        case .planFsck:
            return "doc.text.magnifyingglass"
        case .runActivation:
            return "play.circle"
        case .runUninstall:
            return "trash"
        case .findVolumes:
            return "externaldrive"
        case .runFsck:
            return "externaldrive.badge.exclamationmark"
        case .runWipeDisk:
            return "externaldrive.badge.xmark"
        case .installSMB:
            return "square.and.arrow.down.on.square"
        case .viewDiagnostics:
            return "wrench.and.screwdriver"
        case .backUpAndInspectFirmware:
            return "externaldrive.badge.questionmark"
        case .planPatch, .planRestore:
            return "doc.text.magnifyingglass"
        case .checkAppleFirmware:
            return "checkmark.seal"
        case .validateAppleFirmware:
            return "checkmark.shield"
        case .writePatch:
            return "bolt.trianglebadge.exclamationmark"
        case .writeRestore:
            return "arrow.uturn.backward.circle"
        }
    }

    var isCommitAction: Bool {
        switch self {
        case .enableSSHAccess, .runActivation, .runUninstall, .runFsck, .runWipeDisk, .installSMB,
             .writePatch, .writeRestore:
            return true
        case .checkSSHAccess, .findVolumes, .planFsck, .viewDiagnostics,
             .backUpAndInspectFirmware, .planPatch, .planRestore, .checkAppleFirmware, .validateAppleFirmware:
            return false
        }
    }
}

struct MaintenanceCardPresentation: Equatable, Identifiable {
    let workflow: MaintenanceWorkflow
    let title: String
    let subtitle: String
    let stateTitle: String
    let isSelected: Bool

    var id: MaintenanceWorkflow.ID { workflow.id }
}

extension MaintenanceWorkflow {
    var presentationTitle: String {
        switch self {
        case .sshAccess:
            return L10n.string("maintenance.presentation.ssh_access.title")
        case .bootHook:
            return L10n.string("flash.title")
        case .activate:
            return L10n.string("maintenance.presentation.activate.title")
        case .uninstall:
            return L10n.string("maintenance.presentation.uninstall.title")
        case .fsck:
            return L10n.string("maintenance.presentation.fsck.title")
        case .wipeDisk:
            return L10n.string("maintenance.presentation.wipe_disk.title")
        }
    }

    var presentationSubtitle: String {
        switch self {
        case .sshAccess:
            return L10n.string("maintenance.presentation.ssh_access.subtitle")
        case .bootHook:
            return L10n.string("maintenance.presentation.boot_hook.subtitle")
        case .activate:
            return L10n.string("maintenance.presentation.activate.subtitle")
        case .uninstall:
            return L10n.string("maintenance.presentation.uninstall.subtitle")
        case .fsck:
            return L10n.string("maintenance.presentation.fsck.subtitle")
        case .wipeDisk:
            return L10n.string("maintenance.presentation.wipe_disk.subtitle")
        }
    }

    var presentationRisk: String {
        switch self {
        case .sshAccess:
            return L10n.string("maintenance.presentation.risk.reboot")
        case .bootHook:
            return L10n.string("maintenance.presentation.risk.firmware_write")
        case .activate:
            return L10n.string("maintenance.presentation.risk.remote_write")
        case .uninstall, .fsck, .wipeDisk:
            return L10n.string("maintenance.presentation.risk.destructive")
        }
    }
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

    init(items: [OperationTimelineItem]) {
        self.items = items
    }

    init(events: [BackendEvent], currentStage: OperationStageState?, workflow: MaintenanceWorkflow) {
        let operation = workflow.operationName
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == operation }
        if items.isEmpty, let currentStage, currentStage.operation == operation {
            items = [
                OperationTimelineBuilder.currentStageItem(currentStage)
            ]
        }
        self.items = items
    }
}

enum MaintenanceActionPolicy {
    static func actions(for workflow: MaintenanceWorkflow) -> [MaintenanceUserAction] {
        switch workflow {
        case .sshAccess:
            return [.checkSSHAccess, .enableSSHAccess]
        case .activate:
            return [.runActivation]
        case .uninstall:
            return [.runUninstall]
        case .fsck:
            return [.findVolumes, .planFsck, .runFsck]
        case .wipeDisk:
            return [.runWipeDisk]
        case .bootHook:
            return bootHookActionRows.flatMap { $0 }
        }
    }

    /// The boot hook's buttons in their two rows: the steps of a patch or a
    /// restore, then the Apple firmware checks.
    private static let bootHookActionRows: [[MaintenanceUserAction]] = [
        [.backUpAndInspectFirmware, .planPatch, .planRestore, .writePatch, .writeRestore],
        [.checkAppleFirmware, .validateAppleFirmware]
    ]

    /// The workflow's buttons now, a row each: the boot hook has two rows, and
    /// Wipe Internal Disk adds Install / Update Samba once it wiped the disk
    /// that held the Samba files (or no disk held them).
    @MainActor
    static func actionRows(for workflow: MaintenanceWorkflow, store: MaintenanceStore) -> [[MaintenanceUserAction]] {
        if workflow == .bootHook {
            return bootHookActionRows
        }
        let actions = actions(for: workflow)
        if workflow == .wipeDisk, store.wipeDiskState == .succeeded, store.wipeDiskResult?.offerInstall == true {
            return [actions + [.installSMB]]
        }
        return [actions]
    }

    @MainActor
    static func enabledActions(workflow: MaintenanceWorkflow, store: MaintenanceStore) -> Set<MaintenanceUserAction> {
        switch workflow {
        case .sshAccess:
            return enabled([
                (.checkSSHAccess, store.canCheckSSHAccess),
                (.enableSSHAccess, store.canEnableSSHAccess)
            ])
        case .activate:
            return enabled([
                (.runActivation, store.canRunActivation)
            ])
        case .uninstall:
            return enabled([
                (.runUninstall, store.canRunUninstall)
            ])
        case .fsck:
            return enabled([
                (.findVolumes, store.canFindFsckVolumes),
                (.planFsck, store.canPlanFsck),
                (.runFsck, store.canRunFsck)
            ])
        case .wipeDisk:
            return enabled([
                (.runWipeDisk, store.canRunWipeDisk),
                (.installSMB, !store.isBusy)
            ])
        case .bootHook:
            return enabled([
                (.backUpAndInspectFirmware, store.canBackUpFirmware),
                (.planPatch, store.canPlanFirmware),
                (.planRestore, store.canPlanFirmware),
                (.writePatch, store.canWritePatch),
                (.writeRestore, store.canWriteRestore),
                (.checkAppleFirmware, store.canPlanFirmware),
                (.validateAppleFirmware, store.canPlanFirmware)
            ])
        }
    }

    private static func enabled(_ pairs: [(MaintenanceUserAction, Bool)]) -> Set<MaintenanceUserAction> {
        Set(pairs.compactMap { action, isEnabled in
            isEnabled ? action : nil
        })
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
        case (_, .awaitingConfirmation):
            return L10n.string("maintenance.state.awaiting_confirmation")
        case (_, .running):
            return L10n.string("maintenance.state.running")
        case (_, .succeeded):
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
    /// Whether this workflow's own run is going, which animates its status line.
    let isRunning: Bool
    let warnings: [String]
    let actionRows: [[MaintenanceUserAction]]
    let enabledActions: Set<MaintenanceUserAction>
    let bootHookRows: [PresentationRow]
    let plan: MaintenancePlanPresentation?
    let completion: MaintenanceCompletionPresentation?
    let timeline: MaintenanceTimelinePresentation?
    let error: BackendErrorViewModel?
    private let backupSnapshotStale: Bool

    @MainActor
    init(store: MaintenanceStore, workflow selectedWorkflow: MaintenanceWorkflow? = nil) {
        let workflow = selectedWorkflow ?? store.selectedWorkflow
        let state = store.state(for: workflow)
        self.workflow = workflow
        self.title = workflow.presentationTitle
        self.risk = workflow.presentationRisk
        self.stateTitle = state.title
        let bootHook = store.bootHookStore
        switch state {
        case .operation(let state):
            self.subtitle = workflow.presentationSubtitle
            self.statusMessage = state.maintenanceStatusMessage(for: workflow)
            self.warnings = []
            self.bootHookRows = []
        case .bootHook:
            // The card's subtitle says what the boot hook is for; the panel
            // says what it changes.
            self.subtitle = L10n.string("flash.description")
            self.statusMessage = BootHookPresentation.statusMessage(store: store)
            self.warnings = BootHookPresentation.warnings(store: bootHook)
            self.bootHookRows = BootHookPresentation.rows(store: bootHook)
        }
        self.isRunning = store.isRunning(for: workflow)
        self.actionRows = MaintenanceActionPolicy.actionRows(for: workflow, store: store)
        self.enabledActions = MaintenanceActionPolicy.enabledActions(workflow: workflow, store: store)
        self.plan = Self.plan(workflow: workflow, store: store)
        self.completion = Self.completion(workflow: workflow, store: store)
        self.timeline = Self.timeline(workflow: workflow, state: state, store: store)
        self.error = store.error(for: workflow)
        self.backupSnapshotStale = workflow == .bootHook && bootHook.backupSnapshotStale
    }

    func isEnabled(_ action: MaintenanceUserAction) -> Bool {
        enabledActions.contains(action)
    }

    /// A button's label: after a firmware write the backup no longer
    /// describes the banks, so Back Up and Inspect says to do it again.
    func title(for action: MaintenanceUserAction) -> String {
        action == .backUpAndInspectFirmware && backupSnapshotStale
            ? L10n.string("flash.action.backup_inspect_again")
            : action.title
    }

    @MainActor
    private static func plan(
        workflow: MaintenanceWorkflow,
        store: MaintenanceStore
    ) -> MaintenancePlanPresentation? {
        switch workflow {
        case .sshAccess, .bootHook, .activate, .uninstall, .wipeDisk:
            return nil
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
        }
    }

    @MainActor
    private static func completion(
        workflow: MaintenanceWorkflow,
        store: MaintenanceStore
    ) -> MaintenanceCompletionPresentation? {
        switch workflow {
        case .bootHook:
            return nil
        case .sshAccess:
            guard let payload = store.sshAccessPayload else { return nil }
            return MaintenanceCompletionPresentation(
                title: L10n.string("maintenance.completion.ssh_access"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.result.host"), value: payload.host),
                    PresentationRow(label: L10n.string("maintenance.result.acp"), value: payload.acpPortReachable ? L10n.string("value.reachable") : L10n.string("value.not_reachable")),
                    PresentationRow(label: L10n.string("maintenance.result.ssh"), value: payload.sshPortReachable ? L10n.string("value.reachable") : L10n.string("value.not_reachable")),
                    PresentationRow(label: L10n.string("deploy.result.message"), value: payload.localizedSummary)
                ]
            )
        case .activate:
            guard let result = store.activationResult else { return nil }
            return MaintenanceCompletionPresentation(
                title: L10n.string("maintenance.completion.activate"),
                rows: [
                    PresentationRow(label: L10n.string("maintenance.result.already_active"), value: result.alreadyActive ? L10n.string("value.yes") : L10n.string("value.no")),
                    PresentationRow(label: L10n.string("deploy.result.message"), value: result.localizedSummary)
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
        case .wipeDisk:
            guard let result = store.wipeDiskResult else { return nil }
            var rows = [
                // The disk by Apple's name ("Disk 1"); "Device" means the Time
                // Capsule.
                PresentationRow(label: L10n.string("maintenance.result.disk"), value: result.diskLabel ?? result.disk),
                PresentationRow(label: L10n.string("maintenance.result.volume_name"), value: result.volumeName),
                PresentationRow(label: L10n.string("deploy.result.message"), value: result.localizedSummary)
            ]
            if result.offerInstall {
                rows.append(PresentationRow(label: L10n.string("maintenance.result.next"), value: L10n.string("maintenance.wipe_disk.install_next")))
            }
            return MaintenanceCompletionPresentation(title: L10n.string("maintenance.completion.wipe_disk"), rows: rows)
        }
    }

    private static func resultRows(_ result: MaintenanceResultPayload) -> [PresentationRow] {
        [
            PresentationRow(label: L10n.string("deploy.result.reboot_requested"), value: result.rebootRequested == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.verified"), value: result.verified == true ? L10n.string("value.yes") : L10n.string("value.no")),
            PresentationRow(label: L10n.string("deploy.result.message"), value: result.localizedSummary)
        ]
    }

    @MainActor
    private static func timeline(
        workflow: MaintenanceWorkflow,
        state: MaintenanceWorkflowState,
        store: MaintenanceStore
    ) -> MaintenanceTimelinePresentation? {
        switch state {
        // A boot hook step's timeline stays after it ends, whatever state it
        // ended in.
        case .bootHook, .operation(.loading), .operation(.planning), .operation(.awaitingConfirmation),
             .operation(.running), .operation(.succeeded), .operation(.failed):
            return MaintenanceTimelinePresentation(
                events: store.timelineEvents(for: workflow),
                currentStage: store.currentStage(for: workflow),
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
            switch workflow {
            case .bootHook:
                return profile.traits.supportsBootHook
            case .activate:
                return profile.traits.isNetBSD4
            case .sshAccess, .uninstall, .fsck, .wipeDisk:
                return true
            }
        }
    }
}

struct MaintenanceDashboardPresentation: Equatable {
    let cards: [MaintenanceCardPresentation]
    let detail: MaintenanceWorkflowDetailPresentation

    @MainActor
    init(store: MaintenanceStore, profile: DeviceProfile) {
        let workflows = MaintenanceWorkflowAvailability.workflows(for: profile)
        let selectedWorkflow = workflows.contains(store.selectedWorkflow)
            ? store.selectedWorkflow
            : workflows.first ?? store.selectedWorkflow
        self.cards = workflows.map { workflow in
            MaintenanceCardPresentation(
                workflow: workflow,
                title: workflow.presentationTitle,
                subtitle: workflow.presentationSubtitle,
                stateTitle: store.state(for: workflow).title,
                isSelected: workflow == selectedWorkflow
            )
        }
        self.detail = MaintenanceWorkflowDetailPresentation(store: store, workflow: selectedWorkflow)
    }
}

extension MaintenanceWorkflow {
    var operationName: String {
        switch self {
        case .sshAccess:
            return "set-ssh"
        case .bootHook:
            return "flash"
        case .activate:
            return "activate"
        case .uninstall:
            return "uninstall"
        case .fsck:
            return "fsck"
        case .wipeDisk:
            return "wipe-disk"
        }
    }
}

extension MaintenanceStore {
    func state(for workflow: MaintenanceWorkflow) -> MaintenanceWorkflowState {
        switch workflow {
        case .sshAccess:
            return .operation(sshAccessState)
        case .bootHook:
            return .bootHook(bootHookStore.state)
        case .activate:
            return .operation(activateState)
        case .uninstall:
            return .operation(uninstallState)
        case .fsck:
            return .operation(fsckState)
        case .wipeDisk:
            return .operation(wipeDiskState)
        }
    }
}
