import Combine
import Foundation

enum FlashBuildPolicy: String, CaseIterable, Equatable {
    case disabled
    case readOnly
    case writesEnabled
}

enum FlashPlanMode: String, Codable, CaseIterable, Equatable, Identifiable {
    case patch
    case restore
    case checkApple = "check_apple"
    case downloadOnly = "download_only"

    var id: String { rawValue }

    var writesFirmware: Bool {
        self == .patch || self == .restore
    }
}

enum FlashWorkflowState: String, CaseIterable, Equatable {
    case unavailable
    case disabledInThisBuild
    case eligibleForReadOnlyAnalysis
    case readingBanks
    case savingBackup
    case analyzingBanks
    case planAvailable
    case appleCheckComplete
    case appleFirmwareMismatch
    case appleFirmwareReady
    case writeLocked
    case awaitingStrongConfirmation
    case writing
    case readbackValidating
    case writeValidated
    case writeValidatedSnapshotStale
    case manualPowerCycleRequired
    case restoreRebooting
    case failed

    var title: String {
        switch self {
        case .unavailable:
            return L10n.string("workflow.state.unavailable")
        case .disabledInThisBuild:
            return L10n.string("workflow.state.disabled_in_this_build")
        case .eligibleForReadOnlyAnalysis:
            return L10n.string("workflow.state.read_only_analysis_available")
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
        case .writeLocked:
            return L10n.string("workflow.state.ready")
        case .awaitingStrongConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .writing:
            return L10n.string("workflow.state.writing_firmware")
        case .readbackValidating:
            return L10n.string("workflow.state.validating_write")
        case .writeValidated:
            return L10n.string("workflow.state.write_validated")
        case .writeValidatedSnapshotStale:
            return L10n.string("workflow.state.snapshot_stale")
        case .manualPowerCycleRequired:
            return L10n.string("workflow.state.manual_power_cycle_required")
        case .restoreRebooting:
            return L10n.string("workflow.state.rebooting_after_restore")
        case .failed:
            return L10n.string("workflow.state.failed")
        }
    }
}

struct FlashEligibility: Equatable {
    let state: FlashWorkflowState
    let messageKey: String
    let readOnlyAllowed: Bool
    let writeAllowed: Bool

    var message: String {
        L10n.string(messageKey)
    }
}

enum FlashEligibilityPolicy {
    static func eligibility(for profile: DeviceProfile, buildPolicy: FlashBuildPolicy = .writesEnabled) -> FlashEligibility {
        guard profile.traits.supportsFlashBootHook else {
            return FlashEligibility(
                state: .unavailable,
                messageKey: "flash.eligibility.netbsd4_required",
                readOnlyAllowed: false,
                writeAllowed: false
            )
        }

        switch buildPolicy {
        case .disabled:
            return FlashEligibility(
                state: .disabledInThisBuild,
                messageKey: "flash.eligibility.disabled",
                readOnlyAllowed: false,
                writeAllowed: false
            )
        case .readOnly:
            return FlashEligibility(
                state: .eligibleForReadOnlyAnalysis,
                messageKey: "flash.eligibility.read_only",
                readOnlyAllowed: true,
                writeAllowed: false
            )
        case .writesEnabled:
            return FlashEligibility(
                state: .writeLocked,
                messageKey: "flash.eligibility.write_ready",
                readOnlyAllowed: true,
                writeAllowed: true
            )
        }
    }
}

enum FlashBootHookVisibilityPolicy {
    static func isVisible(for profile: DeviceProfile) -> Bool {
        profile.traits.supportsFlashBootHook
    }
}

struct FlashManualPowerCycleNotice: Identifiable, Equatable {
    let id = UUID()
    let mode: FlashPlanMode
}

private struct FlashFirmwareSelection: Equatable {
    let version: String
    let templatePath: String
}

@MainActor
final class FlashWorkflowStore: ObservableObject {
    @Published private(set) var state: FlashWorkflowState = .writeLocked
    @Published private(set) var backup: FlashBackupPayload?
    @Published private(set) var plan: FlashPlanPayload?
    @Published private(set) var writeResult: FlashWritePayload?
    @Published private(set) var backupSnapshotStale = false
    @Published private(set) var manualPowerCycleNotice: FlashManualPowerCycleNotice?
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

    let buildPolicy: FlashBuildPolicy
    let backend: BackendClient
    private let coordinator: OperationCoordinator?
    private let laneKey: OperationLaneKey?
    private var eligibility = FlashEligibility(
        state: .writeLocked,
        messageKey: "flash.eligibility.write_ready",
        readOnlyAllowed: true,
        writeAllowed: true
    )
    private let operationObserver = BackendOperationObserver()
    private var activeAction: FlashUserAction?
    private var pendingFirmwareSelection: FlashFirmwareSelection?
    private var plannedFirmwareSelection: FlashFirmwareSelection?
    private var cancellables: Set<AnyCancellable> = []

    convenience init(buildPolicy: FlashBuildPolicy = .writesEnabled) {
        self.init(backend: BackendClient(), buildPolicy: buildPolicy)
    }

