import Combine
import Foundation

enum FlashPlanMode: String, Codable, CaseIterable, Equatable, Identifiable {
    case patch
    case restore
    case checkApple = "check_apple"
    case downloadOnly = "download_only"

    var id: String { rawValue }
}

enum BootHookState: String, CaseIterable, Equatable {
    case readingBanks
    case savingBackup
    case analyzingBanks
    case planAvailable
    case appleCheckComplete
    case appleFirmwareMismatch
    case appleFirmwareReady
    case ready
    case awaitingStrongConfirmation
    case writing
    case readbackValidating
    case writeNotNeeded
    case writeValidatedSnapshotStale
    case restoreRebooting
    case failed

    var title: String {
        switch self {
        case .readingBanks:
            return L10n.string("workflow.state.reading_firmware_banks")
        case .savingBackup:
            return L10n.string("workflow.state.saving_backup")
        case .analyzingBanks:
            return L10n.string("workflow.state.analyzing_firmware")
        case .planAvailable:
            return L10n.string("workflow.state.plan_available")
        case .appleCheckComplete:
            return L10n.string("workflow.state.apple_check_complete")
        case .appleFirmwareMismatch:
            return L10n.string("workflow.state.apple_firmware_mismatch")
        case .appleFirmwareReady:
            return L10n.string("workflow.state.apple_firmware_ready")
        case .ready:
            return L10n.string("workflow.state.ready")
        case .awaitingStrongConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .writing:
            return L10n.string("workflow.state.writing_firmware")
        case .readbackValidating:
            return L10n.string("workflow.state.validating_write")
        case .writeNotNeeded:
            return L10n.string("workflow.state.write_not_needed")
        case .writeValidatedSnapshotStale:
            return L10n.string("workflow.state.snapshot_stale")
        case .restoreRebooting:
            return L10n.string("workflow.state.rebooting_after_restore")
        case .failed:
            return L10n.string("workflow.state.failed")
        }
    }
}

private struct BootHookFirmwareSelection: Equatable {
    let version: String
    let templatePath: String
}

@MainActor
final class BootHookStore: ObservableObject {
    @Published private(set) var state: BootHookState = .ready
    @Published private(set) var backup: FlashBackupPayload?
    @Published private(set) var plan: FlashPlanPayload?
    @Published private(set) var writeResult: FlashWritePayload?
    @Published private(set) var backupSnapshotStale = false
    /// The dashboard's alert asking the user to unplug the device.
    @Published private(set) var showsManualPowerCycleNotice = false
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?
    @Published var firmwareVersion = "" {
        didSet {
            invalidatePlanIfFirmwareSelectionChanged()
        }
    }
    @Published var firmwareTemplatePath = "" {
        didSet {
            invalidatePlanIfFirmwareSelectionChanged()
        }
    }

    /// The step a run performs, which says how to read its result.
    private enum RunKind: Equatable {
        case backup
        case plan
        case write
    }

    /// Whether the latest run connects to the device: a backup or a write. A
    /// plan reads only the saved backup, so its failure says nothing about
    /// the device.
    private(set) var lastRunConnectsToDevice = false
    private let operation: MaintenanceWorkflowOperation
    private var runKind: RunKind?
    private var pendingFirmwareSelection: BootHookFirmwareSelection?
    private var plannedFirmwareSelection: BootHookFirmwareSelection?

    init(
        backend: BackendClient,
        coordinator: OperationCoordinator? = nil,
        laneKey: OperationLaneKey? = nil
    ) {
        self.operation = MaintenanceWorkflowOperation(
            name: "flash",
            backend: backend,
            coordinator: coordinator,
            laneKey: laneKey
        )
        self.operation.bind(onEvent: { [weak self] event, activeOperation in
            self?.handle(event, activeOperation: activeOperation)
        }, onRunningChanged: { [weak self] in
            self?.objectWillChange.send()
        })
    }

    var events: [BackendEvent] { operation.events }
    var isRunning: Bool { operation.isRunning }
    var isBusy: Bool { operation.isBusy }

    var canPlan: Bool {
        !isBusy && backup != nil && !backupSnapshotStale
    }

    var canWritePatch: Bool {
        canWrite(mode: .patch)
    }

    var canWriteRestore: Bool {
        canWrite(mode: .restore)
    }

    var manualPowerCycleRequiredAfterWrite: Bool {
        guard let writeResult else {
            return false
        }
        return Self.requiresManualPowerCycleAfterWrite(writeResult)
    }

