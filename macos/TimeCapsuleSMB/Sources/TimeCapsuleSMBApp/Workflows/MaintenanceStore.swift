import Combine
import Foundation

@MainActor
final class MaintenanceStore: ObservableObject {
    @Published var selectedWorkflow: MaintenanceWorkflow = .activate
    @Published var mountWait = "30" {
        didSet { markPlansStaleForOptionChange() }
    }
    @Published var noReboot = false {
        didSet {
            if noReboot && noWait {
                noWait = false
            }
            markPlansStaleForOptionChange()
        }
    }
    @Published var noWait = false {
        didSet {
            if noWait && noReboot {
                noReboot = false
            }
            markPlansStaleForOptionChange()
        }
    }
    var selectedFsckTargetID: FsckTargetViewModel.ID? {
        get { fsckStore.selectedTargetID }
        set {
            guard newValue != fsckStore.selectedTargetID else { return }
            fsckStore.selectTarget(id: newValue, options: currentOptions)
        }
    }

    var activateState: MaintenanceOperationState { activationStore.state }
    var uninstallState: MaintenanceOperationState { uninstallStore.state }
    var fsckState: MaintenanceOperationState { fsckStore.state }
    var sshAccessState: MaintenanceOperationState { sshAccessStore.state }

    var activationResult: ActivationResultPayload? { activationStore.result }
    var uninstallResult: MaintenanceResultPayload? { uninstallStore.result }
    var fsckTargets: [FsckTargetViewModel] { fsckStore.targets }
    var fsckPlan: FsckPlanPayload? { fsckStore.plan }
    var fsckResult: FsckResultPayload? { fsckStore.result }
    var sshAccessPayload: SSHAccessPayload? { sshAccessStore.payload }

    var currentStage: OperationStageState? {
        currentStage(for: selectedWorkflow) ?? workflowStores.lazy.compactMap { $0.currentStage }.first
    }

    var error: BackendErrorViewModel? {
        error(for: selectedWorkflow) ?? workflowStores.lazy.compactMap { $0.error }.first
    }

    let backend: BackendClient
    let activationStore: ActivationStore
    let uninstallStore: UninstallStore
    let fsckStore: FsckStore
    let sshAccessStore: SSHAccessMaintenanceStore

    private let coordinator: OperationCoordinator
    private let laneKeysByWorkflow: [MaintenanceWorkflow: OperationLaneKey]
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    convenience init(backend: BackendClient) {
        self.init(coordinator: OperationCoordinator(backend: backend))
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
        self.sshAccessStore = SSHAccessMaintenanceStore(
            backend: backendsByWorkflow[.sshAccess] ?? coordinator.lane(for: laneKey).backend,
            coordinator: coordinator,
            laneKey: laneKeysByWorkflow[.sshAccess]
        )
        observeWorkflowStores()
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
        observe(sshAccessStore)
        observe(activationStore)
        observe(uninstallStore)
        observe(fsckStore)
    }

    private func observe<Store: ObservableObject>(_ store: Store) where Store.ObjectWillChangePublisher == ObservableObjectPublisher {
        store.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
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
        let deviceBusy = deviceProfileID.map { coordinator.isDeviceBusy($0) } == true
        return maintenanceBusy || deviceBusy
    }

    var canCancel: Bool {
        activeWorkflowStore?.canCancel ?? false
    }

    func timelineEvents(for workflow: MaintenanceWorkflow) -> [BackendEvent] {
        workflowStore(for: workflow).events
    }

    func currentStage(for workflow: MaintenanceWorkflow) -> OperationStageState? {
        workflowStore(for: workflow).currentStage
    }

    func error(for workflow: MaintenanceWorkflow) -> BackendErrorViewModel? {
        workflowStore(for: workflow).error
    }

    func pendingConfirmation(for workflow: MaintenanceWorkflow) -> PendingConfirmation? {
        workflowStore(for: workflow).pendingConfirmation
    }

    func confirmPending(for workflow: MaintenanceWorkflow) {
        workflowStore(for: workflow).confirmPending()
    }

    func cancelPendingConfirmation(for workflow: MaintenanceWorkflow) {
        switch workflow {
        case .sshAccess:
            sshAccessStore.cancelPendingConfirmation()
        case .activate:
            activationStore.cancelPendingConfirmation()
        case .uninstall:
            uninstallStore.cancelPendingConfirmation()
        case .fsck:
            fsckStore.cancelPendingConfirmation(options: currentOptions)
        }
    }

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var selectedFsckTarget: FsckTargetViewModel? {
        fsckStore.selectedTarget
    }

