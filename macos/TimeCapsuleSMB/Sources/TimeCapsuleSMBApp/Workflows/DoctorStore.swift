import Combine
import Foundation

enum DoctorWorkflowState: String, CaseIterable, Equatable, Codable {
    case idle
    case running
    case passed
    case warning
    case failed
    case runFailed

    var title: String {
        switch self {
        case .idle:
            return "Idle"
        case .running:
            return "Running"
        case .passed:
            return "Passed"
        case .warning:
            return "Warning"
        case .failed:
            return "Failed"
        case .runFailed:
            return "Run Failed"
        }
    }
}

struct DoctorCheckGroup: Identifiable, Equatable {
    let domain: String
    let checks: [DoctorCheckPayload]

    var id: String {
        domain
    }
}

struct DoctorSummary: Equatable {
    let passCount: Int
    let warnCount: Int
    let failCount: Int
    let infoCount: Int
    let groups: [DoctorCheckGroup]

    init(payload: DoctorPayload) {
        self.passCount = Self.count(status: "PASS", in: payload)
        self.warnCount = Self.count(status: "WARN", in: payload)
        self.failCount = Self.count(status: "FAIL", in: payload)
        self.infoCount = Self.count(status: "INFO", in: payload)
        self.groups = Self.group(payload.results)
    }

    private static func count(status: String, in payload: DoctorPayload) -> Int {
        payload.counts[status] ?? payload.results.filter { $0.status == status }.count
    }

    private static func group(_ checks: [DoctorCheckPayload]) -> [DoctorCheckGroup] {
        let grouped = Dictionary(grouping: checks) { check in
            check.details.stringValue(for: "domain") ?? "General"
        }
        return grouped
            .map { DoctorCheckGroup(domain: $0.key, checks: $0.value) }
            .sorted { left, right in
                severityRank(left.checks) == severityRank(right.checks)
                    ? left.domain < right.domain
                    : severityRank(left.checks) < severityRank(right.checks)
            }
    }

    private static func severityRank(_ checks: [DoctorCheckPayload]) -> Int {
        if checks.contains(where: { $0.status == "FAIL" }) {
            return 0
        }
        if checks.contains(where: { $0.status == "WARN" }) {
            return 1
        }
        return 2
    }
}

@MainActor
final class DoctorStore: ObservableObject {
    @Published var skipSSH = false
    @Published var skipBonjour = false
    @Published var skipSMB = false
    @Published private(set) var state: DoctorWorkflowState = .idle
    @Published private(set) var payload: DoctorPayload?
    @Published private(set) var summary: DoctorSummary?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?
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

    @discardableResult
    func runDoctor(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            rejectRun("Another operation is already running.")
            return .rejected("Another operation is already running.")
        }
        backend.clear()
        let start = run(
            operation: "doctor",
            params: OperationParams.doctor(
                password: password,
                skipSSH: skipSSH,
                skipBonjour: skipBonjour,
                skipSMB: skipSMB
            ),
            profile: profile
        )
        guard case .started(let operation) = start else {
            rejectRun(start.rejectionMessage ?? "Operation could not start.")
            return start
        }
        lastProcessedEventCount = 0
        activeOperation = operation
        state = .running
        payload = nil
        summary = nil
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        return start
    }

    func clear() {
        backend.clear()
        lastProcessedEventCount = 0
        state = .idle
        payload = nil
        summary = nil
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        activeOperation = nil
    }

    func cancel() {
        backend.cancel()
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
        guard event.operation == "doctor" else {
            return
        }
        guard activeOperation?.operation == event.operation else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }

        if event.type == "error" {
            if event.code == "auth_failed" {
                passwordInvalidProfileID = activeOperation?.profileID
            }
            error = BackendErrorViewModel(event: event)
            state = .runFailed
            activeOperation = nil
            return
        }

        guard event.type == "result" else {
            return
        }
        applyDoctorResult(event)
    }

    private func applyDoctorResult(_ event: BackendEvent) {
        do {
            let decoded = try event.decodePayload(DoctorPayload.self)
            payload = decoded
            summary = DoctorSummary(payload: decoded)
            error = nil
            if decoded.fatal || event.ok == false {
                state = .failed
            } else if summary?.warnCount ?? 0 > 0 {
                state = .warning
            } else {
                state = .passed
            }
            activeOperation = nil
        } catch {
            self.error = BackendErrorViewModel(
                operation: "doctor",
                code: "contract_decode_failed",
                message: error.localizedDescription
            )
            state = .runFailed
            activeOperation = nil
        }
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "doctor",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .runFailed
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
            backend.run(operation: operation, params: params, context: context)
            return .started(activeOperation)
        }
    }
}