    init(backend: BackendClient, buildPolicy: FlashBuildPolicy = .writesEnabled) {
        self.backend = backend
        self.coordinator = nil
        self.laneKey = nil
        self.buildPolicy = buildPolicy
        observeBackend(backend)
    }

    convenience init(coordinator: OperationCoordinator, laneKey: OperationLaneKey, buildPolicy: FlashBuildPolicy = .writesEnabled) {
        let lane = coordinator.lane(for: laneKey)
        self.init(backend: lane.backend, coordinator: coordinator, laneKey: laneKey, buildPolicy: buildPolicy)
    }

    private init(
        backend: BackendClient,
        coordinator: OperationCoordinator?,
        laneKey: OperationLaneKey?,
        buildPolicy: FlashBuildPolicy
    ) {
        self.backend = backend
        self.coordinator = coordinator
        self.laneKey = laneKey
        self.buildPolicy = buildPolicy
        observeBackend(backend)
    }

    private func observeBackend(_ backend: BackendClient) {
        backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var events: [BackendEvent] {
        backend.events
    }

    var isRunning: Bool {
        backend.isRunning
    }

    var isBusy: Bool {
        backend.isRunning || backend.pendingConfirmation != nil
    }

    var canBackup: Bool {
        !isBusy && eligibility.readOnlyAllowed
    }

    var canPlan: Bool {
        !isBusy && eligibility.readOnlyAllowed && backup != nil && !backupSnapshotStale
    }

    var canPlanWrites: Bool {
        canPlan && eligibility.writeAllowed
    }

    var canWritePatch: Bool {
        canWrite(mode: .patch)
    }

    var canWriteRestore: Bool {
        canWrite(mode: .restore)
    }

    var eligibilityMessage: String {
        eligibility.message
    }

    var manualPowerCycleRequiredAfterWrite: Bool {
        guard let writeResult else {
            return false
        }
        return Self.requiresManualPowerCycleAfterWrite(writeResult)
    }

    func refresh(profile: DeviceProfile) {
        eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: buildPolicy)
        if backup == nil, plan == nil, writeResult == nil, operationObserver.activeOperation == nil {
            state = eligibility.state
        }
    }

