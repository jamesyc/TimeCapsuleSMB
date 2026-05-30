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

    private let runner: MaintenanceOperationRunner
    private var plannedOptions: MaintenanceOptions?
    private var plannedTargetID: FsckTargetViewModel.ID?
    private var latestOptions: MaintenanceOptions?

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        self.runner = MaintenanceOperationRunner(
            backend: backend,
            coordinator: coordinator,
            laneKey: laneKey,
            onEvent: { _, _ in },
            onRunningChanged: {}
        )
        self.runner.rebind(onEvent: { [weak self] event, operation in
            self?.handle(event, activeOperation: operation)
        }, onRunningChanged: { [weak self] in
            self?.objectWillChange.send()
        })
    }

    var events: [BackendEvent] { runner.events }
    var isRunning: Bool { runner.isRunning }
    var isBusy: Bool { runner.isBusy }
    var canCancel: Bool { runner.canCancel }
    var pendingConfirmation: PendingConfirmation? { runner.pendingConfirmation }

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
        runner.confirmPending()
    }

    func cancelPendingConfirmation(options: MaintenanceOptions?) {
        latestOptions = options
        runner.cancelPendingConfirmation()
        restoreStateAfterCancellation(options: options)
    }

    func cancel() {
        runner.cancel()
    }

    func clear() {
        runner.clear()
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
            operation: "fsck",
            params: OperationParams.fsckList(mountWait: Double(mountWaitValue)),
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
            operation: "fsck",
            params: OperationParams.fsckPlan(
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
            operation: "fsck",
            params: OperationParams.fsckRun(
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
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String?
    ) -> OperationStartResult {
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        resetRunState()
        let start = runner.start(operation: operation, params: params, profile: profile, password: password)
        if case .rejected(let message) = start {
            rejectRun(message: message)
        }
        return start
    }

    private func resetRunState() {
        runner.resetForRun()
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "fsck" else {
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
            runner.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func handlePlanResult(_ event: BackendEvent) {
        do {
            plan = try event.decodePayload(FsckPlanPayload.self)
            state = .planReady
            error = nil
            runner.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func handleRunResult(_ event: BackendEvent) {
        do {
            result = try event.decodePayload(FsckResultPayload.self)
            state = .succeeded
            error = nil
            runner.finishObserver()
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
            runner.finishObserver()
            restoreStateAfterCancellation(options: latestOptions)
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        runner.finishObserver()
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
        error = BackendErrorViewModel(operation: "fsck", localError: localError)
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "fsck",
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        runner.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = BackendErrorViewModel(
            operation: "fsck",
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
        state = .failed
        runner.finishObserver()
    }

    private func failLocally(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "fsck", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "fsck", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(message: String) {
        error = BackendErrorViewModel(operation: "fsck", code: "operation_rejected", message: message)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }
}
