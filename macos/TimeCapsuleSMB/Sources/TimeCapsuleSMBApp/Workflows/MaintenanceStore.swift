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
    var wipeDiskState: MaintenanceOperationState { wipeDiskStore.state }
    var sshAccessState: MaintenanceOperationState { sshAccessStore.state }

    var activationResult: ActivationResultPayload? { activationStore.result }
    var uninstallResult: MaintenanceResultPayload? { uninstallStore.result }
    var fsckTargets: [FsckTargetViewModel] { fsckStore.targets }
    var fsckPlan: FsckPlanPayload? { fsckStore.plan }
    var fsckResult: FsckResultPayload? { fsckStore.result }
    var wipeDiskResult: WipeDiskResultPayload? { wipeDiskStore.result }
    var sshAccessPayload: SSHAccessPayload? { sshAccessStore.payload }

    let activationStore: ActivationStore
    let uninstallStore: UninstallStore
    let fsckStore: FsckStore
    let wipeDiskStore: WipeDiskStore
    let sshAccessStore: SSHAccessMaintenanceStore
    let bootHookStore: BootHookStore

    private let coordinator: OperationCoordinator
    private let laneKey: OperationLaneKey
    private var cancellables: Set<AnyCancellable> = []

    init(coordinator: OperationCoordinator, laneKey: OperationLaneKey) {
        // Each store runs on its own lane, with that lane's backend.
        func lane(_ workflow: MaintenanceWorkflow) -> (backend: BackendClient, key: OperationLaneKey) {
            let key = Self.workflowLaneKey(workflow, in: laneKey)
            return (coordinator.lane(for: key).backend, key)
        }
        self.coordinator = coordinator
        self.laneKey = laneKey
        self.activationStore = ActivationStore(
            backend: lane(.activate).backend,
            coordinator: coordinator,
            laneKey: lane(.activate).key
        )
        self.uninstallStore = UninstallStore(
            backend: lane(.uninstall).backend,
            coordinator: coordinator,
            laneKey: lane(.uninstall).key
        )
        self.fsckStore = FsckStore(
            backend: lane(.fsck).backend,
            coordinator: coordinator,
            laneKey: lane(.fsck).key
        )
        self.wipeDiskStore = WipeDiskStore(
            backend: lane(.wipeDisk).backend,
            coordinator: coordinator,
            laneKey: lane(.wipeDisk).key
        )
        self.sshAccessStore = SSHAccessMaintenanceStore(
            backend: lane(.sshAccess).backend,
            coordinator: coordinator,
            laneKey: lane(.sshAccess).key
        )
        self.bootHookStore = BootHookStore(
            backend: lane(.bootHook).backend,
            coordinator: coordinator,
            laneKey: lane(.bootHook).key
        )
        observeWorkflowStores()
    }

    /// Each workflow gets a lane of its own, so a store whose lane is busy is
    /// running its own job; a refused second start relies on that.
    private static func workflowLaneKey(_ workflow: MaintenanceWorkflow, in laneKey: OperationLaneKey) -> OperationLaneKey {
        let lane = workflow.deviceWorkflowLane
        return laneKey.deviceProfileID.map { .deviceWorkflow($0, lane) } ?? .appWorkflow(lane)
    }

    private func observeWorkflowStores() {
        observe(sshAccessStore)
        observe(activationStore)
        observe(uninstallStore)
        observe(fsckStore)
        observe(wipeDiskStore)
        observe(bootHookStore)
    }

    private func observe<Store: ObservableObject>(_ store: Store) where Store.ObjectWillChangePublisher == ObservableObjectPublisher {
        store.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var isBusy: Bool {
        let maintenanceBusy = workflowStores.contains { $0.isBusy }
        let deviceBusy = deviceProfileID.map { coordinator.isDeviceBusy($0) } == true
        return maintenanceBusy || deviceBusy
    }

    func timelineEvents(for workflow: MaintenanceWorkflow) -> [BackendEvent] {
        workflowStore(for: workflow).events
    }

    func isRunning(for workflow: MaintenanceWorkflow) -> Bool {
        workflowStore(for: workflow).isRunning
    }

    func currentStage(for workflow: MaintenanceWorkflow) -> OperationStageState? {
        workflowStore(for: workflow).currentStage
    }

    func error(for workflow: MaintenanceWorkflow) -> BackendErrorViewModel? {
        workflowStore(for: workflow).error
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

    var canRunWipeDisk: Bool {
        // isBusy covers the wipe's own store and the device.
        !isBusy
    }

    var canCheckSSHAccess: Bool {
        !isBusy && sshAccessStore.canCheck
    }

    var canEnableSSHAccess: Bool {
        !isBusy && sshAccessStore.canEnable
    }

    var canBackUpFirmware: Bool {
        // isBusy covers the boot hook's own store and the device.
        !isBusy
    }

    /// The two plans and the two Apple firmware checks.
    var canPlanFirmware: Bool {
        !isBusy && bootHookStore.canPlan
    }

    var canWritePatch: Bool {
        !isBusy && bootHookStore.canWritePatch
    }

    var canWriteRestore: Bool {
        !isBusy && bootHookStore.canWriteRestore
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
    func runWipeDisk(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .wipeDisk,
            rejectAlreadyRunning: { wipeDiskStore.rejectAlreadyRunning() },
            start: { wipeDiskStore.runWipe(password: password, profile: profile) }
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

    @discardableResult
    func backUpAndInspectFirmware(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .bootHook,
            rejectAlreadyRunning: { bootHookStore.rejectAlreadyRunning() },
            start: { bootHookStore.backUpAndInspectFirmware(password: password, profile: profile) }
        )
    }

    @discardableResult
    func planFirmware(mode: FlashPlanMode, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .bootHook,
            rejectAlreadyRunning: { bootHookStore.rejectAlreadyRunning() },
            start: { bootHookStore.planFirmware(mode: mode, profile: profile) }
        )
    }

    @discardableResult
    func writeFirmware(mode: FlashPlanMode, password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        startMaintenanceWorkflow(
            .bootHook,
            rejectAlreadyRunning: { bootHookStore.rejectAlreadyRunning() },
            start: { bootHookStore.writeFirmware(mode: mode, password: password, profile: profile) }
        )
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
        [sshAccessStore, bootHookStore, activationStore, uninstallStore, fsckStore, wipeDiskStore]
    }

    private var deviceProfileID: DeviceProfile.ID? {
        laneKey.deviceProfileID
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
        case .wipeDisk:
            return wipeDiskStore
        case .bootHook:
            return bootHookStore
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
    var currentStage: OperationStageState? { get }
    var error: BackendErrorViewModel? { get }
}

extension ActivationStore: MaintenanceWorkflowStore {}
extension UninstallStore: MaintenanceWorkflowStore {}
extension FsckStore: MaintenanceWorkflowStore {}
extension WipeDiskStore: MaintenanceWorkflowStore {}
extension SSHAccessMaintenanceStore: MaintenanceWorkflowStore {}
extension BootHookStore: MaintenanceWorkflowStore {}
