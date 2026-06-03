import Combine
import Foundation

@MainActor
final class MaintenanceOperationRunner {
    let backend: BackendClient

    private let coordinator: OperationCoordinator?
    private let laneKey: OperationLaneKey?
    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []
    private var eventHandler: (BackendEvent, ActiveOperation) -> Void
    private var runningChangedHandler: () -> Void

    init(
        backend: BackendClient,
        coordinator: OperationCoordinator?,
        laneKey: OperationLaneKey?,
        onEvent: @escaping (BackendEvent, ActiveOperation) -> Void,
        onRunningChanged: @escaping () -> Void
    ) {
        self.backend = backend
        self.coordinator = coordinator
        self.laneKey = laneKey
        self.eventHandler = onEvent
        self.runningChangedHandler = onRunningChanged

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
                self?.runningChangedHandler()
            }
            .store(in: &cancellables)
    }

    func rebind(
        onEvent: @escaping (BackendEvent, ActiveOperation) -> Void,
        onRunningChanged: @escaping () -> Void
    ) {
        self.eventHandler = onEvent
        self.runningChangedHandler = onRunningChanged
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

    var canCancel: Bool {
        backend.canCancel
    }

    var pendingConfirmation: PendingConfirmation? {
        backend.pendingConfirmation
    }

    func confirmPending() {
        backend.confirmPending()
    }

    func cancelPendingConfirmation() {
        backend.cancelPendingConfirmation()
    }

    func cancel() {
        backend.cancel()
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
        operationObserver.finish()
    }

    func resetForRun() {
        backend.clear()
        operationObserver.clear()
        operationObserver.finish()
    }

    func finishObserver() {
        operationObserver.finish()
    }

    @discardableResult
    func start(
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String? = nil
    ) -> OperationStartResult {
        if let coordinator {
            let start = coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                password: password,
                laneKey: laneKey
            )
            if case .started(let operation) = start {
                operationObserver.start(operation)
            }
            return start
        }

        guard !isBusy else {
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }

        let context = profile?.runtimeContext
        let updatedParams = OperationCredentialInjector.injectingPassword(password, into: params)
        let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
        backend.run(
            operation: operation,
            params: updatedParams,
            context: context,
            requestID: activeOperation.id.uuidString
        )
        operationObserver.start(activeOperation)
        return .started(activeOperation)
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            eventHandler(event, operation)
        }
    }
}
