import Combine
import Foundation

struct DeployOptions: Equatable {
    let nbnsEnabled: Bool
    let noReboot: Bool
    let noWait: Bool
    let internalShareUseDiskRoot: Bool
    let anyProtocol: Bool
    let debugLogging: Bool
    let ataIdleSeconds: Int
    let ataStandby: Int?
    let mountWait: Int

    init(
        nbnsEnabled: Bool,
        noReboot: Bool,
        noWait: Bool,
        internalShareUseDiskRoot: Bool,
        anyProtocol: Bool,
        debugLogging: Bool,
        ataIdleSeconds: Int = DeviceProfileSettings.default.ataIdleSeconds,
        ataStandby: Int? = DeviceProfileSettings.default.ataStandby,
        mountWait: Int
    ) {
        self.nbnsEnabled = nbnsEnabled
        self.noReboot = noReboot
        self.noWait = noWait
        self.internalShareUseDiskRoot = internalShareUseDiskRoot
        self.anyProtocol = anyProtocol
        self.debugLogging = debugLogging
        self.ataIdleSeconds = ataIdleSeconds
        self.ataStandby = ataStandby
        self.mountWait = mountWait
    }
}

enum DeployExecutionOptionPolicy {
    static func allowsNoReboot(noWait: Bool) -> Bool {
        !noWait
    }

    static func allowsNoWait(noReboot: Bool) -> Bool {
        !noReboot
    }

    static func effectiveRebootOptions(noReboot: Bool, noWait: Bool) -> (noReboot: Bool, noWait: Bool) {
        if noReboot {
            return (true, false)
        }
        return (false, noWait)
    }
}

enum DeployWorkflowState: String, CaseIterable, Equatable, Codable {
    case idle
    case planning
    case planReady
    case planStale
    case planFailed
    case deploying
    case awaitingConfirmation
    case deployed
    case deployFailed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("workflow.state.idle")
        case .planning:
            return L10n.string("workflow.state.planning")
        case .planReady:
            return L10n.string("workflow.state.plan_ready")
        case .planStale:
            return L10n.string("workflow.state.plan_stale")
        case .planFailed:
            return L10n.string("workflow.state.plan_failed")
        case .deploying:
            return L10n.string("workflow.state.deploying")
        case .awaitingConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .deployed:
            return L10n.string("workflow.state.deployed")
        case .deployFailed:
            return L10n.string("workflow.state.deploy_failed")
        }
    }
}

@MainActor
final class DeployWorkflowStore: ObservableObject {
    @Published var nbnsEnabled = true {
        didSet { reconcilePlanFreshness() }
    }
    @Published var noReboot = false {
        didSet {
            if noReboot && noWait {
                noWait = false
            }
            reconcilePlanFreshness()
        }
    }
    @Published var noWait = false {
        didSet {
            if noWait && noReboot {
                noReboot = false
            }
            reconcilePlanFreshness()
        }
    }
    @Published var internalShareUseDiskRoot = false {
        didSet { reconcilePlanFreshness() }
    }
    @Published var anyProtocol = false {
        didSet { reconcilePlanFreshness() }
    }
    @Published var debugLogging = false {
        didSet { reconcilePlanFreshness() }
    }
    @Published var ataIdleSeconds = String(DeviceProfileSettings.default.ataIdleSeconds) {
        didSet { reconcilePlanFreshness() }
    }
    @Published var ataStandby = DeviceProfileSettings.default.ataStandby.map { String($0) } ?? "" {
        didSet { reconcilePlanFreshness() }
    }
    @Published var mountWait = "30" {
        didSet { reconcilePlanFreshness() }
    }

