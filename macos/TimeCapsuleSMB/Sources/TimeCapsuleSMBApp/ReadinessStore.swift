import Combine
import Foundation

enum ReadinessOperationState: String, CaseIterable, Equatable {
    case idle
    case running
    case succeeded
    case failed

    var title: String {
        switch self {
        case .idle:
            return "Idle"
        case .running:
            return "Running"
        case .succeeded:
            return "Succeeded"
        case .failed:
            return "Failed"
        }
    }
}

@MainActor
final class ReadinessStore: ObservableObject {
    @Published private(set) var capabilitiesState: ReadinessOperationState = .idle
    @Published private(set) var pathsState: ReadinessOperationState = .idle
    @Published private(set) var validationState: ReadinessOperationState = .idle
    @Published private(set) var capabilities: CapabilitiesPayload?
    @Published private(set) var paths: PathsPayload?
    @Published private(set) var validation: InstallValidationPayload?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

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

    func runCapabilities() {
        run(operation: "capabilities")
        capabilitiesState = .running
    }

    func runPaths() {
        run(operation: "paths")
        pathsState = .running
    }

    func runValidateInstall() {
        run(operation: "validate-install")
        validationState = .running
    }

    func clear() {
        backend.clear()
        lastProcessedEventCount = 0
        capabilitiesState = .idle
        pathsState = .idle
        validationState = .idle
        capabilities = nil
        paths = nil
        validation = nil
        error = nil
        currentStage = nil
    }

    func cancel() {
        backend.cancel()
    }

    private func run(operation: String) {
        backend.clear()
        lastProcessedEventCount = 0
        error = nil
        currentStage = nil
        backend.run(operation: operation)
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
        guard ["capabilities", "paths", "validate-install"].contains(event.operation) else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }

        if event.type == "error" {
            applyError(event)
            return
        }

        guard event.type == "result" else {
            return
        }

        switch event.operation {
        case "capabilities":
            applyCapabilitiesResult(event)
        case "paths":
            applyPathsResult(event)
        case "validate-install":
            applyValidationResult(event)
        default:
            break
        }
    }

    private func applyCapabilitiesResult(_ event: BackendEvent) {
        do {
            capabilities = try event.decodePayload(CapabilitiesPayload.self)
            capabilitiesState = event.ok == true ? .succeeded : .failed
            error = event.ok == true ? nil : BackendErrorViewModel(
                operation: event.operation,
                code: "operation_failed",
                message: event.payloadSummaryText ?? event.summary
            )
        } catch {
            failContract(operation: "capabilities", error: error)
        }
    }

    private func applyPathsResult(_ event: BackendEvent) {
        do {
            paths = try event.decodePayload(PathsPayload.self)
            pathsState = event.ok == true ? .succeeded : .failed
            error = event.ok == true ? nil : BackendErrorViewModel(
                operation: event.operation,
                code: "operation_failed",
                message: event.payloadSummaryText ?? event.summary
            )
        } catch {
            failContract(operation: "paths", error: error)
        }
    }

    private func applyValidationResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(InstallValidationPayload.self)
            validation = payload
            validationState = payload.ok ? .succeeded : .failed
            error = nil
        } catch {
            failContract(operation: "validate-install", error: error)
        }
    }

    private func applyError(_ event: BackendEvent) {
        error = BackendErrorViewModel(event: event)
        setState(.failed, for: event.operation)
    }

    private func failContract(operation: String, error: Error) {
        self.error = BackendErrorViewModel(
            operation: operation,
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        setState(.failed, for: operation)
    }

    private func setState(_ state: ReadinessOperationState, for operation: String) {
        switch operation {
        case "capabilities":
            capabilitiesState = state
        case "paths":
            pathsState = state
        case "validate-install":
            validationState = state
        default:
            break
        }
    }
}
