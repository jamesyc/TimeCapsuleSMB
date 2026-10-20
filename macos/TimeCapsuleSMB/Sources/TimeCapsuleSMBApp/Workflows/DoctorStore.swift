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
            return L10n.string("workflow.state.idle")
        case .running:
            return L10n.string("workflow.state.running")
        case .passed:
            return L10n.string("workflow.state.passed")
        case .warning:
            return L10n.string("workflow.state.warning")
        case .failed:
            return L10n.string("workflow.state.failed")
        case .runFailed:
            return L10n.string("workflow.state.run_failed")
        }
    }
}

/// A Checkup section's verdict: either section failing fails the Checkup,
/// but each says on its own whether the device or this Mac is at fault.
enum DoctorSectionStatus: String, Equatable, Codable {
    case pass
    case warn
    case fail
    /// Nothing ran on the device: SSH was skipped or refused.
    case notChecked = "not_checked"
}

/// A manager verdict a Checkup result reports, with the dashboard row and the
/// Checkup text it shows. DoctorSummary.payloadProblems is the one list.
/// stopsSamba: file sharing is off for it; otherwise Samba serves and only
/// rsync, which is optional, is off. replacesDisk: the disk could not read
/// the files, so a new disk fixes it, not Install.
struct PayloadProblem: Equatable {
    let code: String
    let rowID: String
    let detailKey: String
    let stopsSamba: Bool
    var replacesDisk = false
}

struct DoctorSummary: Equatable {
    static let runtimeNotInstalledResultCode = "runtime_not_installed"
    static let runtimeNotStartedResultCode = "runtime_not_started"
    static let payloadMissingFromDiskResultCode = "payload_missing_from_disk"
    static let payloadDamagedOnDiskResultCode = "payload_damaged_on_disk"
    static let payloadUnreadableOnDiskResultCode = "payload_unreadable_on_disk"
    static let rsyncMissingFromDiskResultCode = "rsync_missing_from_disk"
    static let rsyncDamagedOnDiskResultCode = "rsync_damaged_on_disk"
    static let rsyncUnreadableOnDiskResultCode = "rsync_unreadable_on_disk"
    static let diskNotMountingResultCode = "disk_not_mounting"
    static let diskFailingResultCode = "disk_failing"
    /// The installed files are missing from every disk, as after an Erase in
    /// AirPort Utility, the disk's copy of Samba is damaged, or the disk could
    /// not read it (a failing disk); or rsync's files are missing,
    /// damaged or unreadable, which leaves Samba serving. Those that stop
    /// Samba come first.
    static let payloadProblems = [
        PayloadProblem(code: payloadMissingFromDiskResultCode, rowID: "runtime-payload-missing",
                       detailKey: "doctor.check.payload_missing_from_disk", stopsSamba: true),
        PayloadProblem(code: payloadDamagedOnDiskResultCode, rowID: "runtime-payload-damaged",
                       detailKey: "doctor.check.payload_damaged_on_disk", stopsSamba: true),
        PayloadProblem(code: payloadUnreadableOnDiskResultCode, rowID: "runtime-payload-unreadable",
                       detailKey: "doctor.check.payload_unreadable_on_disk", stopsSamba: true, replacesDisk: true),
        PayloadProblem(code: rsyncMissingFromDiskResultCode, rowID: "runtime-rsync-missing",
                       detailKey: "doctor.check.rsync_missing_from_disk", stopsSamba: false),
        PayloadProblem(code: rsyncDamagedOnDiskResultCode, rowID: "runtime-rsync-damaged",
                       detailKey: "doctor.check.rsync_damaged_on_disk", stopsSamba: false),
        PayloadProblem(code: rsyncUnreadableOnDiskResultCode, rowID: "runtime-rsync-unreadable",
                       detailKey: "doctor.check.rsync_unreadable_on_disk", stopsSamba: false, replacesDisk: true),
    ]

    static func payloadProblem(code: String?) -> PayloadProblem? {
        payloadProblems.first { $0.code == code }
    }

    let passCount: Int
    let warnCount: Int
    let failCount: Int
    let infoCount: Int
    let runtimeNotInstalled: Bool
    let runtimeNotStarted: Bool
    /// The check code of a manager verdict (payloadProblems), the first in
    /// that list when there are several. nil when none.
    let payloadProblemCode: String?
    var payloadProblem: PayloadProblem? { Self.payloadProblem(code: payloadProblemCode) }
    /// A disk will not mount and no other holds the Samba files, as when
    /// diskd hangs on a disk that stopped answering.
    let diskNotMounting: Bool
    /// The disk reported errors it could not recover from: it outranks the
    /// payload problems, whose fix (Install) it would undo.
    let diskFailing: Bool
    let checks: [DoctorCheckPayload]
    let sections: [DoctorCheckDomain: DoctorSection]
    /// nil only for a payload without one, which the bundled helper never sends.
    let overallStatus: DoctorSectionStatus?