    @Published private(set) var state: DeployWorkflowState = .idle
    @Published private(set) var plan: DeployPlanPayload?
    @Published private(set) var result: DeployResultPayload?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var plannedOptions: DeployOptions?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    let backend: BackendClient
    private let coordinator: OperationCoordinator?
    private let laneKey: OperationLaneKey?

    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
        self.coordinator = nil
        self.laneKey = nil
        observeBackend(backend)
    }

    convenience init(coordinator: OperationCoordinator) {
        self.init(coordinator: coordinator, laneKey: .app)
    }

    init(coordinator: OperationCoordinator, laneKey: OperationLaneKey) {
        let lane = coordinator.lane(for: laneKey)
        self.backend = lane.backend
        self.coordinator = coordinator
        self.laneKey = laneKey
        observeBackend(lane.backend)
    }

    private func observeBackend(_ backend: BackendClient) {
        backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
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

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var hasValidOptions: Bool {
        deployOptionsValidationMessage == nil
    }

    var canDeploy: Bool {
        !isBusy && state == .planReady && plan != nil && currentOptions == plannedOptions
    }

    @discardableResult
    func runPlan(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            let localError = deployOptionsValidationError ?? .deployOptionsInvalid
            failLocally(state: .planFailed, localError: localError)
            return .rejected(localError.message)
        }
        guard !isBusy else {
            rejectRun(state: .planFailed, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        backend.clear()
        let start = run(
            operation: "deploy",
            params: OperationParams.deployPlan(
                noReboot: options.noReboot,
                noWait: options.noWait,
                nbnsEnabled: options.nbnsEnabled,
                internalShareUseDiskRoot: options.internalShareUseDiskRoot,
                anyProtocol: options.anyProtocol,
                debugLogging: options.debugLogging,
                ataIdleSeconds: options.ataIdleSeconds,
                ataStandby: options.ataStandby,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile
        )
        guard case .started(let operation) = start else {
            if let message = start.rejectionMessage {
                rejectRun(state: .planFailed, message: message)
            } else {
                rejectRun(state: .planFailed, localError: .operationCouldNotStart)
            }
            return start
        }
        operationObserver.start(operation)
        state = .planning
        plan = nil
        result = nil
        error = nil
        currentStage = nil
        plannedOptions = options
        passwordInvalidProfileID = nil
        process(backend.events)
        return start
    }

    @discardableResult
    func runDeploy(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = plannedOptions, plan != nil, currentOptions == options else {
            state = .planStale
            error = BackendErrorViewModel(operation: "deploy", localError: .deployPlanStale)
            return .rejected(WorkflowLocalError.deployPlanStale.message)
        }
        guard state == .planReady else {
            return .rejected(WorkflowLocalError.deployPlanNotReady.message)
        }
        guard !isBusy else {
            rejectRun(state: .deployFailed, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        backend.clear()
        let start = run(
            operation: "deploy",
            params: OperationParams.deployRun(
                noReboot: options.noReboot,
                noWait: options.noWait,
                nbnsEnabled: options.nbnsEnabled,
                internalShareUseDiskRoot: options.internalShareUseDiskRoot,
                anyProtocol: options.anyProtocol,
                debugLogging: options.debugLogging,
                ataIdleSeconds: options.ataIdleSeconds,
                ataStandby: options.ataStandby,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile
        )
        guard case .started(let operation) = start else {
            if let message = start.rejectionMessage {
                rejectRun(state: .deployFailed, message: message)
            } else {
                rejectRun(state: .deployFailed, localError: .operationCouldNotStart)
            }
            return start
        }
        operationObserver.start(operation)
        state = .deploying
        result = nil
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        process(backend.events)
        return start
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
        state = .idle
        plan = nil
        result = nil
        error = nil
        currentStage = nil
        plannedOptions = nil
        passwordInvalidProfileID = nil
        operationObserver.finish()
    }

    func cancel() {
        backend.cancel()
    }

    private var currentOptions: DeployOptions? {
        guard let mountWaitValue, let ataIdleSecondsValue, hasValidAtaStandby else {
            return nil
        }
        let rebootOptions = DeployExecutionOptionPolicy.effectiveRebootOptions(noReboot: noReboot, noWait: noWait)
        return DeployOptions(
            nbnsEnabled: nbnsEnabled,
            noReboot: rebootOptions.noReboot,
            noWait: rebootOptions.noWait,
            internalShareUseDiskRoot: internalShareUseDiskRoot,
            anyProtocol: anyProtocol,
            debugLogging: debugLogging,
            ataIdleSeconds: ataIdleSecondsValue,
            ataStandby: ataStandbyValue,
            mountWait: mountWaitValue
        )
    }

    private var ataIdleSecondsValue: Int? {
        ValueParsers.nonNegativeInteger(ataIdleSeconds)
    }

    private var ataStandbyValue: Int? {
        let trimmed = ataStandby.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        return ValueParsers.nonNegativeInteger(trimmed)
    }

    private var hasValidAtaStandby: Bool {
        ataStandby.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || ataStandbyValue != nil
    }

    private var deployOptionsValidationError: WorkflowLocalError? {
        if mountWaitValue == nil {
            return .mountWaitInvalid
        }
        if ataIdleSecondsValue == nil {
            return .ataIdleSecondsInvalid
        }
        if !hasValidAtaStandby {
            return .ataStandbyInvalid
        }
        return nil
    }

    private var deployOptionsValidationMessage: String? {
        deployOptionsValidationError?.message
    }

    private func reconcilePlanFreshness() {
        guard plan != nil, state == .planReady || state == .planStale else {
            return
        }
        if currentOptions == plannedOptions {
            state = .planReady
            if error?.code == WorkflowLocalError.deployPlanStale.code {
                error = nil
            }
        } else {
            state = .planStale
        }
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "deploy" else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .deploying
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
            applyFailureResult(event)
            return
        }

        switch state {
        case .planning:
            applyPlanResult(event)
        case .deploying, .awaitingConfirmation:
            applyDeployResult(event)
        default:
            break
        }
    }

    private func applyPlanResult(_ event: BackendEvent) {
        do {
            plan = try event.decodePayload(DeployPlanPayload.self)
            result = nil
            error = nil
            operationObserver.finish()
            state = .planReady
            reconcilePlanFreshness()
        } catch {
            failContract(state: .planFailed, error: error)
        }
    }

    private func applyDeployResult(_ event: BackendEvent) {
        do {
            result = try event.decodePayload(DeployResultPayload.self)
            error = nil
            state = .deployed
            operationObserver.finish()
        } catch {
            failContract(state: .deployFailed, error: error)
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled()
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = state == .planning ? .planFailed : .deployFailed
        operationObserver.finish()
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        operationObserver.finish()
        guard plan != nil else {
            state = .idle
            return
        }
        state = .planReady
        reconcilePlanFreshness()
    }

    private func applyFailureResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = state == .planning ? .planFailed : .deployFailed
        operationObserver.finish()
    }

    private func failContract(state: DeployWorkflowState, error: Error) {
        self.error = BackendErrorViewModel(
            operation: "deploy",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        self.state = state
        operationObserver.finish()
    }

    private func failLocally(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func failLocally(state: DeployWorkflowState, localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "deploy", localError: localError)
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func rejectRun(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func rejectRun(state: DeployWorkflowState, localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "deploy", localError: localError)
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func run(operation: String, params: [String: JSONValue], profile: DeviceProfile?) -> OperationStartResult {
        if let coordinator {
            return coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                laneKey: laneKey
            )
        } else {
            guard !isBusy else {
                return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
            }
            let context = profile?.runtimeContext
            let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
            backend.run(
                operation: operation,
                params: params,
                context: context,
                requestID: activeOperation.id.uuidString
            )
            return .started(activeOperation)
        }
    }
}