    @discardableResult
    func backupAndInspect(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard canBackup else {
            return reject(.flashBackupUnavailable)
        }
        let start = startRun(
            action: .backupAndInspect,
            params: OperationParams.flashBackup(password: password),
            profile: profile
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
        process(backend.events)
        return start
    }

    @discardableResult
    func planFlash(mode: FlashPlanMode, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard canPlan else {
            return reject(.flashBackupRequired)
        }
        if mode.writesFirmware, !canPlanWrites {
            return reject(.flashWritesDisabled)
        }
        guard let backupDir = backup?.backupDir else {
            return reject(.flashBackupRequired)
        }
        let action = FlashUserAction.planAction(for: mode)
        let selection = currentFirmwareSelection
        let start = startRun(
            action: action,
            params: OperationParams.flashPlan(
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
        process(backend.events)
        return start
    }

    @discardableResult
    func write(mode: FlashPlanMode, password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard mode.writesFirmware else {
            return reject(.flashModeReadOnly)
        }
        guard !isBusy else {
            return reject(.operationAlreadyRunning)
        }
        guard let plan, plan.mode == mode, plan.writeRequested, let backupDir = backup?.backupDir else {
            state = .writeLocked
            return reject(.flashPlanRequired)
        }
        let selection = currentFirmwareSelection
        guard plannedFirmwareSelection == selection else {
            return reject(.flashPlanStale)
        }
        let action = mode == .patch ? FlashUserAction.writePatch : .writeRestore
        let start = startRun(
            action: action,
            params: OperationParams.flashWrite(
                backupDir: backupDir,
                mode: mode,
                firmwareVersion: selection.version,
                firmwareTemplate: selection.templatePath,
                password: password
            ),
            profile: profile
        )
        guard case .started = start else {
            return start
        }
        state = .writing
        writeResult = nil
        process(backend.events)
        return start
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
        state = eligibility.state
        backup = nil
        plan = nil
        writeResult = nil
        backupSnapshotStale = false
        manualPowerCycleNotice = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
        operationObserver.finish()
        activeAction = nil
        pendingFirmwareSelection = nil
        plannedFirmwareSelection = nil
    }

    func dismissManualPowerCycleNotice() {
        manualPowerCycleNotice = nil
    }

    private func canWrite(mode: FlashPlanMode) -> Bool {
        !isBusy
            && eligibility.writeAllowed
            && plan?.mode == mode
            && plan?.writeRequested == true
            && backup != nil
            && !backupSnapshotStale
            && plannedFirmwareSelection == currentFirmwareSelection
            && [.planAvailable, .failed].contains(state)
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "flash" else {
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
            switch activeAction {
            case .backupAndInspect:
                backup = try event.decodePayload(FlashBackupPayload.self)
                backupSnapshotStale = false
                plannedFirmwareSelection = nil
                state = .planAvailable
            case .planPatch, .planRestore, .checkApple, .downloadApple:
                plan = try event.decodePayload(FlashPlanPayload.self)
                if let plan {
                    plannedFirmwareSelection = pendingFirmwareSelection ?? currentFirmwareSelection
                    pendingFirmwareSelection = nil
                    if plannedFirmwareSelection == currentFirmwareSelection {
                        state = Self.stateAfterPlan(plan)
                    } else {
                        self.plan = nil
                        state = backup == nil ? eligibility.state : .planAvailable
                    }
                }
            case .writePatch, .writeRestore:
                let result = try event.decodePayload(FlashWritePayload.self)
                writeResult = result
                if Self.writeMayHaveModifiedFirmware(result) {
                    markSnapshotStaleAfterWrite()
                    if Self.requiresManualPowerCycleAfterWrite(result) {
                        manualPowerCycleNotice = FlashManualPowerCycleNotice(mode: result.mode)
                    }
                } else {
                    state = .writeValidated
                }
            case nil:
                break
            }
            error = nil
            currentStage = nil
            operationObserver.finish()
            activeAction = nil
            pendingFirmwareSelection = nil
        } catch {
            self.error = BackendErrorViewModel(
                operation: "flash",
                code: "contract_decode_failed",
                message: error.localizedDescription
            )
            state = .failed
            operationObserver.finish()
            activeAction = nil
            pendingFirmwareSelection = nil
        }
    }

    private func markSnapshotStaleAfterWrite() {
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
            return !result.rebootRequested
        case .checkApple, .downloadOnly:
            return false
        }
    }

    private static func stateAfterPlan(_ plan: FlashPlanPayload) -> FlashWorkflowState {
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
            error = nil
            state = .awaitingStrongConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            error = nil
            currentStage = nil
            operationObserver.finish()
            activeAction = nil
            pendingFirmwareSelection = nil
            state = plan == nil ? (backup == nil ? eligibility.state : .writeLocked) : .planAvailable
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        if activeAction == .writePatch || activeAction == .writeRestore,
           currentStageMayHaveModifiedFirmware() {
            markSnapshotStaleAfterWrite()
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        operationObserver.finish()
        activeAction = nil
        pendingFirmwareSelection = nil
    }

    private var currentFirmwareSelection: FlashFirmwareSelection {
        FlashFirmwareSelection(
            version: firmwareVersion.trimmingCharacters(in: .whitespacesAndNewlines),
            templatePath: firmwareTemplatePath.trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }

    private func invalidatePlanIfFirmwareSelectionChanged() {
        guard !isBusy, plan != nil, plannedFirmwareSelection != currentFirmwareSelection else {
            return
        }
        plan = nil
        writeResult = nil
        plannedFirmwareSelection = nil
        if backup != nil, !backupSnapshotStale {
            state = .planAvailable
        }
    }

    private func currentStageMayHaveModifiedFirmware() -> Bool {
        guard currentStage?.operation == "flash" else {
            return false
        }
        return currentStage?.stage == "write_primary_bank"
            || currentStage?.stage == "write_active_bank"
            || currentStage?.stage == "post_write_validation"
            || currentStage?.stage == "reboot"
            || currentStage?.stage == "wait_for_reboot_down"
            || currentStage?.stage == "wait_for_reboot_up"
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        operationObserver.finish()
        activeAction = nil
    }

    private func stateForStage(_ stage: String) -> FlashWorkflowState {
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
        case "write_primary_bank", "write_active_bank":
            return .writing
        case "reboot", "wait_for_reboot_down", "wait_for_reboot_up":
            return .restoreRebooting
        default:
            return state
        }
    }

    private func reject(_ message: String) -> OperationStartResult {
        error = BackendErrorViewModel(operation: "flash", code: "operation_rejected", message: message)
        state = .failed
        return .rejected(message)
    }

    private func reject(_ localError: WorkflowLocalError) -> OperationStartResult {
        error = BackendErrorViewModel(operation: "flash", localError: localError)
        state = .failed
        return .rejected(localError.message)
    }

    private func startRun(
        action: FlashUserAction,
        params: [String: JSONValue],
        profile: DeviceProfile?
    ) -> OperationStartResult {
        guard !isBusy else {
            return reject(.operationAlreadyRunning)
        }
        resetRunState()
        let start = run(operation: "flash", params: params, profile: profile)
        switch start {
        case .started(let operation):
            operationObserver.start(operation)
            activeAction = action
        case .rejected(let message):
            return reject(message)
        }
        return start
    }

    private func resetRunState() {
        backend.clear()
        operationObserver.clear()
        error = nil
        manualPowerCycleNotice = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        operationObserver.finish()
        activeAction = nil
    }

    private func run(operation: String, params: [String: JSONValue], profile: DeviceProfile?) -> OperationStartResult {
        if let coordinator {
            return coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                laneKey: laneKey
            )
        }
        guard !isBusy else {
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        let context = profile?.runtimeContext
        let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
        backend.run(
            operation: operation,
            params: params,
            context: context,
            requestID: activeOperation.id.uuidString
        )
        return .started(activeOperation)
    }
}
