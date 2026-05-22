import Combine
import Foundation

struct DeployOptions: Equatable {
    let nbnsEnabled: Bool
    let noReboot: Bool
    let noWait: Bool
    let internalShareUseDiskRoot: Bool
    let anyProtocol: Bool
    let debugLogging: Bool
    let mountWait: Int
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
            return "Idle"
        case .planning:
            return "Planning"
        case .planReady:
            return "Plan Ready"
        case .planStale:
            return "Plan Stale"
        case .planFailed:
            return "Plan Failed"
        case .deploying:
            return "Deploying"
        case .awaitingConfirmation:
            return "Awaiting Confirmation"
        case .deployed:
            return "Deployed"
        case .deployFailed:
            return "Deploy Failed"
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

    private var activeOperation: ActiveOperation?
    private var lastProcessedEventCount = 0
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

    var canDeploy: Bool {
        !isBusy && state == .planReady && plan != nil && currentOptions == plannedOptions
    }

    @discardableResult
    func runPlan(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(state: .planFailed, message: "Mount wait must be a non-negative integer.")
            return .rejected("Mount wait must be a non-negative integer.")
        }
        guard !isBusy else {
            rejectRun(state: .planFailed, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
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
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile
        )
        guard case .started(let operation) = start else {
            rejectRun(state: .planFailed, message: start.rejectionMessage ?? "Operation could not start.")
            return start
        }
        lastProcessedEventCount = 0
        activeOperation = operation
        state = .planning
        plan = nil
        result = nil
        error = nil
        currentStage = nil
        plannedOptions = options
        passwordInvalidProfileID = nil
        return start
    }

    @discardableResult
    func runDeploy(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = plannedOptions, plan != nil, currentOptions == options else {
            state = .planStale
            error = BackendErrorViewModel(
                operation: "deploy",
                code: "plan_stale",
                message: "Review and regenerate the deploy plan before deploying."
            )
            return .rejected("Review and regenerate the deploy plan before deploying.")
        }
        guard state == .planReady else {
            return .rejected("Deploy plan is not ready.")
        }
        guard !isBusy else {
            rejectRun(state: .deployFailed, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
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
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile
        )
        guard case .started(let operation) = start else {
            rejectRun(state: .deployFailed, message: start.rejectionMessage ?? "Operation could not start.")
            return start
        }
        lastProcessedEventCount = 0
        activeOperation = operation
        state = .deploying
        result = nil
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        return start
    }

    func clear() {
        backend.clear()
        lastProcessedEventCount = 0
        state = .idle
        plan = nil
        result = nil
        error = nil
        currentStage = nil
        plannedOptions = nil
        passwordInvalidProfileID = nil
        activeOperation = nil
    }

    func cancel() {
        backend.cancel()
    }

    private var currentOptions: DeployOptions? {
        guard let mountWaitValue else {
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
            mountWait: mountWaitValue
        )
    }

    private func reconcilePlanFreshness() {
        guard plan != nil, state == .planReady || state == .planStale else {
            return
        }
        if currentOptions == plannedOptions {
            state = .planReady
            if error?.code == "plan_stale" {
                error = nil
            }
        } else {
            state = .planStale
        }
    }

    private func process(_ events: [BackendEvent]) {
        if events.count < lastProcessedEventCount {
            lastProcessedEventCount = 0
        }
        guard events.count > lastProcessedEventCount else {
            return
        }
        for event in events.dropFirst(lastProcessedEventCount) {
            handle(event)
        }
        lastProcessedEventCount = events.count
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "deploy" else {
            return
        }
        guard activeOperation?.operation == event.operation else {
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
            applyError(event)
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
            activeOperation = nil
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
            activeOperation = nil
        } catch {
            failContract(state: .deployFailed, error: error)
        }
    }

    private func applyError(_ event: BackendEvent) {
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
            passwordInvalidProfileID = activeOperation?.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = state == .planning ? .planFailed : .deployFailed
        activeOperation = nil
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        activeOperation = nil
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
            message: event.payloadSummaryText ?? event.summary
        )
        state = state == .planning ? .planFailed : .deployFailed
        activeOperation = nil
    }

    private func failContract(state: DeployWorkflowState, error: Error) {
        self.error = BackendErrorViewModel(
            operation: "deploy",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        self.state = state
        activeOperation = nil
    }

    private func failLocally(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        self.state = state
        activeOperation = nil
    }

    private func rejectRun(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        self.state = state
        activeOperation = nil
    }

    private func run(operation: String, params: [String: JSONValue], profile: DeviceProfile?) -> OperationStartResult {
        if let coordinator {
            return coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                laneKey: laneKey ?? profile.map { .device($0.id) } ?? .app
            )
        } else {
            guard !isBusy else {
                return .rejected("Another operation is already running.")
            }
            let context = profile?.runtimeContext
            let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
            backend.run(operation: operation, params: params, context: profile?.runtimeContext)
            return .started(activeOperation)
        }
    }
}
