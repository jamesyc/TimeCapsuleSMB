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
            return L10n.string("maintenance.workflow.activate")
        case .uninstall:
            return L10n.string("maintenance.workflow.uninstall")
        case .fsck:
            return L10n.string("maintenance.workflow.fsck")
        case .repairXattrs:
            return L10n.string("maintenance.workflow.repair_xattrs")
        }
    }

    var deviceWorkflowLane: DeviceWorkflowLane {
        switch self {
        case .activate:
            return .activate
        case .uninstall:
            return .uninstall
        case .fsck:
            return .fsck
        case .repairXattrs:
            return .repairXattrs
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
            return L10n.string("workflow.state.idle")
        case .loading:
            return L10n.string("workflow.state.loading")
        case .listReady:
            return L10n.string("workflow.state.list_ready")
        case .planning:
            return L10n.string("workflow.state.planning")
        case .planReady:
            return L10n.string("workflow.state.plan_ready")
        case .planStale:
            return L10n.string("workflow.state.plan_stale")
        case .scanning:
            return L10n.string("workflow.state.scanning")
        case .scanReady:
            return L10n.string("workflow.state.scan_ready")
        case .scanStale:
            return L10n.string("workflow.state.scan_stale")
        case .awaitingConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .running:
            return L10n.string("workflow.state.running")
        case .repairing:
            return L10n.string("workflow.state.repairing")
        case .succeeded:
            return L10n.string("workflow.state.succeeded")
        case .repaired:
            return L10n.string("workflow.state.repaired")
        case .failed:
            return L10n.string("workflow.state.failed")
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
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairRecursive = true {
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairMaxDepth = "" {
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairIncludeHidden = false {
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairIncludeTimeMachine = false {
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairFixPermissions = false {
        didSet { markRepairScanStaleIfNeeded() }
    }
    @Published var repairVerbose = false {
        didSet { markRepairScanStaleIfNeeded() }
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
    @Published private(set) var currentStagesByWorkflow: [MaintenanceWorkflow: OperationStageState] = [:]
    @Published private(set) var errorsByWorkflow: [MaintenanceWorkflow: BackendErrorViewModel] = [:]

    let backend: BackendClient
    private let backendsByWorkflow: [MaintenanceWorkflow: BackendClient]
    private let coordinator: OperationCoordinator?
    private let laneKeysByWorkflow: [MaintenanceWorkflow: OperationLaneKey]

    private var plannedUninstallOptions: MaintenanceOptions?
    private var plannedFsckOptions: MaintenanceOptions?
    private var plannedFsckTargetID: FsckTargetViewModel.ID?
    private var scannedRepairPath: String?
    private var scannedRepairOptions: RepairXattrsOptions?
    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        let backendsByWorkflow = Self.standaloneBackends(primary: backend)
        self.backend = backend
        self.backendsByWorkflow = backendsByWorkflow
        self.coordinator = nil
        self.laneKeysByWorkflow = [:]
        observeBackends(backendsByWorkflow)
    }

    convenience init(coordinator: OperationCoordinator) {
        self.init(coordinator: coordinator, laneKey: .app)
    }

    init(coordinator: OperationCoordinator, laneKey: OperationLaneKey) {
        let laneKeysByWorkflow = Self.laneKeysByWorkflow(from: laneKey)
        let backendsByWorkflow = Self.coordinatedBackends(
            coordinator: coordinator,
            laneKeysByWorkflow: laneKeysByWorkflow
        )
        self.backend = backendsByWorkflow[.activate] ?? coordinator.lane(for: laneKey).backend
        self.backendsByWorkflow = backendsByWorkflow
        self.coordinator = coordinator
        self.laneKeysByWorkflow = laneKeysByWorkflow
        observeBackends(backendsByWorkflow)
    }

    private static func standaloneBackends(primary backend: BackendClient) -> [MaintenanceWorkflow: BackendClient] {
        Dictionary(uniqueKeysWithValues: MaintenanceWorkflow.allCases.map { workflow in
            workflow == .activate ? (workflow, backend) : (workflow, backend.makeSibling())
        })
    }

    private static func coordinatedBackends(
        coordinator: OperationCoordinator,
        laneKeysByWorkflow: [MaintenanceWorkflow: OperationLaneKey]
    ) -> [MaintenanceWorkflow: BackendClient] {
        Dictionary(uniqueKeysWithValues: MaintenanceWorkflow.allCases.map { workflow in
            let laneKey = laneKeysByWorkflow[workflow] ?? .app
            return (workflow, coordinator.lane(for: laneKey).backend)
        })
    }

    private static func laneKeysByWorkflow(from laneKey: OperationLaneKey) -> [MaintenanceWorkflow: OperationLaneKey] {
        switch laneKey {
        case .app:
            return Dictionary(uniqueKeysWithValues: MaintenanceWorkflow.allCases.map { workflow in
                (workflow, .appWorkflow(workflow.deviceWorkflowLane))
            })
        case .device(let profileID), .deviceWorkflow(let profileID, .maintenance):
            return Dictionary(uniqueKeysWithValues: MaintenanceWorkflow.allCases.map { workflow in
                (workflow, .deviceWorkflow(profileID, workflow.deviceWorkflowLane))
            })
        default:
            return Dictionary(uniqueKeysWithValues: MaintenanceWorkflow.allCases.map { workflow in
                (workflow, laneKey)
            })
        }
    }

    private func observeBackends(_ backendsByWorkflow: [MaintenanceWorkflow: BackendClient]) {
        for backend in uniqueBackends(backendsByWorkflow.values) {
            backend.$events
                .sink { [weak self] events in
                    Task { @MainActor in
                        self?.process(events)
                    }
                }
                .store(in: &cancellables)
        }
    }

    var events: [BackendEvent] {
        uniqueBackends(backendsByWorkflow.values).flatMap(\.events)
    }

    var isRunning: Bool {
        uniqueBackends(backendsByWorkflow.values).contains { $0.isRunning }
    }

    var isBusy: Bool {
        let maintenanceBusy = uniqueBackends(backendsByWorkflow.values).contains {
            $0.isRunning || $0.pendingConfirmation != nil
        }
        let deviceBusy = deviceProfileID.map { coordinator?.isDeviceBusy($0) ?? false } == true
        return maintenanceBusy || deviceBusy
    }

    var canCancel: Bool {
        activeBackend?.canCancel ?? false
    }

    func timelineEvents(for workflow: MaintenanceWorkflow) -> [BackendEvent] {
        backend(for: workflow).events
    }

    func currentStage(for workflow: MaintenanceWorkflow) -> OperationStageState? {
        currentStagesByWorkflow[workflow]
    }

    func error(for workflow: MaintenanceWorkflow) -> BackendErrorViewModel? {
        errorsByWorkflow[workflow]
    }

    func pendingConfirmation(for workflow: MaintenanceWorkflow) -> PendingConfirmation? {
        backend(for: workflow).pendingConfirmation
    }

    func confirmPending(for workflow: MaintenanceWorkflow) {
        backend(for: workflow).confirmPending()
    }

    func cancelPendingConfirmation(for workflow: MaintenanceWorkflow) {
        backend(for: workflow).cancelPendingConfirmation()
    }

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var selectedFsckTarget: FsckTargetViewModel? {
        guard let selectedFsckTargetID else {
            return nil
        }
        return fsckTargets.first { $0.id == selectedFsckTargetID }
    }

    var canPlanActivation: Bool {
        !isBusy
    }

    var canRunActivation: Bool {
        !isBusy && activationPlan != nil && activateState == .planReady
    }

    var canPlanUninstall: Bool {
        !isBusy && currentOptions != nil
    }

    var canRunUninstall: Bool {
        !isBusy && uninstallPlan != nil && uninstallState == .planReady && currentOptions == plannedUninstallOptions
    }

    var canFindFsckVolumes: Bool {
        !isBusy && mountWaitValue != nil
    }

    var canPlanFsck: Bool {
        !isBusy && selectedFsckTarget != nil && currentOptions != nil
    }

    var canRunFsck: Bool {
        !isBusy
            && fsckPlan != nil
            && fsckState == .planReady
            && currentOptions == plannedFsckOptions
            && selectedFsckTargetID == plannedFsckTargetID
    }

    var canRepairXattrs: Bool {
        !isBusy
            && repairState == .scanReady
            && repairScan?.repairableCount ?? 0 > 0
            && scannedRepairPath == trimmedRepairPath
            && scannedRepairOptions == currentRepairOptions
    }

    var canScanRepairXattrs: Bool {
        !isBusy
            && !trimmedRepairPath.isEmpty
            && currentRepairOptions != nil
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
        process(backend(for: .activate).events)
        return start
    }

    @discardableResult
    func runActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            rejectRun(workflow: .activate, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        guard canRunActivation else {
            failLocally(workflow: .activate, localError: .activationPlanRequired)
            return .rejected(WorkflowLocalError.activationPlanRequired.message)
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
        process(backend(for: .activate).events)
        return start
    }

    @discardableResult
    func planUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(workflow: .uninstall, localError: .mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
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
        process(backend(for: .uninstall).events)
        return start
    }

    @discardableResult
    func runUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            rejectRun(workflow: .uninstall, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        guard let options = plannedUninstallOptions, currentOptions == options, uninstallPlan != nil else {
            uninstallState = .planStale
            setError(BackendErrorViewModel(operation: "uninstall", localError: .uninstallPlanStale), for: .uninstall)
            return .rejected(WorkflowLocalError.uninstallPlanStale.message)
        }
        guard uninstallState == .planReady else {
            return .rejected(WorkflowLocalError.uninstallPlanNotReady.message)
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
        process(backend(for: .uninstall).events)
        return start
    }

    @discardableResult
    func refreshFsckTargets(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let mountWaitValue else {
            failLocally(workflow: .fsck, localError: .mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
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
        process(backend(for: .fsck).events)
        return start
    }

    @discardableResult
    func planFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            failLocally(workflow: .fsck, localError: .mountWaitInvalid)
            return .rejected(WorkflowLocalError.mountWaitInvalid.message)
        }
        guard let target = selectedFsckTarget else {
            failLocally(workflow: .fsck, localError: .fsckTargetRequired)
            return .rejected(WorkflowLocalError.fsckTargetRequired.message)
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
        process(backend(for: .fsck).events)
        return start
    }

    @discardableResult
    func runFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard !isBusy else {
            rejectRun(workflow: .fsck, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        guard let options = plannedFsckOptions,
              let target = selectedFsckTarget,
              selectedFsckTargetID == plannedFsckTargetID,
            currentOptions == options,
            fsckPlan != nil else {
            fsckState = .planStale
            setError(BackendErrorViewModel(operation: "fsck", localError: .fsckPlanStale), for: .fsck)
            return .rejected(WorkflowLocalError.fsckPlanStale.message)
        }
        guard fsckState == .planReady else {
            return .rejected(WorkflowLocalError.fsckPlanNotReady.message)
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
        process(backend(for: .fsck).events)
        return start
    }

    @discardableResult
    func scanRepairXattrs() -> OperationStartResult {
        guard let options = currentRepairOptions else {
            failLocally(workflow: .repairXattrs, localError: .repairXattrsDepthInvalid)
            return .rejected(WorkflowLocalError.repairXattrsDepthInvalid.message)
        }
        guard !trimmedRepairPath.isEmpty else {
            failLocally(workflow: .repairXattrs, localError: .repairXattrsPathRequired)
            return .rejected(WorkflowLocalError.repairXattrsPathRequired.message)
        }
        let path = trimmedRepairPath
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsScan(path: path, options: options),
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
        scannedRepairOptions = options
        process(backend(for: .repairXattrs).events)
        return start
    }

    @discardableResult
    func runRepairXattrs() -> OperationStartResult {
        guard !isBusy else {
            rejectRun(workflow: .repairXattrs, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        guard canRepairXattrs else {
            repairState = .scanStale
            setError(BackendErrorViewModel(operation: "repair-xattrs", localError: .repairXattrsScanStale), for: .repairXattrs)
            return .rejected(WorkflowLocalError.repairXattrsScanStale.message)
        }
        guard let options = scannedRepairOptions else {
            repairState = .scanStale
            setError(BackendErrorViewModel(operation: "repair-xattrs", localError: .repairXattrsScanStale), for: .repairXattrs)
            return .rejected(WorkflowLocalError.repairXattrsScanStale.message)
        }
        let start = startRun(
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsRun(path: trimmedRepairPath, options: options),
            profile: nil,
            workflow: .repairXattrs
        )
        guard case .started = start else {
            return start
        }
        selectedWorkflow = .repairXattrs
        repairState = .repairing
        repairResult = nil
        process(backend(for: .repairXattrs).events)
        return start
    }

    func clear() {
        for backend in uniqueBackends(backendsByWorkflow.values) {
            backend.clear()
        }
        operationObserver.clear()
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
        currentStagesByWorkflow = [:]
        errorsByWorkflow = [:]
        plannedUninstallOptions = nil
        plannedFsckOptions = nil
        plannedFsckTargetID = nil
        scannedRepairPath = nil
        scannedRepairOptions = nil
        operationObserver.finish()
    }

    func cancel() {
        activeBackend?.cancel()
    }

    private var activeBackend: BackendClient? {
        uniqueBackends(backendsByWorkflow.values)
            .first { $0.isRunning || $0.pendingConfirmation != nil }
    }

    private var deviceProfileID: DeviceProfile.ID? {
        laneKeysByWorkflow.values.lazy.compactMap(\.deviceProfileID).first
    }

    private func backend(for workflow: MaintenanceWorkflow) -> BackendClient {
        backendsByWorkflow[workflow] ?? backend
    }

    private func laneKey(for workflow: MaintenanceWorkflow) -> OperationLaneKey? {
        laneKeysByWorkflow[workflow]
    }

    private func workflow(for operation: String) -> MaintenanceWorkflow? {
        MaintenanceWorkflow.allCases.first { $0.operationName == operation }
    }

    private func setError(_ error: BackendErrorViewModel, for workflow: MaintenanceWorkflow) {
        errorsByWorkflow[workflow] = error
        self.error = error
    }

    private func clearError(for workflow: MaintenanceWorkflow) {
        errorsByWorkflow[workflow] = nil
        if error?.operation == workflow.operationName {
            error = errorsByWorkflow[selectedWorkflow] ?? errorsByWorkflow.values.first
        }
    }

    private func clearCurrentStage(for workflow: MaintenanceWorkflow) {
        currentStagesByWorkflow[workflow] = nil
        if currentStage?.operation == workflow.operationName {
            currentStage = currentStagesByWorkflow[selectedWorkflow] ?? currentStagesByWorkflow.values.first
        }
    }

    private func uniqueBackends<S: Sequence>(_ backends: S) -> [BackendClient] where S.Element == BackendClient {
        var seen: Set<ObjectIdentifier> = []
        var unique: [BackendClient] = []
        for backend in backends where seen.insert(ObjectIdentifier(backend)).inserted {
            unique.append(backend)
        }
        return unique
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

    private var repairMaxDepthValue: Int? {
        let trimmed = repairMaxDepth.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        return ValueParsers.nonNegativeInteger(trimmed)
    }

    private var currentRepairOptions: RepairXattrsOptions? {
        let trimmed = repairMaxDepth.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty, repairMaxDepthValue == nil {
            return nil
        }
        return RepairXattrsOptions(
            recursive: repairRecursive,
            maxDepth: repairMaxDepthValue,
            includeHidden: repairIncludeHidden,
            includeTimeMachine: repairIncludeTimeMachine,
            fixPermissions: repairFixPermissions,
            verbose: repairVerbose
        )
    }

    private func resetRunState(workflow: MaintenanceWorkflow) {
        backend(for: workflow).clear()
        operationObserver.clear()
        clearError(for: workflow)
        clearCurrentStage(for: workflow)
        passwordInvalidProfileID = nil
        operationObserver.finish()
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard let workflow = workflow(for: event.operation) else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            currentStagesByWorkflow[workflow] = stage
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
                operationObserver.finish()
            } catch {
                failContract(workflow: .activate, error: error)
            }
            return
        }
        do {
            activationResult = try event.decodePayload(ActivationResultPayload.self)
            activateState = .succeeded
            clearError(for: .activate)
            operationObserver.finish()
        } catch {
            failContract(workflow: .activate, error: error)
        }
    }

    private func handleUninstallResult(_ event: BackendEvent) {
        if uninstallState == .planning {
            do {
                uninstallPlan = try event.decodePayload(UninstallPlanPayload.self)
                uninstallState = .planReady
                operationObserver.finish()
            } catch {
                failContract(workflow: .uninstall, error: error)
            }
            return
        }
        do {
            uninstallResult = try event.decodePayload(MaintenanceResultPayload.self)
            uninstallState = .succeeded
            clearError(for: .uninstall)
            operationObserver.finish()
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
                clearError(for: .fsck)
                operationObserver.finish()
            } catch {
                failContract(workflow: .fsck, error: error)
            }
        case .planning:
            do {
                fsckPlan = try event.decodePayload(FsckPlanPayload.self)
                fsckState = .planReady
                clearError(for: .fsck)
                operationObserver.finish()
            } catch {
                failContract(workflow: .fsck, error: error)
            }
        default:
            do {
                fsckResult = try event.decodePayload(FsckResultPayload.self)
                fsckState = .succeeded
                clearError(for: .fsck)
                operationObserver.finish()
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
                operationObserver.finish()
            } else {
                repairResult = payload
                repairState = .repaired
                operationObserver.finish()
            }
            clearError(for: .repairXattrs)
        } catch {
            failContract(workflow: .repairXattrs, error: error)
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard let workflow = workflow(for: event.operation) else {
            return
        }
        if event.code == "confirmation_required" {
            clearError(for: workflow)
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
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled(operation: event.operation)
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        setError(BackendErrorViewModel(event: event), for: workflow)
        failState(for: event.operation)
    }

    private func applyConfirmationCancelled(operation: String) {
        if let workflow = workflow(for: operation) {
            clearError(for: workflow)
            clearCurrentStage(for: workflow)
        }
        operationObserver.finish()
        switch operation {
        case "activate":
            activateState = activationPlan == nil ? .idle : .planReady
        case "uninstall":
            restoreUninstallStateAfterCancellation()
        case "fsck":
            restoreFsckStateAfterCancellation()
        case "repair-xattrs":
            restoreRepairStateAfterCancellation()
        default:
            break
        }
    }

    private func restoreUninstallStateAfterCancellation() {
        guard uninstallPlan != nil else {
            uninstallState = .idle
            return
        }
        uninstallState = currentOptions == plannedUninstallOptions ? .planReady : .planStale
    }

    private func restoreFsckStateAfterCancellation() {
        guard fsckPlan != nil else {
            fsckState = fsckTargets.isEmpty ? .idle : .listReady
            return
        }
        fsckState = currentOptions == plannedFsckOptions && selectedFsckTargetID == plannedFsckTargetID
            ? .planReady
            : .planStale
    }

    private func restoreRepairStateAfterCancellation() {
        guard repairScan != nil else {
            repairState = .idle
            return
        }
        repairState = scannedRepairPath == trimmedRepairPath && scannedRepairOptions == currentRepairOptions
            ? .scanReady
            : .scanStale
    }

    private func applyFalseResult(_ event: BackendEvent) {
        if let workflow = workflow(for: event.operation) {
            setError(
                BackendErrorViewModel(
                    operation: event.operation,
                    code: "operation_failed",
                    message: event.localizedPayloadSummaryText ?? event.localizedSummary
                ),
                for: workflow
            )
        }
        failState(for: event.operation)
    }

    private func failContract(workflow: MaintenanceWorkflow, error: Error) {
        setError(
            BackendErrorViewModel(
                operation: operationName(for: workflow),
                code: "contract_decode_failed",
                message: error.localizedDescription
            ),
            for: workflow
        )
        setState(.failed, for: workflow)
        operationObserver.finish()
    }

    private func failLocally(workflow: MaintenanceWorkflow, message: String) {
        setError(
            BackendErrorViewModel(
                operation: operationName(for: workflow),
                code: "validation_failed",
                message: message
            ),
            for: workflow
        )
        selectedWorkflow = workflow
        clearCurrentStage(for: workflow)
        setState(.failed, for: workflow)
        operationObserver.finish()
    }

    private func failLocally(workflow: MaintenanceWorkflow, localError: WorkflowLocalError) {
        setError(BackendErrorViewModel(operation: operationName(for: workflow), localError: localError), for: workflow)
        selectedWorkflow = workflow
        clearCurrentStage(for: workflow)
        setState(.failed, for: workflow)
        operationObserver.finish()
    }

    private func rejectRun(workflow: MaintenanceWorkflow, message: String) {
        setError(
            BackendErrorViewModel(
                operation: operationName(for: workflow),
                code: "operation_rejected",
                message: message
            ),
            for: workflow
        )
        selectedWorkflow = workflow
        clearCurrentStage(for: workflow)
        setState(.failed, for: workflow)
        operationObserver.finish()
    }

    private func rejectRun(workflow: MaintenanceWorkflow, localError: WorkflowLocalError) {
        setError(BackendErrorViewModel(operation: operationName(for: workflow), localError: localError), for: workflow)
        selectedWorkflow = workflow
        clearCurrentStage(for: workflow)
        setState(.failed, for: workflow)
        operationObserver.finish()
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
        operationObserver.finish()
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

    private func markRepairScanStaleIfNeeded() {
        if repairState == .scanReady,
           scannedRepairPath != trimmedRepairPath || scannedRepairOptions != currentRepairOptions {
            repairState = .scanStale
        }
    }

    private func startRun(
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        workflow: MaintenanceWorkflow
    ) -> OperationStartResult {
        guard !isBusy else {
            rejectRun(workflow: workflow, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        resetRunState(workflow: workflow)
        let start = run(operation: operation, params: params, profile: profile, workflow: workflow)
        switch start {
        case .started(let operation):
            operationObserver.start(operation)
        case .rejected(let message):
            rejectRun(workflow: workflow, message: message)
        }
        return start
    }

    private func run(
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        workflow: MaintenanceWorkflow
    ) -> OperationStartResult {
        if let coordinator {
            return coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                laneKey: laneKey(for: workflow)
            )
        } else {
            guard !isBusy else {
                return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
            }
            let context = profile?.runtimeContext
            let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
            backend(for: workflow).run(
                operation: operation,
                params: params,
                context: context,
                requestID: activeOperation.id.uuidString
            )
            return .started(activeOperation)
        }
    }
}
