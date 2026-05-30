import Combine
import Foundation

@MainActor
final class RepairXattrsStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var scan: RepairXattrsPayload?
    @Published private(set) var result: RepairXattrsPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let runner: MaintenanceOperationRunner
    private var scannedPath: String?
    private var scannedOptions: RepairXattrsOptions?
    private var latestPath: String?
    private var latestOptions: RepairXattrsOptions?

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

    func canScan(path: String, options: RepairXattrsOptions?) -> Bool {
        return !isBusy && !path.isEmpty && options != nil
    }

    func canRepair(path: String, options: RepairXattrsOptions?) -> Bool {
        return !isBusy
            && state == .scanReady
            && scan?.repairableCount ?? 0 > 0
            && scannedPath == path
            && scannedOptions == options
    }

    func markScanStaleIfNeeded(path: String, options: RepairXattrsOptions?) {
        latestPath = path
        latestOptions = options
        if state == .scanReady,
           scannedPath != path || scannedOptions != options {
            state = .scanStale
        }
    }

    func confirmPending() {
        runner.confirmPending()
    }

    func cancelPendingConfirmation(path: String, options: RepairXattrsOptions?) {
        latestPath = path
        latestOptions = options
        runner.cancelPendingConfirmation()
        restoreStateAfterCancellation(path: path, options: options)
    }

    func cancel() {
        runner.cancel()
    }

    func clear() {
        runner.clear()
        state = .idle
        scan = nil
        result = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
        scannedPath = nil
        scannedOptions = nil
        latestPath = nil
        latestOptions = nil
    }

    @discardableResult
    func scanRepairXattrs(path: String, options: RepairXattrsOptions?) -> OperationStartResult {
        latestPath = path
        latestOptions = options
        guard let options else {
            failLocally(.repairXattrsDepthInvalid)
            return .rejected(WorkflowLocalError.repairXattrsDepthInvalid.message)
        }
        guard !path.isEmpty else {
            failLocally(.repairXattrsPathRequired)
            return .rejected(WorkflowLocalError.repairXattrsPathRequired.message)
        }
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsScan(path: path, options: options),
            profile: nil,
            password: nil
        )
        guard case .started = start else {
            return start
        }
        state = .scanning
        scan = nil
        result = nil
        scannedPath = path
        scannedOptions = options
        return start
    }

    @discardableResult
    func runRepairXattrs(path: String, options: RepairXattrsOptions?) -> OperationStartResult {
        latestPath = path
        latestOptions = options
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard canRepair(path: path, options: options), let scannedOptions else {
            state = .scanStale
            error = BackendErrorViewModel(operation: "repair-xattrs", localError: .repairXattrsScanStale)
            return .rejected(WorkflowLocalError.repairXattrsScanStale.message)
        }
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsRun(path: path, options: scannedOptions),
            profile: nil,
            password: nil
        )
        guard case .started = start else {
            return start
        }
        state = .repairing
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
        guard event.operation == "repair-xattrs" else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .repairing
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

        do {
            let payload = try event.decodePayload(RepairXattrsPayload.self)
            if state == .scanning {
                scan = payload
                state = .scanReady
            } else {
                result = payload
                state = .repaired
            }
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
            restoreStateAfterCancellation(path: latestPath ?? "", options: latestOptions)
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        runner.finishObserver()
    }

    private func restoreStateAfterCancellation(path: String, options: RepairXattrsOptions?) {
        guard scan != nil else {
            state = .idle
            return
        }
        state = scannedPath == path && scannedOptions == options ? .scanReady : .scanStale
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "repair-xattrs",
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        runner.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = BackendErrorViewModel(
            operation: "repair-xattrs",
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
        state = .failed
        runner.finishObserver()
    }

    private func failLocally(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "repair-xattrs", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "repair-xattrs", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(message: String) {
        error = BackendErrorViewModel(operation: "repair-xattrs", code: "operation_rejected", message: message)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }
}
