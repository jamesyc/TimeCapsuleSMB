import Combine
import Foundation

@MainActor
final class MaintenanceWorkflowOperation {
    let name: String

    private let coordinator: OperationCoordinator
    private let lane: OperationLane
    private var backend: BackendClient { lane.backend }
    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []
    private var eventHandler: (BackendEvent, ActiveOperation) -> Void = { _, _ in }
    private var runningChangedHandler: () -> Void = {}

    init(
        name: String,
        backend: BackendClient,
        coordinator: OperationCoordinator? = nil,
        laneKey: OperationLaneKey? = nil
    ) {
        self.name = name
        let coordinator = coordinator ?? OperationCoordinator(backend: backend)
        self.coordinator = coordinator
        self.lane = coordinator.lane(for: laneKey ?? .app)

        self.backend.didUpdateEvents
            .sink { [weak self] events in
                // Consume terminal events before another run can clear this request's history.
                self?.process(events)
            }
            .store(in: &cancellables)
        self.backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.runningChangedHandler()
            }
            .store(in: &cancellables)
    }

    func bind(
        onEvent: @escaping (BackendEvent, ActiveOperation) -> Void,
        onRunningChanged: @escaping () -> Void
    ) {
        eventHandler = onEvent
        runningChangedHandler = onRunningChanged
    }

    var events: [BackendEvent] { backend.events }
    var isRunning: Bool { backend.isRunning }
    var isBusy: Bool { lane.isBusy }
    var canCancel: Bool { lane.canCancel }
    var pendingConfirmation: PendingConfirmation? { backend.pendingConfirmation }

    func confirmPending() {
        lane.confirmPending()
    }

    func cancelPendingConfirmation() {
        lane.cancelPendingConfirmation()
    }

    func cancel() {
        lane.cancel()
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
    }

    func resetForRun() {
        clear()
    }

    func finishObserver() {
        operationObserver.finish()
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
        let start = coordinator.run(
            operation: name,
            params: params,
            context: profile?.runtimeContext,
            activeDeviceID: profile?.id,
            password: password,
            laneKey: lane.key
        )
        switch start {
        case .started(let operation):
            operationObserver.start(operation)
        case .rejected(let message):
            rejectRun(message)
        }
        return start
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            eventHandler(event, operation)
        }
    }

    func localError(_ localError: WorkflowLocalError) -> BackendErrorViewModel {
        BackendErrorViewModel(operation: name, localError: localError)
    }

    func rejectedError(message: String) -> BackendErrorViewModel {
        BackendErrorViewModel(operation: name, code: "operation_rejected", message: message)
    }

    func falseResultError(from event: BackendEvent) -> BackendErrorViewModel {
        BackendErrorViewModel(event: event)
    }

    func contractDecodeError(_ decodeError: Error) -> BackendErrorViewModel {
        BackendErrorViewModel(
            operation: name,
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
    }
}