    var canRunActivation: Bool {
        !isBusy && activationStore.canRun
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

    var canCheckSSHAccess: Bool {
        !isBusy && sshAccessStore.canCheck
    }

    var canEnableSSHAccess: Bool {
        !isBusy && sshAccessStore.canEnable
    }

    @discardableResult
    func runActivation(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .activate,
            rejectAlreadyRunning: { activationStore.rejectAlreadyRunning() },
            start: { activationStore.runActivation(password: password, profile: profile) }
        )
    }

    @discardableResult
    func runUninstall(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .uninstall,
            rejectAlreadyRunning: { uninstallStore.rejectAlreadyRunning() },
            start: { uninstallStore.runUninstall(options: currentOptions, password: password, profile: profile) }
        )
    }

    @discardableResult
    func refreshFsckTargets(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .fsck,
            rejectAlreadyRunning: { fsckStore.rejectAlreadyRunning() },
            start: { fsckStore.refreshTargets(mountWaitValue: mountWaitValue, password: password, profile: profile) }
        )
    }

    @discardableResult
    func planFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .fsck,
            rejectAlreadyRunning: { fsckStore.rejectAlreadyRunning() },
            start: { fsckStore.planFsck(options: currentOptions, password: password, profile: profile) }
        )
    }

    @discardableResult
    func runFsck(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .fsck,
            rejectAlreadyRunning: { fsckStore.rejectAlreadyRunning() },
            start: { fsckStore.runFsck(options: currentOptions, password: password, profile: profile) }
        )
    }

    @discardableResult
    func checkSSHAccess(profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .sshAccess,
            rejectAlreadyRunning: { sshAccessStore.rejectAlreadyRunning() },
            start: { sshAccessStore.check(profile: profile) }
        )
    }

    @discardableResult
    func enableSSHAccess(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .sshAccess,
            rejectAlreadyRunning: { sshAccessStore.rejectAlreadyRunning() },
            start: { sshAccessStore.enable(password: password, noWait: noWait, profile: profile) }
        )
    }

    func clear() {
        activationStore.clear()
        uninstallStore.clear()
        fsckStore.clear()
        sshAccessStore.clear()
    }

    func cancel() {
        activeWorkflowStore?.cancel()
    }

    private func begin(workflow: MaintenanceWorkflow) -> Bool {
        selectedWorkflow = workflow
        return !isBusy
    }

    private func startMaintenanceWorkflow(
        _ workflow: MaintenanceWorkflow,
        rejectAlreadyRunning: () -> OperationStartResult,
        start: () -> OperationStartResult
    ) -> OperationStartResult {
        begin(workflow: workflow) ? start() : rejectAlreadyRunning()
    }

    private var workflowStores: [any MaintenanceWorkflowStore] {
        [sshAccessStore, activationStore, uninstallStore, fsckStore]
    }

    private var activeWorkflowStore: (any MaintenanceWorkflowStore)? {
        workflowStores.first { $0.isBusy }
    }

    private var deviceProfileID: DeviceProfile.ID? {
        laneKeysByWorkflow.values.lazy.compactMap(\.deviceProfileID).first
    }

    private func workflowStore(for workflow: MaintenanceWorkflow) -> any MaintenanceWorkflowStore {
        switch workflow {
        case .sshAccess:
            return sshAccessStore
        case .activate:
            return activationStore
        case .uninstall:
            return uninstallStore
        case .fsck:
            return fsckStore
        }
    }

    private var currentOptions: MaintenanceOptions? {
        guard let mountWaitValue else {
            return nil
        }
        let rebootOptions = RebootExecutionOptionPolicy.normalized(
            noReboot: noReboot,
            noWait: noWait
        )
        return MaintenanceOptions(
            noReboot: rebootOptions.noReboot,
            noWait: rebootOptions.noWait,
            mountWait: mountWaitValue
        )
    }

    private func markPlansStaleForOptionChange() {
        fsckStore.markPlanStaleIfNeeded(options: currentOptions)
    }
}

@MainActor
private protocol MaintenanceWorkflowStore: ObservableObject {
    var events: [BackendEvent] { get }
    var isRunning: Bool { get }
    var isBusy: Bool { get }
    var canCancel: Bool { get }
    var currentStage: OperationStageState? { get }
    var error: BackendErrorViewModel? { get }
    var pendingConfirmation: PendingConfirmation? { get }
    func confirmPending()
    func cancel()
}

extension ActivationStore: MaintenanceWorkflowStore {}
extension UninstallStore: MaintenanceWorkflowStore {}
extension FsckStore: MaintenanceWorkflowStore {}
extension SSHAccessMaintenanceStore: MaintenanceWorkflowStore {}
