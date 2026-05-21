import Combine
import Foundation

enum MaintenanceWorkflow: String, CaseIterable, Equatable, Identifiable {
    case activate
    case uninstall
    case fsck
    case repairXattrs

    var id: String { rawValue }

    var title: String {
        switch self {
        case .activate:
            return "Activate"
        case .uninstall:
            return "Uninstall"
        case .fsck:
            return "fsck"
        case .repairXattrs:
            return "Repair xattrs"
        }
    }
}

enum MaintenanceOperationState: String, CaseIterable, Equatable {
    case idle
    case loading
    case listReady
    case planning
    case planReady
    case planStale
    case scanning
    case scanReady
    case scanStale
    case awaitingConfirmation
    case running
    case repairing
    case succeeded
    case repaired
    case failed

    var title: String {
        switch self {
        case .idle:
            return "Idle"
        case .loading:
            return "Loading"
        case .listReady:
            return "List Ready"
        case .planning:
            return "Planning"
        case .planReady:
            return "Plan Ready"
        case .planStale:
            return "Plan Stale"
        case .scanning:
            return "Scanning"
        case .scanReady:
            return "Scan Ready"
        case .scanStale:
            return "Scan Stale"
        case .awaitingConfirmation:
            return "Awaiting Confirmation"
        case .running:
            return "Running"
        case .repairing:
            return "Repairing"
        case .succeeded:
            return "Succeeded"
        case .repaired:
            return "Repaired"
        case .failed:
            return "Failed"
        }
    }
}

struct MaintenanceOptions: Equatable {
    let noReboot: Bool
    let noWait: Bool
    let mountWait: Int
}

struct FsckTargetViewModel: Identifiable, Equatable {
    let id: String
    let device: String
    let mountpoint: String
    let name: String?
    let builtin: Bool?

    init(payload: FsckTargetPayload) {
        self.id = "\(payload.device)|\(payload.mountpoint)"
        self.device = payload.device
        self.mountpoint = payload.mountpoint
        self.name = payload.name
        self.builtin = payload.builtin
    }

    var volumeParam: String {
        device
    }
}

@MainActor
final class MaintenanceStore: ObservableObject {
    @Published var selectedWorkflow: MaintenanceWorkflow = .activate
    @Published var mountWait = "30" {
        didSet { markPlansStaleForOptionChange() }
    }
    @Published var noReboot = false {
        didSet { markPlansStaleForOptionChange() }
    }
    @Published var noWait = false {
        didSet { markPlansStaleForOptionChange() }
    }
    @Published var repairPath = "" {
        didSet { markRepairStaleForPathChange() }
    }
    @Published var selectedFsckTargetID: FsckTargetViewModel.ID? {
        didSet { markFsckPlanStaleIfNeeded() }
    }

    @Published private(set) var activateState: MaintenanceOperationState = .idle
    @Published private(set) var uninstallState: MaintenanceOperationState = .idle
    @Published private(set) var fsckState: MaintenanceOperationState = .idle
    @Published private(set) var repairState: MaintenanceOperationState = .idle

    @Published private(set) var activationPlan: ActivationPlanPayload?
    @Published private(set) var activationResult: ActivationResultPayload?
    @Published private(set) var uninstallPlan: UninstallPlanPayload?
    @Published private(set) var uninstallResult: MaintenanceResultPayload?
    @Published private(set) var fsckTargets: [FsckTargetViewModel] = []
    @Published private(set) var fsckPlan: FsckPlanPayload?
    @Published private(set) var fsckResult: FsckResultPayload?
    @Published private(set) var repairScan: RepairXattrsPayload?
    @Published private(set) var repairResult: RepairXattrsPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    let backend: BackendClient
    private let coordinator: OperationCoordinator?

