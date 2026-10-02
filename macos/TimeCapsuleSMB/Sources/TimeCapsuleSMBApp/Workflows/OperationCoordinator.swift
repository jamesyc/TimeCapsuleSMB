import Combine
import Foundation

struct ActiveOperation: Equatable, Identifiable {
    let id: UUID
    let operation: String
    let profileID: DeviceProfile.ID?
    let context: DeviceRuntimeContext?

    init(
        id: UUID = UUID(),
        operation: String,
        profileID: DeviceProfile.ID?,
        context: DeviceRuntimeContext?
    ) {
        self.id = id
        self.operation = operation
        self.profileID = profileID
        self.context = context
    }
}

enum OperationStartResult: Equatable {
    case started(ActiveOperation)
    case rejected(String)

    var operation: ActiveOperation? {
        guard case .started(let operation) = self else {
            return nil
        }
        return operation
    }

    var rejectionMessage: String? {
        guard case .rejected(let message) = self else {
            return nil
        }
        return message
    }
}

enum DeviceWorkflowLane: String, Hashable, Equatable, CaseIterable {
    case configure
    case deploy
    case doctor
    case reachability
    case sshAccess = "ssh_access"
    case maintenance
    case activate
    case uninstall
    case fsck
    case flash

    static func lane(for operation: String) -> DeviceWorkflowLane? {
        switch operation {
        case "configure", "update-config-settings":
            return .configure
        case "deploy":
            return .deploy
        case "doctor":
            return .doctor
        case "reachability":
            return .reachability
        case "set-ssh":
            return .sshAccess
        case "activate":
            return .activate
        case "uninstall":
            return .uninstall
        case "fsck":
            return .fsck
        case "flash":
            return .flash
        default:
            return nil
        }
    }
}

enum OperationLaneKey: Hashable, Equatable, Identifiable, CustomStringConvertible {
    case app
    case appWorkflow(DeviceWorkflowLane)
    case device(DeviceProfile.ID)
    case deviceWorkflow(DeviceProfile.ID, DeviceWorkflowLane)
    case candidateHost(String)
    case localPath(String)

    var id: String {
        switch self {
        case .app:
            return "app"
        case .appWorkflow(let workflow):
            return "app:\(workflow.rawValue)"
        case .device(let profileID):
            return "device:\(profileID)"
        case .deviceWorkflow(let profileID, let workflow):
            return "device:\(profileID):\(workflow.rawValue)"
        case .candidateHost(let host):
            return "candidate:\(host)"
        case .localPath(let path):
            return "local-path:\(path)"
        }
    }

    var description: String {
        id
    }

    var deviceProfileID: DeviceProfile.ID? {
        switch self {
        case .device(let profileID), .deviceWorkflow(let profileID, _):
            return profileID
        case .app, .appWorkflow, .candidateHost, .localPath:
            return nil
        }
    }
}

private enum OperationResourceKey: Hashable, Equatable {
    case device(DeviceProfile.ID)
}

@MainActor
final class OperationLane: ObservableObject {
    let key: OperationLaneKey
    let backend: BackendClient

    @Published private(set) var activeOperation: ActiveOperation?

    var onStateChanged: (() -> Void)?

    private var isReplayingConfirmation = false
    private var cancellables: Set<AnyCancellable> = []

    init(key: OperationLaneKey, backend: BackendClient) {
        self.key = key
        self.backend = backend

        Publishers.CombineLatest(backend.$isRunning, backend.$pendingConfirmation)
            .sink { [weak self] isRunning, pendingConfirmation in
                guard let self else { return }
                if !isRunning && pendingConfirmation == nil && !self.isReplayingConfirmation {
                    self.activeOperation = nil
                    self.onStateChanged?()
                }
            }
            .store(in: &cancellables)
    }

    var isBusy: Bool {
        backend.isRunning || backend.pendingConfirmation != nil
    }

    var canCancel: Bool {
        backend.canCancel
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        context: DeviceRuntimeContext?,
        activeDeviceID: DeviceProfile.ID?,
        password: String? = nil
    ) -> OperationStartResult {
        guard !isBusy else {
            return .rejected(L10n.string("operation.error.already_running"))
        }

        let updatedParams = OperationCredentialInjector.injectingPassword(password, into: params)

        let activeOperation = ActiveOperation(
            operation: operation,
            profileID: activeDeviceID,
            context: context
        )
        self.activeOperation = activeOperation
        backend.run(
            operation: operation,
            params: updatedParams,
            context: context,
            requestID: activeOperation.id.uuidString
        )
        onStateChanged?()
        return .started(activeOperation)
    }

    func confirmPending() {
        guard backend.pendingConfirmation != nil else {
            return
        }
        isReplayingConfirmation = true
        backend.confirmPending()
        isReplayingConfirmation = false
        onStateChanged?()
    }

    func cancelPendingConfirmation() {
        backend.cancelPendingConfirmation()
        onStateChanged?()
    }

