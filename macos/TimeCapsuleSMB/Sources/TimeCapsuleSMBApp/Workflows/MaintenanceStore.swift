import Combine
import Foundation

@MainActor
final class MaintenanceStore: ObservableObject {
    @Published var selectedWorkflow: MaintenanceWorkflow = .activate {
        didSet { syncFromWorkflowStores() }
    }
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
        didSet {
            guard selectedFsckTargetID != fsckStore.selectedTargetID else {
                return
            }
            fsckStore.selectTarget(id: selectedFsckTargetID, options: currentOptions)
            syncFromWorkflowStores()
        }
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
    let activationStore: ActivationStore
    let uninstallStore: UninstallStore
    let fsckStore: FsckStore
    let repairXattrsStore: RepairXattrsStore

    private let coordinator: OperationCoordinator?
    private let laneKeysByWorkflow: [MaintenanceWorkflow: OperationLaneKey]
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        let backendsByWorkflow = Self.standaloneBackends(primary: backend)
        self.backend = backend
        self.coordinator = nil
        self.laneKeysByWorkflow = [:]
        self.activationStore = ActivationStore(backend: backendsByWorkflow[.activate] ?? backend)
        self.uninstallStore = UninstallStore(backend: backendsByWorkflow[.uninstall] ?? backend.makeSibling())
        self.fsckStore = FsckStore(backend: backendsByWorkflow[.fsck] ?? backend.makeSibling())
        self.repairXattrsStore = RepairXattrsStore(backend: backendsByWorkflow[.repairXattrs] ?? backend.makeSibling())
        observeWorkflowStores()
        syncFromWorkflowStores()
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
        self.coordinator = coordinator
        self.laneKeysByWorkflow = laneKeysByWorkflow
        self.activationStore = ActivationStore(
            backend: backendsByWorkflow[.activate] ?? coordinator.lane(for: laneKey).backend,
            coordinator: coordinator,
            laneKey: laneKeysByWorkflow[.activate]
        )
        self.uninstallStore = UninstallStore(
            backend: backendsByWorkflow[.uninstall] ?? coordinator.lane(for: laneKey).backend,
            coordinator: coordinator,
            laneKey: laneKeysByWorkflow[.uninstall]
        )
        self.fsckStore = FsckStore(
            backend: backendsByWorkflow[.fsck] ?? coordinator.lane(for: laneKey).backend,
            coordinator: coordinator,
            laneKey: laneKeysByWorkflow[.fsck]
        )
        self.repairXattrsStore = RepairXattrsStore(
            backend: backendsByWorkflow[.repairXattrs] ?? coordinator.lane(for: laneKey).backend,
            coordinator: coordinator,
            laneKey: laneKeysByWorkflow[.repairXattrs]
        )
        observeWorkflowStores()
        syncFromWorkflowStores()
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

    private func observeWorkflowStores() {
        observe(activationStore)
        observe(uninstallStore)
        observe(fsckStore)
        observe(repairXattrsStore)
    }

