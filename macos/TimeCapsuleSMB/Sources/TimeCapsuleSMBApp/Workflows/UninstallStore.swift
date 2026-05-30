import Combine
import Foundation

@MainActor
final class UninstallStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var plan: UninstallPlanPayload?
    @Published private(set) var result: MaintenanceResultPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let runner: MaintenanceOperationRunner
    private var plannedOptions: MaintenanceOptions?
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

    func canPlan(options: MaintenanceOptions?) -> Bool {
        return !isBusy && options != nil
    }

    func canRun(options: MaintenanceOptions?) -> Bool {
        return !isBusy && plan != nil && state == .planReady && options == plannedOptions
    }

    func markPlanStaleIfNeeded(options: MaintenanceOptions?) {
        latestOptions = options
        if state == .planReady, options != plannedOptions {
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
        plan = nil
        result = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
        plannedOptions = nil
        latestOptions = nil
    }

    @discardableResult
    func planUninstall(
        options: MaintenanceOptions?,
        password: String,
        profile: DeviceProfile? = nil
    ) -> OperationStartResult {
        latestOptions = options
        guard let options else {
            failLocally(.mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
        }
        let start = startRun(
            operation: "uninstall",
            params: OperationParams.uninstallPlan(
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
        return start
    }

    @discardableResult
    func runUninstall(
        options: MaintenanceOptions?,
        password: String,
        profile: DeviceProfile? = nil
    ) -> OperationStartResult {
        latestOptions = options
        guard !isBusy else {
            return rejectAlreadyRunning()
        }
        guard let currentOptions = options,
              let plannedOptions,
              currentOptions == plannedOptions,
              plan != nil else {
            markStale(.uninstallPlanStale)
            return .rejected(WorkflowLocalError.uninstallPlanStale.message)
        }
        guard state == .planReady else {
            return .rejected(WorkflowLocalError.uninstallPlanNotReady.message)
        }
        let start = startRun(
            operation: "uninstall",
            params: OperationParams.uninstallRun(
                noReboot: currentOptions.noReboot,
                noWait: currentOptions.noWait,
                mountWait: Double(currentOptions.mountWait)
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
        guard event.operation == "uninstall" else {
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
                plan = try event.decodePayload(UninstallPlanPayload.self)
                state = .planReady
                runner.finishObserver()
            } catch {
                failContract(error)
            }
            return
        }

        do {
            result = try event.decodePayload(MaintenanceResultPayload.self)
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
            state = .idle
            return
        }
        state = options == plannedOptions ? .planReady : .planStale
    }

    private func markStale(_ localError: WorkflowLocalError) {
        state = .planStale
        error = BackendErrorViewModel(operation: "uninstall", localError: localError)
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "uninstall",
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        runner.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = BackendErrorViewModel(
            operation: "uninstall",
            code: "contract_decode_failed",
            message: decodeError.localizedDescription
        )
        state = .failed
        runner.finishObserver()
    }

    private func failLocally(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "uninstall", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "uninstall", localError: localError)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }

    private func rejectRun(message: String) {
        error = BackendErrorViewModel(operation: "uninstall", code: "operation_rejected", message: message)
        currentStage = nil
        state = .failed
        runner.finishObserver()
    }
}
