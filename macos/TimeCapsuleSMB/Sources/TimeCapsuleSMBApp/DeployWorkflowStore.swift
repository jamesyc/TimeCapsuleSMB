import Combine
import Foundation

struct DeployOptions: Equatable {
    let nbnsEnabled: Bool
    let noReboot: Bool
    let noWait: Bool
    let debugLogging: Bool
    let mountWait: Int
}

enum DeployWorkflowState: String, CaseIterable, Equatable {
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

    let backend: BackendClient

    private var lastProcessedEventCount = 0
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
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
        nonNegativeInteger(mountWait)
    }

    var canDeploy: Bool {
        !backend.isRunning && state == .planReady && plan != nil && currentOptions == plannedOptions
    }

    func runPlan(password: String) {
        guard let options = currentOptions else {
            failLocally(state: .planFailed, message: "Mount wait must be a non-negative integer.")
            return
        }
        backend.clear()
        lastProcessedEventCount = 0
        state = .planning
        plan = nil
        result = nil
        error = nil
        currentStage = nil
        plannedOptions = options
        backend.run(
            operation: "deploy",
            params: OperationParams.deployPlan(
                noReboot: options.noReboot,
                noWait: options.noWait,
                nbnsEnabled: options.nbnsEnabled,
                debugLogging: options.debugLogging,
                mountWait: Double(options.mountWait),
                password: password
            )
        )
    }

    func runDeploy(password: String) {
        guard let options = plannedOptions, plan != nil, currentOptions == options else {
            state = .planStale
            error = BackendErrorViewModel(
                operation: "deploy",
                code: "plan_stale",
                message: "Review and regenerate the deploy plan before deploying."
            )
            return
        }
        guard state == .planReady else {
            return
        }
        backend.clear()
        lastProcessedEventCount = 0
        state = .deploying
        result = nil
        error = nil
        currentStage = nil
        backend.run(
            operation: "deploy",
            params: OperationParams.deployRun(
                noReboot: options.noReboot,
                noWait: options.noWait,
                nbnsEnabled: options.nbnsEnabled,
                debugLogging: options.debugLogging,
                mountWait: Double(options.mountWait),
                password: password
            )
        )
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
        } catch {
            failContract(state: .planFailed, error: error)
        }
    }

    private func applyDeployResult(_ event: BackendEvent) {
        do {
            result = try event.decodePayload(DeployResultPayload.self)
            error = nil
            state = .deployed
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
        error = BackendErrorViewModel(event: event)
        state = state == .planning ? .planFailed : .deployFailed
    }

    private func applyFailureResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "operation_failed",
            message: event.payloadSummaryText ?? event.summary
        )
        state = state == .planning ? .planFailed : .deployFailed
    }

    private func failContract(state: DeployWorkflowState, error: Error) {
        self.error = BackendErrorViewModel(
            operation: "deploy",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        self.state = state
    }

    private func failLocally(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        self.state = state
    }

    private func nonNegativeInteger(_ text: String) -> Int? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Int(trimmed), value >= 0 else {
            return nil
        }
        return value
    }
}