    /// The checks a running Checkup has reported so far, shown as they arrive.
    static func liveChecks(from events: [BackendEvent]) -> [DoctorCheckPayload] {
        events
            .filter { $0.type == "check" && $0.operation == "doctor" }
            .map { DoctorCheckPayload(status: $0.status ?? "", message: $0.message ?? "", details: $0.details ?? .object([:])) }
    }

    init(payload: DoctorPayload) {
        self.passCount = Self.count(status: "PASS", in: payload)
        self.warnCount = Self.count(status: "WARN", in: payload)
        self.failCount = Self.count(status: "FAIL", in: payload)
        self.infoCount = Self.count(status: "INFO", in: payload)
        self.runtimeNotInstalled = Self.containsResultCode(Self.runtimeNotInstalledResultCode, in: payload)
        self.runtimeNotStarted = Self.containsResultCode(Self.runtimeNotStartedResultCode, in: payload)
        self.payloadProblemCode = Self.payloadProblems.first { Self.containsResultCode($0.code, in: payload) }?.code
        self.diskNotMounting = Self.containsResultCode(Self.diskNotMountingResultCode, in: payload)
        self.diskFailing = Self.containsResultCode(Self.diskFailingResultCode, in: payload)
        self.checks = payload.results
        self.sections = Dictionary(
            (payload.sections ?? [:]).map { (DoctorCheckDomainPolicy.domain(for: $0.key), $0.value) },
            uniquingKeysWith: { first, _ in first }
        )
        self.overallStatus = payload.status
    }

    private static func count(status: String, in payload: DoctorPayload) -> Int {
        payload.counts[status] ?? payload.results.filter { $0.status == status }.count
    }

    private static func containsResultCode(_ code: String, in payload: DoctorPayload) -> Bool {
        payload.results.contains { check in
            check.details.stringValue(for: "code") == code
        }
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
    private let coordinator: OperationCoordinator
    private let laneKey: OperationLaneKey

    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    convenience init(backend: BackendClient) {
        self.init(coordinator: OperationCoordinator(backend: backend), laneKey: .app)
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
        backend.didUpdateEvents
            .sink { [weak self] events in
                // Consume terminal events before another run can clear this request's history.
                self?.process(events)
            }
            .store(in: &cancellables)
        backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
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
    func runDoctor(
        password: String,
        profile: DeviceProfile? = nil,
        localNetworkPreflight: LocalNetworkPreflightResult? = nil
    ) -> OperationStartResult {
        guard !isBusy else {
            rejectRun(.operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        backend.clear()
        let start = coordinator.run(
            operation: "doctor",
            params: OperationParams.Doctor.run(
                skipSSH: skipSSH,
                skipBonjour: skipBonjour,
                skipSMB: skipSMB,
                localNetworkPreflight: localNetworkPreflight
            ),
            context: profile?.runtimeContext,
            activeDeviceID: profile?.id,
            password: password,
            laneKey: laneKey
        )
        guard case .started(let operation) = start else {
            if let message = start.rejectionMessage {
                rejectRun(message)
            } else {
                rejectRun(.operationCouldNotStart)
            }
            return start
        }
        operationObserver.start(operation)
        resetForRun()
        passwordInvalidProfileID = nil
        process(backend.events)
        return start
    }

    /// Shows the Checkup as running while the app asks macOS about Local
    /// Network permission, before the helper starts: it neither looks idle
    /// nor offers to start again.
    func beginPreparing() {
        guard !isBusy else {
            return
        }
        backend.clear()
        resetForRun()
    }

    private func resetForRun() {
        state = .running
        payload = nil
        summary = nil
        error = nil
        currentStage = nil
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
        clearResultState()
    }

    func invalidateResult() {
        operationObserver.ignoreExistingEvents(backend.events)
        clearResultState()
    }

    private func clearResultState() {
        state = .idle
        payload = nil
        summary = nil
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        operationObserver.finish()
    }

    func cancel() {
        backend.cancel()
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "doctor" else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }

        if event.type == "error" {
            if event.code == "auth_failed" {
                passwordInvalidProfileID = activeOperation.profileID
            }
            error = BackendErrorViewModel(event: event)
            state = .runFailed
            operationObserver.finish()
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
            switch summary?.overallStatus {
            case .fail?:
                state = .failed
            case .warn?:
                state = .warning
            default:
                state = decoded.fatal || event.ok == false ? .failed : .passed
            }
            operationObserver.finish()
        } catch {
            self.error = BackendErrorViewModel(
                operation: "doctor",
                code: "contract_decode_failed",
                message: error.localizedDescription
            )
            state = .runFailed
            operationObserver.finish()
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
        operationObserver.finish()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "doctor", localError: localError)
        currentStage = nil
        state = .runFailed
        operationObserver.finish()
    }

}
