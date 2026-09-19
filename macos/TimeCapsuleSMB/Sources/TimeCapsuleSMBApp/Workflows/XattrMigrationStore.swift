import Combine
import Foundation

@MainActor
final class XattrMigrationStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var payload: XattrMigrationPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let operation: MaintenanceWorkflowOperation

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        operation = MaintenanceWorkflowOperation(
            name: "migrate-xattr",
            backend: backend,
            coordinator: coordinator,
            laneKey: laneKey
        )
        operation.bind(onEvent: { [weak self] event, activeOperation in
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
    var canRefresh: Bool { !isBusy }
    var canStart: Bool { !isBusy && payload?.state != "running" }
    var canRequestCancel: Bool { !isBusy && payload?.state == "running" }

    func confirmPending() { operation.confirmPending() }
    func cancelPendingConfirmation() { operation.cancelPendingConfirmation() }
    func cancel() { operation.cancel() }

    func clear() {
        operation.clear()
        state = .idle
        payload = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
    }

    func refresh(password: String, profile: DeviceProfile?) -> OperationStartResult {
        start(params: OperationParams.XattrMigration.status(), password: password, profile: profile)
    }

    func run(mountWait: Int, password: String, profile: DeviceProfile?) -> OperationStartResult {
        start(params: OperationParams.XattrMigration.start(mountWait: mountWait), password: password, profile: profile)
    }

    func requestCancel(password: String, profile: DeviceProfile?) -> OperationStartResult {
        start(params: OperationParams.XattrMigration.cancel(), password: password, profile: profile)
    }

    @discardableResult
    func rejectAlreadyRunning() -> OperationStartResult {
        let message = WorkflowLocalError.operationAlreadyRunning.message
        error = operation.localError(.operationAlreadyRunning)
        state = .failed
        return .rejected(message)
    }

    private func start(
        params: [String: JSONValue],
        password: String,
        profile: DeviceProfile?
    ) -> OperationStartResult {
        operation.start(
            params: params,
            profile: profile,
            password: password,
            rejectAlreadyRunning: { _ = rejectAlreadyRunning() },
            resetRunState: {
                operation.resetForRun()
                error = nil
                currentStage = nil
                passwordInvalidProfileID = nil
                state = .loading
            },
            rejectRun: { message in
                error = operation.rejectedError(message: message)
                state = .failed
            }
        )
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == operation.name else { return }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation { state = .running }
            return
        }
        if event.type == "error" {
            if event.code == "confirmation_required" {
                error = nil
                state = .awaitingConfirmation
                return
            }
            if event.code == "auth_failed" { passwordInvalidProfileID = activeOperation.profileID }
            error = BackendErrorViewModel(event: event)
            state = .failed
            operation.finishObserver()
            return
        }
        guard event.type == "result", event.ok != false else { return }
        do {
            payload = try event.decodePayload(XattrMigrationPayload.self)
            state = payload?.state == "running" ? .running : .succeeded
            error = nil
            operation.finishObserver()
        } catch {
            self.error = operation.contractDecodeError(error)
            state = .failed
            operation.finishObserver()
        }
    }
}