    @discardableResult
    func backUpAndInspectFirmware(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        let start = startRun(
            .backup,
            params: OperationParams.Flash.backup(),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .readingBanks
        backup = nil
        plan = nil
        writeResult = nil
        backupSnapshotStale = false
        pendingFirmwareSelection = nil
        plannedFirmwareSelection = nil
        return start
    }

    @discardableResult
    func planFirmware(mode: FlashPlanMode, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard let backupDir = backup?.backupDir, !backupSnapshotStale else {
            return reject(.flashBackupRequired)
        }
        let selection = currentFirmwareSelection
        let start = startRun(
            .plan,
            params: OperationParams.Flash.plan(
                backupDir: backupDir,
                mode: mode,
                firmwareVersion: selection.version,
                firmwareTemplate: selection.templatePath
            ),
            profile: profile
        )
        guard case .started = start else {
            return start
        }
        pendingFirmwareSelection = selection
        state = .analyzingBanks
        plan = nil
        writeResult = nil
        return start
    }

    @discardableResult
    func writeFirmware(mode: FlashPlanMode, password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard let plan, plan.mode == mode, plan.writeRequested, let backupDir = backup?.backupDir else {
            return reject(.flashPlanRequired)
        }
        let selection = currentFirmwareSelection
        // A field change drops the plan, so this cannot fail today; it keeps a
        // firmware write from ever using values that were never planned.
        guard plannedFirmwareSelection == selection else {
            return reject(.flashPlanStale)
        }
        let start = startRun(
            .write,
            params: OperationParams.Flash.write(
                backupDir: backupDir,
                mode: mode,
                firmwareVersion: selection.version,
                firmwareTemplate: selection.templatePath
            ),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .writing
        writeResult = nil
        return start
    }

    @discardableResult
    func rejectAlreadyRunning() -> OperationStartResult {
        operation.refuseAlreadyRunning { _ = reject(.operationAlreadyRunning) }
    }

    func dismissManualPowerCycleNotice() {
        showsManualPowerCycleNotice = false
    }

    private func canWrite(mode: FlashPlanMode) -> Bool {
        !isBusy
            && plan?.mode == mode
            && plan?.writeRequested == true
            && backup != nil
            && !backupSnapshotStale
            && plannedFirmwareSelection == currentFirmwareSelection
            && [.planAvailable, .failed].contains(state)
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == operation.name else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            state = stateForStage(stage.stage)
            return
        }

        if event.type == "error" {
            applyError(event, activeOperation: activeOperation)
            return
        }

        guard event.type == "result" else {
            return
        }
        if event.ok == false {
            applyFalseResult(event)
            return
        }
        applyResult(event)
    }

    private func applyResult(_ event: BackendEvent) {
        do {
            switch runKind {
            case .backup:
                backup = try event.decodePayload(FlashBackupPayload.self)
                backupSnapshotStale = false
                plannedFirmwareSelection = nil
                state = .planAvailable
            case .plan:
                let plan = try event.decodePayload(FlashPlanPayload.self)
                plannedFirmwareSelection = pendingFirmwareSelection ?? currentFirmwareSelection
                // A plan always works from a backup.
                if plannedFirmwareSelection == currentFirmwareSelection {
                    self.plan = plan
                    state = Self.stateAfterPlan(plan)
                } else {
                    self.plan = nil
                    state = .planAvailable
                }
            case .write:
                let result = try event.decodePayload(FlashWritePayload.self)
                writeResult = result
                if Self.writeMayHaveModifiedFirmware(result) {
                    markBackupStale()
                    if Self.requiresManualPowerCycleAfterWrite(result) {
                        showsManualPowerCycleNotice = true
                    }
                } else if result.writeStatus == "not_needed" {
                    // The helper planned again and found the bank already as
                    // asked, so it wrote nothing; the plan that said otherwise
                    // is wrong.
                    plan = nil
                    plannedFirmwareSelection = nil
                    state = .writeNotNeeded
                } else {
                    // Success that says neither: nothing to drop the plan for.
                    state = .planAvailable
                }
            case nil:
                break
            }
            error = nil
            currentStage = nil
        } catch {
            markSnapshotStaleIfFailedWriteMayHaveModifiedFirmware()
            self.error = operation.contractDecodeError(error)
            state = .failed
        }
        finishRun()
    }

    /// However a write fails, once it may have started writing the backup
    /// no longer describes the banks.
    private func markSnapshotStaleIfFailedWriteMayHaveModifiedFirmware() {
        if runKind == .write, currentStageMayHaveModifiedFirmware() {
            markBackupStale()
        }
    }

    private func markBackupStale() {
        backupSnapshotStale = true
        plan = nil
        plannedFirmwareSelection = nil
        state = .writeValidatedSnapshotStale
    }

    private static func writeMayHaveModifiedFirmware(_ result: FlashWritePayload) -> Bool {
        result.writeMayHaveModifiedDevice
            || (result.writeValidated && (result.mode == .patch || result.mode == .restore))
    }

    private static func requiresManualPowerCycleAfterWrite(_ result: FlashWritePayload) -> Bool {
        guard writeMayHaveModifiedFirmware(result) else {
            return false
        }
        switch result.mode {
        case .patch:
            return true
        case .restore:
            // A rewritten secondary bank needs nothing: the device keeps running its primary.
            return !result.rebootRequested && result.postWriteAction != "none"
        case .checkApple, .downloadOnly:
            return false
        }
    }

    private static func stateAfterPlan(_ plan: FlashPlanPayload) -> BootHookState {
        switch plan.mode {
        case .checkApple:
            return plan.appleFirmwareMatch?.matched == false ? .appleFirmwareMismatch : .appleCheckComplete
        case .downloadOnly:
            return .appleFirmwareReady
        case .patch, .restore:
            return .planAvailable
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        if event.code == "confirmation_required" {
            // Without a confirmation the app can read there is nothing to
            // confirm, and the helper has already exited: the write failed.
            guard PendingConfirmation(confirmationEvent: event, originalParams: [:]) != nil else {
                error = operation.contractDecodeError(BackendContractError.payloadDecodeFailed(
                    operation: operation.name, payloadType: "PendingConfirmation", message: "no confirmation_id"
                ))
                state = .failed
                finishRun()
                return
            }
            error = nil
            state = .awaitingStrongConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            // Only a write asks for confirmation, and it keeps its plan.
            error = nil
            currentStage = nil
            state = .planAvailable
            finishRun()
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        markSnapshotStaleIfFailedWriteMayHaveModifiedFirmware()
        // The live bank no longer matches the backup the plan came from.
        if event.code == "flash_backup_outdated" {
            markBackupStale()
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        finishRun()
    }

    private var currentFirmwareSelection: BootHookFirmwareSelection {
        BootHookFirmwareSelection(
            version: firmwareVersion.trimmingCharacters(in: .whitespacesAndNewlines),
            templatePath: firmwareTemplatePath.trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }

    private func invalidatePlanIfFirmwareSelectionChanged() {
        // A step that is still running, or waiting for its confirmation, keeps
        // its plan; finishRun checks again when it ends. The lane can still be
        // busy after that, while the helper exits.
        guard runKind == nil, plan != nil, plannedFirmwareSelection != currentFirmwareSelection else {
            return
        }
        plan = nil
        writeResult = nil
        plannedFirmwareSelection = nil
        if backup != nil, !backupSnapshotStale, state != .failed {
            state = .planAvailable
        }
    }

    private func currentStageMayHaveModifiedFirmware() -> Bool {
        currentStage?.stage == "write_primary_bank"
            || currentStage?.stage == "write_active_bank"
            || currentStage?.stage == "write_secondary_bank"
            || currentStage?.stage == "post_write_validation"
            || currentStage?.stage == "reboot"
            || currentStage?.stage == "wait_for_reboot_down"
            || currentStage?.stage == "wait_for_reboot_up"
    }

    private func applyFalseResult(_ event: BackendEvent) {
        markSnapshotStaleIfFailedWriteMayHaveModifiedFirmware()
        error = operation.falseResultError(from: event)
        state = .failed
        finishRun()
    }

    private func stateForStage(_ stage: String) -> BootHookState {
        switch stage {
        case "read_flash":
            return .readingBanks
        case "save_raw_backup":
            return .savingBackup
        case "inspect_backup", "analyze_flash", "plan_flash":
            return .analyzingBanks
        case "confirm_write":
            return .awaitingStrongConfirmation
        case "pre_write_validation", "post_write_validation":
            return .readbackValidating
        case "write_primary_bank", "write_active_bank", "write_secondary_bank":
            return .writing
        case "reboot", "wait_for_reboot_down", "wait_for_reboot_up":
            return .restoreRebooting
        default:
            return state
        }
    }

    private func reject(_ localError: WorkflowLocalError) -> OperationStartResult {
        error = operation.localError(localError)
        state = .failed
        return .rejected(localError.message)
    }

    private func startRun(
        _ kind: RunKind,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String? = nil
    ) -> OperationStartResult {
        let start = operation.start(
            params: params,
            profile: profile,
            password: password,
            resetRunState: resetRunState,
            rejectRun: { message in
                error = operation.rejectedError(message: message)
                state = .failed
            }
        )
        if case .started = start {
            runKind = kind
            lastRunConnectsToDevice = kind != .plan
        }
        return start
    }

    private func resetRunState() {
        operation.resetForRun()
        error = nil
        showsManualPowerCycleNotice = false
        currentStage = nil
        passwordInvalidProfileID = nil
        runKind = nil
    }

    private func finishRun() {
        operation.finishObserver()
        runKind = nil
        pendingFirmwareSelection = nil
        // The fields are locked while busy, but a template chosen in a panel
        // opened before the run can still land during it.
        invalidatePlanIfFirmwareSelectionChanged()
    }
}