    private func observe<Store: ObservableObject>(_ store: Store) where Store.ObjectWillChangePublisher == ObservableObjectPublisher {
        store.objectWillChange
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.syncFromWorkflowStores()
                }
            }
            .store(in: &cancellables)
    }

    var events: [BackendEvent] {
        workflowStores.flatMap(\.events)
    }

    var isRunning: Bool {
        workflowStores.contains { $0.isRunning }
    }

    var isBusy: Bool {
        let maintenanceBusy = workflowStores.contains { $0.isBusy }
        let deviceBusy = deviceProfileID.map { coordinator?.isDeviceBusy($0) ?? false } == true
        return maintenanceBusy || deviceBusy
    }

    var canCancel: Bool {
        activeWorkflowStore?.canCancel ?? false
    }

    func timelineEvents(for workflow: MaintenanceWorkflow) -> [BackendEvent] {
        workflowStore(for: workflow).events
    }

    func currentStage(for workflow: MaintenanceWorkflow) -> OperationStageState? {
        currentStagesByWorkflow[workflow]
    }

    func error(for workflow: MaintenanceWorkflow) -> BackendErrorViewModel? {
        errorsByWorkflow[workflow]
    }

    func pendingConfirmation(for workflow: MaintenanceWorkflow) -> PendingConfirmation? {
        workflowStore(for: workflow).pendingConfirmation
    }

    func confirmPending(for workflow: MaintenanceWorkflow) {
        workflowStore(for: workflow).confirmPending()
    }

    func cancelPendingConfirmation(for workflow: MaintenanceWorkflow) {
        switch workflow {
        case .activate:
            activationStore.cancelPendingConfirmation()
        case .uninstall:
            uninstallStore.cancelPendingConfirmation(options: currentOptions)
        case .fsck:
            fsckStore.cancelPendingConfirmation(options: currentOptions)
        case .repairXattrs:
            repairXattrsStore.cancelPendingConfirmation(path: trimmedRepairPath, options: currentRepairOptions)
        }
        syncFromWorkflowStores()
    }

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var selectedFsckTarget: FsckTargetViewModel? {
        fsckStore.selectedTarget
    }

    var canPlanActivation: Bool {
        !isBusy && activationStore.canPlan
    }

    var canRunActivation: Bool {
        !isBusy && activationStore.canRun
    }

    var canPlanUninstall: Bool {
        !isBusy && uninstallStore.canPlan(options: currentOptions)
    }

    var canRunUninstall: Bool {
        !isBusy && uninstallStore.canRun(options: currentOptions)
    }

    var canFindFsckVolumes: Bool {
        !isBusy && fsckStore.canFindVolumes(mountWaitValue: mountWaitValue)
    }

    var canPlanFsck: Bool {
        !isBusy && fsckStore.canPlan(options: currentOptions)
    }

    var canRunFsck: Bool {
        !isBusy && fsckStore.canRun(options: currentOptions)
    }

    var canRepairXattrs: Bool {
        !isBusy && repairXattrsStore.canRepair(path: trimmedRepairPath, options: currentRepairOptions)
    }

    var canScanRepairXattrs: Bool {
        !isBusy && repairXattrsStore.canScan(path: trimmedRepairPath, options: currentRepairOptions)
    }

    @discardableResult
    func planActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .activate) else {
            let start = activationStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = activationStore.planActivation(password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func runActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .activate) else {
            let start = activationStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = activationStore.runActivation(password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func planUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .uninstall) else {
            let start = uninstallStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = uninstallStore.planUninstall(options: currentOptions, password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func runUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .uninstall) else {
            let start = uninstallStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = uninstallStore.runUninstall(options: currentOptions, password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func refreshFsckTargets(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .fsck) else {
            let start = fsckStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = fsckStore.refreshTargets(mountWaitValue: mountWaitValue, password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func planFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .fsck) else {
            let start = fsckStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = fsckStore.planFsck(options: currentOptions, password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func runFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard begin(workflow: .fsck) else {
            let start = fsckStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = fsckStore.runFsck(options: currentOptions, password: password, profile: profile)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func scanRepairXattrs() -> OperationStartResult {
        guard begin(workflow: .repairXattrs) else {
            let start = repairXattrsStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = repairXattrsStore.scanRepairXattrs(path: trimmedRepairPath, options: currentRepairOptions)
        syncFromWorkflowStores()
        return start
    }

    @discardableResult
    func runRepairXattrs() -> OperationStartResult {
        guard begin(workflow: .repairXattrs) else {
            let start = repairXattrsStore.rejectAlreadyRunning()
            syncFromWorkflowStores()
            return start
        }
        let start = repairXattrsStore.runRepairXattrs(path: trimmedRepairPath, options: currentRepairOptions)
        syncFromWorkflowStores()
        return start
    }

    func clear() {
        activationStore.clear()
        uninstallStore.clear()
        fsckStore.clear()
        repairXattrsStore.clear()
        syncFromWorkflowStores()
    }

    func cancel() {
        activeWorkflowStore?.cancel()
    }

    private func begin(workflow: MaintenanceWorkflow) -> Bool {
        selectedWorkflow = workflow
        return !isBusy
    }

    private var workflowStores: [any MaintenanceWorkflowStore] {
        [activationStore, uninstallStore, fsckStore, repairXattrsStore]
    }

    private var activeWorkflowStore: (any MaintenanceWorkflowStore)? {
        workflowStores.first { $0.isBusy }
    }

    private var deviceProfileID: DeviceProfile.ID? {
        laneKeysByWorkflow.values.lazy.compactMap(\.deviceProfileID).first
    }

    private func workflowStore(for workflow: MaintenanceWorkflow) -> any MaintenanceWorkflowStore {
        switch workflow {
        case .activate:
            return activationStore
        case .uninstall:
            return uninstallStore
        case .fsck:
            return fsckStore
        case .repairXattrs:
            return repairXattrsStore
        }
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

    private func markPlansStaleForOptionChange() {
        uninstallStore.markPlanStaleIfNeeded(options: currentOptions)
        fsckStore.markPlanStaleIfNeeded(options: currentOptions)
        syncFromWorkflowStores()
    }

    private func markRepairScanStaleIfNeeded() {
        repairXattrsStore.markScanStaleIfNeeded(path: trimmedRepairPath, options: currentRepairOptions)
        syncFromWorkflowStores()
    }

    private func syncFromWorkflowStores() {
        activateState = activationStore.state
        activationPlan = activationStore.plan
        activationResult = activationStore.result

        uninstallState = uninstallStore.state
        uninstallPlan = uninstallStore.plan
        uninstallResult = uninstallStore.result

        fsckState = fsckStore.state
        fsckTargets = fsckStore.targets
        if selectedFsckTargetID != fsckStore.selectedTargetID {
            selectedFsckTargetID = fsckStore.selectedTargetID
        }
        fsckPlan = fsckStore.plan
        fsckResult = fsckStore.result

        repairState = repairXattrsStore.state
        repairScan = repairXattrsStore.scan
        repairResult = repairXattrsStore.result

        currentStagesByWorkflow = workflowStages
        errorsByWorkflow = workflowErrors
        currentStage = currentStagesByWorkflow[selectedWorkflow] ?? currentStagesByWorkflow.values.first
        error = errorsByWorkflow[selectedWorkflow] ?? errorsByWorkflow.values.first
        passwordInvalidProfileID = [
            activationStore.passwordInvalidProfileID,
            uninstallStore.passwordInvalidProfileID,
            fsckStore.passwordInvalidProfileID,
            repairXattrsStore.passwordInvalidProfileID
        ].compactMap { $0 }.first
    }

    private var workflowStages: [MaintenanceWorkflow: OperationStageState] {
        var stages: [MaintenanceWorkflow: OperationStageState] = [:]
        stages[.activate] = activationStore.currentStage
        stages[.uninstall] = uninstallStore.currentStage
        stages[.fsck] = fsckStore.currentStage
        stages[.repairXattrs] = repairXattrsStore.currentStage
        return stages
    }

    private var workflowErrors: [MaintenanceWorkflow: BackendErrorViewModel] {
        var errors: [MaintenanceWorkflow: BackendErrorViewModel] = [:]
        errors[.activate] = activationStore.error
        errors[.uninstall] = uninstallStore.error
        errors[.fsck] = fsckStore.error
        errors[.repairXattrs] = repairXattrsStore.error
        return errors
    }
}

@MainActor
private protocol MaintenanceWorkflowStore: ObservableObject {
    var events: [BackendEvent] { get }
    var isRunning: Bool { get }
    var isBusy: Bool { get }
    var canCancel: Bool { get }
    var pendingConfirmation: PendingConfirmation? { get }
    func confirmPending()
    func cancel()
}

extension ActivationStore: MaintenanceWorkflowStore {}
extension UninstallStore: MaintenanceWorkflowStore {}
extension FsckStore: MaintenanceWorkflowStore {}
extension RepairXattrsStore: MaintenanceWorkflowStore {}
