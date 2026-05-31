import Combine
import Foundation

@MainActor
final class ActivationStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var plan: ActivationPlanPayload?
    @Published private(set) var result: ActivationResultPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let operation: MaintenanceWorkflowOperation

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        self.operation = MaintenanceWorkflowOperation(
            name: "activate",
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

    var canPlan: Bool {
        !isBusy
    }

    var canRun: Bool {
        !isBusy && plan != nil && state == .planReady
    }

    func confirmPending() {
        operation.confirmPending()
    }

    func cancelPendingConfirmation() {
        operation.cancelPendingConfirmation()
    }

    func cancel() {
        operation.cancel()
    }

    func clear() {
        operation.clear()
        state = .idle
        plan = nil
        result = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
    }

    @discardableResult
    func planActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        let start = startRun(
            params: OperationParams.Activation.params(dryRun: true),
            profile: profile,
            password: password
        )
        guard case .started = start else {
            return start
        }
        state = .planning
        plan = nil
        result = nil
        return start
    }

    @discardableResult
    func runActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard canRun else {
            failLocally(.activationPlanRequired)
            return .rejected(WorkflowLocalError.activationPlanRequired.message)
        }
        let start = startRun(
            params: OperationParams.Activation.params(dryRun: false),
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

        if state == .planning {
            do {
                plan = try event.decodePayload(ActivationPlanPayload.self)
                state = .planReady
                operation.finishObserver()
            } catch {
                failContract(error)
            }
            return
        }

        do {
            result = try event.decodePayload(ActivationResultPayload.self)
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
            state = plan == nil ? .idle : .planReady
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        operation.finishObserver()
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