    private var plannedUninstallOptions: MaintenanceOptions?
    private var plannedFsckOptions: MaintenanceOptions?
    private var plannedFsckTargetID: FsckTargetViewModel.ID?
    private var scannedRepairPath: String?
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
        nonNegativeInteger(mountWait)
    }

    var selectedFsckTarget: FsckTargetViewModel? {
        guard let selectedFsckTargetID else {
            return nil
        }
        return fsckTargets.first { $0.id == selectedFsckTargetID }
    }

    var canRunActivation: Bool {
        !backend.isRunning && activationPlan != nil && activateState == .planReady
    }

    var canRunUninstall: Bool {
        !backend.isRunning && uninstallPlan != nil && uninstallState == .planReady && currentOptions == plannedUninstallOptions
    }

    var canPlanFsck: Bool {
        !backend.isRunning && selectedFsckTarget != nil && currentOptions != nil
    }

    var canRunFsck: Bool {
        !backend.isRunning
            && fsckPlan != nil
            && fsckState == .planReady
            && currentOptions == plannedFsckOptions
            && selectedFsckTargetID == plannedFsckTargetID
    }

    var canRepairXattrs: Bool {
        !backend.isRunning
            && repairState == .scanReady
            && repairScan?.repairableCount ?? 0 > 0
            && scannedRepairPath == trimmedRepairPath
    }

    @discardableResult
    func planActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        let start = startRun(
            operation: "activate",
            params: OperationParams.activatePlan(password: password),
            profile: profile,
            workflow: .activate
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .activate
        activateState = .planning
        activationPlan = nil
        activationResult = nil
        return start
    }

    @discardableResult
    func runActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !backend.isRunning else {
            rejectRun(workflow: .activate, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
        }
        guard canRunActivation else {
            failLocally(workflow: .activate, message: "Plan NetBSD4 activation before running it.")
            return .rejected("Plan NetBSD4 activation before running it.")
        }
        let start = startRun(
            operation: "activate",
            params: OperationParams.activateRun(password: password),
            profile: profile,
            workflow: .activate
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .activate
        activateState = .running
        activationResult = nil
        return start
    }

    @discardableResult
    func planUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(workflow: .uninstall, message: "Mount wait must be a non-negative integer.")
            return .rejected("Mount wait must be a non-negative integer.")
        }
        let start = startRun(
            operation: "uninstall",
            params: OperationParams.uninstallPlan(
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile,
            workflow: .uninstall
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .uninstall
        uninstallState = .planning
        uninstallPlan = nil
        uninstallResult = nil
        plannedUninstallOptions = options
        return start
    }

    @discardableResult
    func runUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !backend.isRunning else {
            rejectRun(workflow: .uninstall, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
        }
        guard let options = plannedUninstallOptions, currentOptions == options, uninstallPlan != nil else {
            uninstallState = .planStale
            error = BackendErrorViewModel(
                operation: "uninstall",
                code: "plan_stale",
                message: "Review and regenerate the uninstall plan before running it."
            )
            return .rejected("Review and regenerate the uninstall plan before running it.")
        }
        guard uninstallState == .planReady else {
            return .rejected("Uninstall plan is not ready.")
        }
        let start = startRun(
            operation: "uninstall",
            params: OperationParams.uninstallRun(
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile,
            workflow: .uninstall
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .uninstall
        uninstallState = .running
        uninstallResult = nil
        return start
    }

    @discardableResult
    func refreshFsckTargets(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let mountWaitValue else {
            failLocally(workflow: .fsck, message: "Mount wait must be a non-negative integer.")
            return .rejected("Mount wait must be a non-negative integer.")
        }
        let start = startRun(
            operation: "fsck",
            params: OperationParams.fsckList(mountWait: Double(mountWaitValue), password: password),
            profile: profile,
            workflow: .fsck
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .fsck
        fsckState = .loading
        fsckTargets = []
        selectedFsckTargetID = nil
        fsckPlan = nil
        fsckResult = nil
        return start
    }

    @discardableResult
    func planFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(workflow: .fsck, message: "Mount wait must be a non-negative integer.")
            return .rejected("Mount wait must be a non-negative integer.")
        }
        guard let target = selectedFsckTarget else {
            failLocally(workflow: .fsck, message: "Select a mounted HFS volume before planning fsck.")
            return .rejected("Select a mounted HFS volume before planning fsck.")
        }
        let start = startRun(
            operation: "fsck",
            params: OperationParams.fsckPlan(
                volume: target.volumeParam,
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile,
            workflow: .fsck
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .fsck
        fsckState = .planning
        fsckPlan = nil
        fsckResult = nil
        plannedFsckOptions = options
        plannedFsckTargetID = target.id
        return start
    }

    @discardableResult
    func runFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !backend.isRunning else {
            rejectRun(workflow: .fsck, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
        }
        guard let options = plannedFsckOptions,
              let target = selectedFsckTarget,
              selectedFsckTargetID == plannedFsckTargetID,
              currentOptions == options,
              fsckPlan != nil else {
            fsckState = .planStale
            error = BackendErrorViewModel(
                operation: "fsck",
                code: "plan_stale",
                message: "Review and regenerate the fsck plan before running it."
            )
            return .rejected("Review and regenerate the fsck plan before running it.")
        }
        guard fsckState == .planReady else {
            return .rejected("fsck plan is not ready.")
        }
        let start = startRun(
            operation: "fsck",
            params: OperationParams.fsckRun(
                volume: target.volumeParam,
                noReboot: options.noReboot,
                noWait: options.noWait,
                mountWait: Double(options.mountWait),
                password: password
            ),
            profile: profile,
            workflow: .fsck
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .fsck
        fsckState = .running
        fsckResult = nil
        return start
    }

    @discardableResult
    func scanRepairXattrs() -> OperationStartResult {
        guard !trimmedRepairPath.isEmpty else {
            failLocally(workflow: .repairXattrs, message: "Choose a mounted SMB share path before scanning.")
            return .rejected("Choose a mounted SMB share path before scanning.")
        }
        let path = trimmedRepairPath
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsScan(path: path),
            profile: nil,
            workflow: .repairXattrs
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .repairXattrs
        repairState = .scanning
        repairScan = nil
        repairResult = nil
        scannedRepairPath = path
        return start
    }

    @discardableResult
    func runRepairXattrs() -> OperationStartResult {
        guard !backend.isRunning else {
            rejectRun(workflow: .repairXattrs, message: "Another operation is already running.")
            return .rejected("Another operation is already running.")
        }
        guard canRepairXattrs else {
            repairState = .scanStale
            error = BackendErrorViewModel(
                operation: "repair-xattrs",
                code: "scan_stale",
                message: "Run a fresh xattr scan before repairing."
            )
            return .rejected("Run a fresh xattr scan before repairing.")
        }
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsRun(path: trimmedRepairPath),
            profile: nil,
            workflow: .repairXattrs
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .repairXattrs
        repairState = .repairing
        repairResult = nil
        return start
    }

    func clear() {
        backend.clear()
        lastProcessedEventCount = 0
        activateState = .idle
        uninstallState = .idle
        fsckState = .idle
        repairState = .idle
        activationPlan = nil
        activationResult = nil
        uninstallPlan = nil
        uninstallResult = nil
        fsckTargets = []
        selectedFsckTargetID = nil
        fsckPlan = nil
        fsckResult = nil
        repairScan = nil
        repairResult = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
        plannedUninstallOptions = nil
        plannedFsckOptions = nil
        plannedFsckTargetID = nil
        scannedRepairPath = nil
        activeOperation = nil
    }

    func cancel() {
        backend.cancel()
    }

    private var currentOptions: MaintenanceOptions? {
        guard let mountWaitValue else {
            return nil
        }
        return MaintenanceOptions(noReboot: noReboot, noWait: noWait, mountWait: mountWaitValue)
    }

    private var trimmedRepairPath: String {
        repairPath.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func resetRunState() {
        backend.clear()
        lastProcessedEventCount = 0
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
        activeOperation = nil
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
        guard ["activate", "uninstall", "fsck", "repair-xattrs"].contains(event.operation) else {
            return
        }
        guard activeOperation?.operation == event.operation else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if event.operation == "activate", activateState == .awaitingConfirmation {
                activateState = .running
            } else if event.operation == "uninstall", uninstallState == .awaitingConfirmation {
                uninstallState = .running
            } else if event.operation == "fsck", fsckState == .awaitingConfirmation {
                fsckState = .running
            } else if event.operation == "repair-xattrs", repairState == .awaitingConfirmation {
                repairState = .repairing
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
            applyFalseResult(event)
            return
        }

        switch event.operation {
        case "activate":
            handleActivateResult(event)
        case "uninstall":
            handleUninstallResult(event)
        case "fsck":
            handleFsckResult(event)
        case "repair-xattrs":
            handleRepairResult(event)
        default:
            break
        }
    }

    private func handleActivateResult(_ event: BackendEvent) {
        if activateState == .planning {
            do {
                activationPlan = try event.decodePayload(ActivationPlanPayload.self)
                activateState = .planReady
                activeOperation = nil
            } catch {
                failContract(workflow: .activate, error: error)
            }
            return
        }
        do {
            activationResult = try event.decodePayload(ActivationResultPayload.self)
            activateState = .succeeded
            error = nil
            activeOperation = nil
        } catch {
            failContract(workflow: .activate, error: error)
        }
    }

    private func handleUninstallResult(_ event: BackendEvent) {
        if uninstallState == .planning {
            do {
                uninstallPlan = try event.decodePayload(UninstallPlanPayload.self)
                uninstallState = .planReady
                activeOperation = nil
            } catch {
                failContract(workflow: .uninstall, error: error)
            }
            return
        }
        do {
            uninstallResult = try event.decodePayload(MaintenanceResultPayload.self)
            uninstallState = .succeeded
            error = nil
            activeOperation = nil
        } catch {
            failContract(workflow: .uninstall, error: error)
        }
    }

    private func handleFsckResult(_ event: BackendEvent) {
        switch fsckState {
        case .loading:
            do {
                let payload = try event.decodePayload(FsckVolumeListPayload.self)
                fsckTargets = payload.targets.map(FsckTargetViewModel.init)
                selectedFsckTargetID = fsckTargets.count == 1 ? fsckTargets[0].id : nil
                fsckState = .listReady
                error = nil
                activeOperation = nil
            } catch {
                failContract(workflow: .fsck, error: error)
            }
        case .planning:
            do {
                fsckPlan = try event.decodePayload(FsckPlanPayload.self)
                fsckState = .planReady
                error = nil
                activeOperation = nil
            } catch {
                failContract(workflow: .fsck, error: error)
            }
        default:
            do {
                fsckResult = try event.decodePayload(FsckResultPayload.self)
                fsckState = .succeeded
                error = nil
                activeOperation = nil
            } catch {
                failContract(workflow: .fsck, error: error)
            }
        }
    }

    private func handleRepairResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(RepairXattrsPayload.self)
            if repairState == .scanning {
                repairScan = payload
                repairState = .scanReady
                activeOperation = nil
            } else {
                repairResult = payload
                repairState = .repaired
                activeOperation = nil
            }
            error = nil
        } catch {
            failContract(workflow: .repairXattrs, error: error)
        }
    }

    private func applyError(_ event: BackendEvent) {
        if event.code == "confirmation_required" {
            error = nil
            switch event.operation {
            case "activate":
                activateState = .awaitingConfirmation
            case "uninstall":
                uninstallState = .awaitingConfirmation
            case "fsck":
                fsckState = .awaitingConfirmation
            case "repair-xattrs":
                repairState = .awaitingConfirmation
            default:
                break
            }
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation?.profileID
        }
        error = BackendErrorViewModel(event: event)
        failState(for: event.operation)
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.payloadSummaryText ?? event.summary
        )
        failState(for: event.operation)
    }

    private func failContract(workflow: MaintenanceWorkflow, error: Error) {
        self.error = BackendErrorViewModel(
            operation: operationName(for: workflow),
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        setState(.failed, for: workflow)
        activeOperation = nil
    }

    private func failLocally(workflow: MaintenanceWorkflow, message: String) {
        error = BackendErrorViewModel(
            operation: operationName(for: workflow),
            code: "validation_failed",
            message: message
        )
        selectedWorkflow = workflow
        currentStage = nil
        setState(.failed, for: workflow)
        activeOperation = nil
    }

    private func rejectRun(workflow: MaintenanceWorkflow, message: String) {
        error = BackendErrorViewModel(
            operation: operationName(for: workflow),
            code: "operation_rejected",
            message: message
        )
        selectedWorkflow = workflow
        currentStage = nil
        setState(.failed, for: workflow)
        activeOperation = nil
    }

    private func failState(for operation: String) {
        switch operation {
        case "activate":
            activateState = .failed
        case "uninstall":
            uninstallState = .failed
        case "fsck":
            fsckState = .failed
        case "repair-xattrs":
            repairState = .failed
        default:
            break
        }
        activeOperation = nil
    }

    private func setState(_ state: MaintenanceOperationState, for workflow: MaintenanceWorkflow) {
        switch workflow {
        case .activate:
            activateState = state
        case .uninstall:
            uninstallState = state
        case .fsck:
            fsckState = state
        case .repairXattrs:
            repairState = state
        }
    }

    private func operationName(for workflow: MaintenanceWorkflow) -> String {
        switch workflow {
        case .activate:
            return "activate"
        case .uninstall:
            return "uninstall"
        case .fsck:
            return "fsck"
        case .repairXattrs:
            return "repair-xattrs"
        }
    }

    private func markPlansStaleForOptionChange() {
        if uninstallState == .planReady, currentOptions != plannedUninstallOptions {
            uninstallState = .planStale
        }
        markFsckPlanStaleIfNeeded()
    }

    private func markFsckPlanStaleIfNeeded() {
        if fsckState == .planReady,
           currentOptions != plannedFsckOptions || selectedFsckTargetID != plannedFsckTargetID {
            fsckState = .planStale
        }
    }

    private func markRepairStaleForPathChange() {
        if repairState == .scanReady, scannedRepairPath != trimmedRepairPath {
            repairState = .scanStale
        }
    }

    private func nonNegativeInteger(_ text: String) -> Int? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Int(trimmed), value >= 0 else {
            return nil
        }
        return value
    }

    private func startRun(
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        workflow: MaintenanceWorkflow
    ) -> OperationStartResult {
        guard !backend.isRunning else {
            let message = "Another operation is already running."
            rejectRun(workflow: workflow, message: message)
            return .rejected(message)
        }
        resetRunState()
        let start = run(operation: operation, params: params, profile: profile)
        switch start {
        case .started(let operation):
            activeOperation = operation
        case .rejected(let message):
            rejectRun(workflow: workflow, message: message)
        }
        return start
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
            backend.run(operation: operation, params: params, context: context)
            return .started(activeOperation)
        }
    }
}
