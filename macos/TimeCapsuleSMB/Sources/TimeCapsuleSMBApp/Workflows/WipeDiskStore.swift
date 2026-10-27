import Combine
import Foundation

/// Wipe Internal Disk: one action. The helper finds the internal disk itself
/// (there is nothing to choose) and stops at its confirmation, which names
/// the disk and what will be lost; a disk that cannot be wiped comes back as
/// an error with its guidance. Nothing in the device's disk service is called
/// before the user confirms.
@MainActor
final class WipeDiskStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var result: WipeDiskResultPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let operation: MaintenanceWorkflowOperation

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        self.operation = MaintenanceWorkflowOperation(
            name: "wipe-disk",
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
        result = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
    }

    @discardableResult
    func runWipe(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        let start = operation.start(
            params: OperationParams.WipeDisk.run(),
            profile: profile,
            password: password,
            rejectAlreadyRunning: { rejectRun(.operationAlreadyRunning) },
            resetRunState: resetRunState,
            rejectRun: rejectRun(message:)
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
            error = operation.falseResultError(from: event)
            state = .failed
            operation.finishObserver()
            return
        }
        do {
            result = try event.decodePayload(WipeDiskResultPayload.self)
            state = .succeeded
            error = nil
            operation.finishObserver()
        } catch {
            self.error = operation.contractDecodeError(error)
            state = .failed
            operation.finishObserver()
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
            state = .idle
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
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
