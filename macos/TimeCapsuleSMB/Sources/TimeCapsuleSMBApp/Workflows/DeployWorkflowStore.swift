import Combine
import Foundation

struct DeployOptions: Equatable {
    let nbnsEnabled: Bool
    let noReboot: Bool
    let noWait: Bool
    let debugLogging: Bool
    let mountWait: Int
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
        didSet { markPlanStaleIfNeeded() }
    }
    @Published var noReboot = false {
        didSet { markPlanStaleIfNeeded() }
    }
    @Published var noWait = false {
        didSet { markPlanStaleIfNeeded() }
    }
    @Published var debugLogging = false {
        didSet { markPlanStaleIfNeeded() }
    }
    @Published var mountWait = "30" {
        didSet { markPlanStaleIfNeeded() }
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

    private var activeOperation: ActiveOperation?
    private var lastProcessedEventCount = 0
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
        self.coordinator = nil
        observeBackend(backend)
    }

    init(coordinator: OperationCoordinator) {
        self.backend = coordinator.backend
        self.coordinator = coordinator
        observeBackend(coordinator.backend)
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

    var canCancel: Bool {
        backend.canCancel
    }

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var canDeploy: Bool {
        !backend.isRunning && state == .planReady && plan != nil && currentOptions == plannedOptions
    }

    @discardableResult
    func runPlan(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(state: .planFailed, message: "Mount wait must be a non-negative integer.")
            return .rejected("Mount wait must be a non-negative integer.")
        }
        guard !backend.isRunning else {
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
        guard !backend.isRunning else {
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
        return DeployOptions(
            nbnsEnabled: nbnsEnabled,
            noReboot: noReboot,
            noWait: noWait,
            debugLogging: debugLogging,
            mountWait: mountWaitValue
        )
    }

    private func markPlanStaleIfNeeded() {
        guard state == .planReady, currentOptions != plannedOptions else {
            return
        }
        state = .planStale
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
            state = .planReady
            activeOperation = nil
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
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation?.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = state == .planning ? .planFailed : .deployFailed
        activeOperation = nil
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
            return coordinator.run(operation: operation, params: params, profile: profile)
        } else {
            guard !backend.isRunning else {
                return .rejected("Another operation is already running.")
            }
            let context = profile?.runtimeContext
            let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
            backend.run(operation: operation, params: params, context: profile?.runtimeContext)
            return .started(activeOperation)
        }
    }
}
