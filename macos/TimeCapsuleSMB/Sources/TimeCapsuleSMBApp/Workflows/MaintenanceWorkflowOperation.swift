import Foundation

@MainActor
final class MaintenanceWorkflowOperation {
    let name: String

    private let runner: MaintenanceOperationRunner

    init(
        name: String,
        backend: BackendClient,
        coordinator: OperationCoordinator? = nil,
        laneKey: OperationLaneKey? = nil
    ) {
        self.name = name
        self.runner = MaintenanceOperationRunner(
            backend: backend,
            coordinator: coordinator,
            laneKey: laneKey,
            onEvent: { _, _ in },
            onRunningChanged: {}
        )
    }

    func bind(
        onEvent: @escaping (BackendEvent, ActiveOperation) -> Void,
        onRunningChanged: @escaping () -> Void
    ) {
        runner.rebind(onEvent: onEvent, onRunningChanged: onRunningChanged)
    }

    var events: [BackendEvent] { runner.events }
    var isRunning: Bool { runner.isRunning }
    var isBusy: Bool { runner.isBusy }
    var canCancel: Bool { runner.canCancel }
    var pendingConfirmation: PendingConfirmation? { runner.pendingConfirmation }

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
    }

    func resetForRun() {
        runner.resetForRun()
    }

    func finishObserver() {
        runner.finishObserver()
    }

    @discardableResult
    func start(
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String?,
        rejectAlreadyRunning: () -> Void,
        resetRunState: () -> Void,
        rejectRun: (String) -> Void
    ) -> OperationStartResult {
        guard !isBusy else {
            rejectAlreadyRunning()
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        resetRunState()
        let start = runner.start(operation: name, params: params, profile: profile, password: password)
        if case .rejected(let message) = start {
            rejectRun(message)
        }
        return start
    }

    func localError(_ localError: WorkflowLocalError) -> BackendErrorViewModel {
        BackendErrorViewModel(operation: name, localError: localError)
    }

    func rejectedError(message: String) -> BackendErrorViewModel {
        BackendErrorViewModel(operation: name, code: "operation_rejected", message: message)
    }

    func falseResultError(from event: BackendEvent) -> BackendErrorViewModel {
        BackendErrorViewModel(
            operation: name,
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
    }

    func contractDecodeError(_ decodeError: Error) -> BackendErrorViewModel {
        BackendErrorViewModel(
            operation: name,
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
    }
}
