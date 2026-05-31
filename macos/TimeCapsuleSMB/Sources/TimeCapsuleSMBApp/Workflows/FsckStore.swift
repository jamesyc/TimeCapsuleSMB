import Combine
import Foundation

@MainActor
final class FsckStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var targets: [FsckTargetViewModel] = []
    @Published private(set) var selectedTargetID: FsckTargetViewModel.ID?
    @Published private(set) var plan: FsckPlanPayload?
    @Published private(set) var result: FsckResultPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let operation: MaintenanceWorkflowOperation
    private var plannedOptions: MaintenanceOptions?
    private var plannedTargetID: FsckTargetViewModel.ID?
    private var latestOptions: MaintenanceOptions?

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        self.operation = MaintenanceWorkflowOperation(
            name: "fsck",
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
    var canCancel: Bool { operation.canCancel }
    var pendingConfirmation: PendingConfirmation? { operation.pendingConfirmation }

    var selectedTarget: FsckTargetViewModel? {
        guard let selectedTargetID else {
            return nil
        }
        return targets.first { $0.id == selectedTargetID }
    }

    func canFindVolumes(mountWaitValue: Int?) -> Bool {
        !isBusy && mountWaitValue != nil
    }

    func canPlan(options: MaintenanceOptions?) -> Bool {
        return !isBusy && selectedTarget != nil && options != nil
    }

    func canRun(options: MaintenanceOptions?) -> Bool {
        return !isBusy
            && plan != nil
            && state == .planReady
            && options == plannedOptions
            && selectedTargetID == plannedTargetID
    }

    func selectTarget(id: FsckTargetViewModel.ID?, options: MaintenanceOptions?) {
        selectedTargetID = id
        markPlanStaleIfNeeded(options: options)
    }

    func markPlanStaleIfNeeded(options: MaintenanceOptions?) {
        latestOptions = options
        if state == .planReady,
           options != plannedOptions || selectedTargetID != plannedTargetID {
            state = .planStale
        }
    }

    func confirmPending() {
        operation.confirmPending()
    }

    func cancelPendingConfirmation(options: MaintenanceOptions?) {
        latestOptions = options
        operation.cancelPendingConfirmation()
        restoreStateAfterCancellation(options: options)
    }

    func cancel() {
        operation.cancel()
    }

    func clear() {
        operation.clear()
        state = .idle
        targets = []
        selectedTargetID = nil
        plan = nil
        result = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
        plannedOptions = nil
        plannedTargetID = nil
        latestOptions = nil
    }

    @discardableResult
    func refreshTargets(
        mountWaitValue: Int?,
        password: String,
        profile: DeviceProfile? = nil
    ) -> OperationStartResult {
        guard let mountWaitValue else {
            failLocally(.mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
        }
        let start = startRun(
            params: OperationParams.Fsck.listVolumes(mountWait: Double(mountWaitValue)),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .loading
        targets = []
        selectedTargetID = nil
        plan = nil
        result = nil
        return start
    }

    @discardableResult
    func planFsck(
        options: MaintenanceOptions?,
        password: String,
        profile: DeviceProfile? = nil
    ) -> OperationStartResult {
        latestOptions = options
        guard let options else {
            failLocally(.mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
        }
        guard let target = selectedTarget else {
            failLocally(.fsckTargetRequired)
            return .rejected(WorkflowLocalError.fsckTargetRequired.message)
        }
        let start = startRun(
            params: OperationParams.Fsck.run(
                dryRun: true,
                volume: target.volumeParam,
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait)
            ),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .planning
        plan = nil
        result = nil
        plannedOptions = options
        plannedTargetID = target.id
        return start
    }

    @discardableResult
    func runFsck(
        options: MaintenanceOptions?,
        password: String,
        profile: DeviceProfile? = nil
    ) -> OperationStartResult {
        latestOptions = options
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard let options,
              let plannedOptions,
              options == plannedOptions,
              let target = selectedTarget,
              selectedTargetID == plannedTargetID,
              plan != nil else {
            markStale(.fsckPlanStale)
            return .rejected(WorkflowLocalError.fsckPlanStale.message)
        }
        guard state == .planReady else {
            return .rejected(WorkflowLocalError.fsckPlanNotReady.message)
        }
        let start = startRun(
            params: OperationParams.Fsck.run(
                dryRun: false,
                volume: target.volumeParam,
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait)
            ),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .running
        result = nil
        return start
    }

    @discardableResult
    func rejectAlreadyRunning() -> OperationStartResult {
        rejectRun(.operationAlreadyRunning)
        return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
    }

    private func startRun(
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String?
    ) -> OperationStartResult {
        operation.start(
            params: params,
            profile: profile,
            password: password,
            rejectAlreadyRunning: { rejectRun(.operationAlreadyRunning) },
            resetRunState: resetRunState,
            rejectRun: rejectRun(message:)
        )
    }

    private func resetRunState() {
        operation.resetForRun()
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == operation.name else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .running
            }
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

        switch state {
        case .loading:
            handleListResult(event)
        case .planning:
            handlePlanResult(event)
        default:
            handleRunResult(event)
        }
    }

    private func handleListResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(FsckVolumeListPayload.self)
            targets = payload.targets.map(FsckTargetViewModel.init)
            selectedTargetID = targets.count == 1 ? targets[0].id : nil
            state = .listReady
            error = nil
            operation.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func handlePlanResult(_ event: BackendEvent) {
        do {
            plan = try event.decodePayload(FsckPlanPayload.self)
            state = .planReady
            error = nil
            operation.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func handleRunResult(_ event: BackendEvent) {
        do {
            result = try event.decodePayload(FsckResultPayload.self)
            state = .succeeded
            error = nil
            operation.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            error = nil
            currentStage = nil
            operation.finishObserver()
            restoreStateAfterCancellation(options: latestOptions)
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        operation.finishObserver()
    }

    private func restoreStateAfterCancellation(options: MaintenanceOptions?) {
        guard plan != nil else {
            state = targets.isEmpty ? .idle : .listReady
            return
        }
        state = options == plannedOptions && selectedTargetID == plannedTargetID ? .planReady : .planStale
    }

    private func markStale(_ localError: WorkflowLocalError) {
        state = .planStale
        error = operation.localError(localError)
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = operation.falseResultError(from: event)
        state = .failed
        operation.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = operation.contractDecodeError(decodeError)
        state = .failed
        operation.finishObserver()
    }

    private func failLocally(_ localError: WorkflowLocalError) {
        error = operation.localError(localError)
        currentStage = nil
        state = .failed
        operation.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = operation.localError(localError)
        currentStage = nil
        state = .failed
        operation.finishObserver()
    }

    private func rejectRun(message: String) {
        error = operation.rejectedError(message: message)
        currentStage = nil
        state = .failed
        operation.finishObserver()
    }
}
