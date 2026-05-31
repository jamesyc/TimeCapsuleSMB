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

    private let runner: MaintenanceOperationRunner

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

    var canPlan: Bool {
        !isBusy
    }

    var canRun: Bool {
        !isBusy && plan != nil && state == .planReady
    }

    func confirmPending() {
        runner.confirmPending()
    }

    func cancelPendingConfirmation() {
        runner.cancelPendingConfirmation()
    }

    func cancel() {
        runner.cancel()
    }

    func clear() {
        runner.clear()
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
            operation: "activate",
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
            operation: "activate",
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
        guard event.operation == "activate" else {
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
                runner.finishObserver()
            } catch {
                failContract(error)
            }
            return
        }

        do {
            result = try event.decodePayload(ActivationResultPayload.self)
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
            state = plan == nil ? .idle : .planReady
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        runner.finishObserver()
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "activate",
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        runner.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = BackendErrorViewModel(
            operation: "activate",
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
        state = .failed
        runner.finishObserver()
    }

    private func failLocally(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "activate", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "activate", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(message: String) {
        error = BackendErrorViewModel(operation: "activate", code: "operation_rejected", message: message)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }
}