    func cancel() {
        backend.cancel()
    }

    func clear() {
        backend.clear()
        activeOperation = nil
        onStateChanged?()
    }
}

@MainActor
final class OperationCoordinator: ObservableObject {
    @Published private(set) var activeOperations: [OperationLaneKey: ActiveOperation] = [:]
    @Published private(set) var lanesRevision = 0
    @Published private(set) var readyConfirmation: PendingConfirmation?

    let appLane: OperationLane

    private var lanes: [OperationLaneKey: OperationLane] = [:]
    private var laneCancellables: [OperationLaneKey: Set<AnyCancellable>] = [:]
    private var helperPathCancellable: AnyCancellable?

    var backend: BackendClient {
        appLane.backend
    }

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.appLane = OperationLane(key: .app, backend: backend)
        lanes[.app] = appLane
        observe(lane: appLane)
        helperPathCancellable = backend.$helperPath
            .sink { [weak self] helperPath in
                Task { @MainActor in
                    self?.syncHelperPath(helperPath)
                }
            }
    }

    func lane(for key: OperationLaneKey) -> OperationLane {
        if let lane = lanes[key] {
            return lane
        }
        let lane = OperationLane(key: key, backend: backend.makeSibling())
        lanes[key] = lane
        observe(lane: lane)
        refreshLaneState()
        return lane
    }

    func lane(for profile: DeviceProfile) -> OperationLane {
        lane(for: .device(profile.id))
    }

    var allLanes: [OperationLane] {
        lanes.values.sorted { left, right in
            laneSortKey(left.key) < laneSortKey(right.key)
        }
    }

    var pendingConfirmation: PendingConfirmation? {
        pendingConfirmationLane?.backend.pendingConfirmation
    }

    var pendingConfirmationLane: OperationLane? {
        if let primary = primaryLane(), primary.backend.pendingConfirmation != nil {
            return primary
        }
        return allLanes.first { $0.backend.pendingConfirmation != nil }
    }

    var canCancel: Bool {
        primaryLane()?.canCancel ?? false
    }

    func canCancel(profileID: DeviceProfile.ID) -> Bool {
        cancellableDeviceLane(for: profileID)?.canCancel ?? false
    }

    var hasActiveWork: Bool {
        allLanes.contains { $0.isBusy }
    }

    func activeOperation(for key: OperationLaneKey) -> ActiveOperation? {
        lane(for: key).activeOperation
    }

    func activeOperation(for profile: DeviceProfile) -> ActiveOperation? {
        activeDeviceLane(for: profile.id)?.activeOperation
    }

    func isDeviceBusy(_ profileID: DeviceProfile.ID) -> Bool {
        allLanes.contains { lane in
            resourceKey(for: lane) == .device(profileID) && lane.isBusy
        }
    }

    func isDeviceBusy(_ profile: DeviceProfile) -> Bool {
        isDeviceBusy(profile.id)
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        profile: DeviceProfile?,
        password: String? = nil
    ) -> OperationStartResult {
        run(
            operation: operation,
            params: params,
            context: profile?.runtimeContext,
            activeDeviceID: profile?.id,
            password: password,
            laneKey: profile.map { defaultLaneKey(operation: operation, activeDeviceID: $0.id) } ?? .app
        )
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        laneKey: OperationLaneKey
    ) -> OperationStartResult {
        run(
            operation: operation,
            params: params,
            context: nil,
            activeDeviceID: nil,
            laneKey: laneKey
        )
    }

    @discardableResult
    func run(
        operation: String,
        params: [String: JSONValue] = [:],
        context: DeviceRuntimeContext?,
        activeDeviceID: DeviceProfile.ID?,
        password: String? = nil,
        laneKey: OperationLaneKey? = nil
    ) -> OperationStartResult {
        let resolvedLaneKey = laneKey ?? defaultLaneKey(operation: operation, activeDeviceID: activeDeviceID)
        let lane = lane(for: resolvedLaneKey)
        if let resourceKey = resourceKey(for: resolvedLaneKey, activeDeviceID: activeDeviceID),
           conflictingLane(for: resourceKey, excluding: resolvedLaneKey) != nil {
            return .rejected(L10n.string("operation.error.already_running"))
        }
        let result = lane.run(
            operation: operation,
            params: params,
            context: context,
            activeDeviceID: activeDeviceID,
            password: password
        )
        refreshLaneState()
        return result
    }

    func confirmPending() {
        pendingConfirmationLane?.confirmPending()
        refreshLaneState()
    }

    func cancelPendingConfirmation() {
        pendingConfirmationLane?.cancelPendingConfirmation()
        refreshLaneState()
    }

    // Actions carry the displayed confirmation, not the current primary lane.
    // Its UUID also prevents a stale dismissal from cancelling the next prompt.
    func confirm(_ confirmation: PendingConfirmation) {
        guard let lane = confirmationLane(for: confirmation), !lane.backend.isRunning else { return }
        lane.confirmPending()
        refreshLaneState()
    }

    func cancel(_ confirmation: PendingConfirmation) {
        guard let lane = confirmationLane(for: confirmation), !lane.backend.isRunning else { return }
        lane.cancelPendingConfirmation()
        refreshLaneState()
    }

    private func confirmationLane(for confirmation: PendingConfirmation) -> OperationLane? {
        allLanes.first { $0.backend.pendingConfirmation?.id == confirmation.id }
    }

    func cancel() {
        primaryLane()?.cancel()
    }

    func cancel(profileID: DeviceProfile.ID) {
        cancellableDeviceLane(for: profileID)?.cancel()
    }

    func cancel(laneKey: OperationLaneKey) {
        lane(for: laneKey).cancel()
    }

    func clear() {
        for lane in lanes.values {
            lane.clear()
        }
        refreshLaneState()
    }

    func clear(laneKey: OperationLaneKey) {
        lane(for: laneKey).clear()
        refreshLaneState()
    }

    private func observe(lane: OperationLane) {
        var cancellables: Set<AnyCancellable> = []

        lane.onStateChanged = { [weak self] in
            self?.refreshLaneState()
        }
        lane.backend.$events
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refreshLaneState()
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refreshLaneState()
                }
            }
            .store(in: &cancellables)
        lane.backend.$activeOperationName
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refreshLaneState()
                }
            }
            .store(in: &cancellables)
        lane.backend.$pendingConfirmation
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refreshLaneState()
                }
            }
            .store(in: &cancellables)

        laneCancellables[lane.key] = cancellables
    }

    private func refreshLaneState() {
        activeOperations = lanes.compactMapValues(\.activeOperation)
        // Keep the displayed request stable while other devices finish. A pending
        // confirmation reserves its device even before it can be presented.
        if readyConfirmation.flatMap({ confirmationLane(for: $0) }) == nil {
            readyConfirmation = allLanes.first {
                !$0.backend.isRunning && $0.backend.pendingConfirmation != nil
            }?.backend.pendingConfirmation
        }
        lanesRevision += 1
    }

    private func primaryLane() -> OperationLane? {
        if let runningDevice = allLanes.first(where: { lane in
            lane.key != .app && lane.backend.isRunning
        }) {
            return runningDevice
        }
        if appLane.backend.isRunning {
            return appLane
        }
        if let pendingDevice = allLanes.first(where: { lane in
            lane.key != .app && lane.backend.pendingConfirmation != nil
        }) {
            return pendingDevice
        }
        if appLane.backend.pendingConfirmation != nil {
            return appLane
        }
        return allLanes.first { $0.activeOperation != nil }
    }

    private func activeDeviceLane(for profileID: DeviceProfile.ID) -> OperationLane? {
        allLanes.first { lane in
            resourceKey(for: lane) == .device(profileID) && lane.activeOperation != nil
        }
    }

    private func cancellableDeviceLane(for profileID: DeviceProfile.ID) -> OperationLane? {
        allLanes.first { lane in
            resourceKey(for: lane) == .device(profileID) && lane.canCancel
        }
    }

    private func conflictingLane(for resourceKey: OperationResourceKey, excluding laneKey: OperationLaneKey) -> OperationLane? {
        allLanes.first { lane in
            lane.key != laneKey && self.resourceKey(for: lane) == resourceKey && lane.isBusy
        }
    }

    private func defaultLaneKey(operation: String, activeDeviceID: DeviceProfile.ID?) -> OperationLaneKey {
        guard let activeDeviceID else {
            return .app
        }
        if let workflow = DeviceWorkflowLane.lane(for: operation) {
            return .deviceWorkflow(activeDeviceID, workflow)
        }
        return .device(activeDeviceID)
    }

    private func resourceKey(for lane: OperationLane) -> OperationResourceKey? {
        resourceKey(for: lane.key, activeDeviceID: lane.activeOperation?.profileID)
    }

    private func resourceKey(
        for laneKey: OperationLaneKey,
        activeDeviceID: DeviceProfile.ID?
    ) -> OperationResourceKey? {
        if let profileID = laneKey.deviceProfileID ?? activeDeviceID {
            return .device(profileID)
        }
        return nil
    }

    private func syncHelperPath(_ helperPath: String) {
        for lane in lanes.values where lane.backend !== appLane.backend {
            if lane.backend.helperPath != helperPath {
                lane.backend.helperPath = helperPath
            }
        }
    }

    private func laneSortKey(_ key: OperationLaneKey) -> String {
        switch key {
        case .app:
            return "0:app"
        case .appWorkflow(let workflow):
            return "0:app:\(workflow.rawValue)"
        case .device(let profileID):
            return "1:\(profileID)"
        case .deviceWorkflow(let profileID, let workflow):
            return "1:\(profileID):\(workflow.rawValue)"
        case .candidateHost(let host):
            return "2:\(host)"
        case .localPath(let path):
            return "3:\(path)"
        }
    }
}
