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

enum OperationLaneKey: Hashable, Equatable, Identifiable, CustomStringConvertible {
    case app
    case device(DeviceProfile.ID)
    case candidateHost(String)
    case localPath(String)

    var id: String {
        switch self {
        case .app:
            return "app"
        case .device(let profileID):
            return "device:\(profileID)"
        case .candidateHost(let host):
            return "candidate:\(host)"
        case .localPath(let path):
            return "local-path:\(path)"
        }
    }

    var description: String {
        id
    }

}

@MainActor
final class OperationLane: ObservableObject {
    let key: OperationLaneKey
    let backend: BackendClient

    @Published private(set) var activeOperation: ActiveOperation?
    @Published private(set) var rejectedOperationMessage: String?

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
            let message = L10n.string("operation.error.already_running")
            rejectedOperationMessage = message
            onStateChanged?()
            return .rejected(message)
        }

        var updatedParams = params
        if let password,
           !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           updatedParams["credentials"] == nil {
            updatedParams["credentials"] = .object(["password": .string(password)])
        }

        let activeOperation = ActiveOperation(
            operation: operation,
            profileID: activeDeviceID,
            context: context
        )
        rejectedOperationMessage = nil
        self.activeOperation = activeOperation
        backend.run(operation: operation, params: updatedParams, context: context)
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
        backend.pendingConfirmation = nil
        onStateChanged?()
    }

    func cancel() {
        backend.cancel()
    }

    func clear() {
        backend.clear()
        rejectedOperationMessage = nil
        activeOperation = nil
        onStateChanged?()
    }
}

@MainActor
final class OperationCoordinator: ObservableObject {
    @Published private(set) var activeOperations: [OperationLaneKey: ActiveOperation] = [:]
    @Published private(set) var activeOperation: ActiveOperation?
    @Published private(set) var activeDeviceID: DeviceProfile.ID?
    @Published private(set) var rejectedOperationMessages: [OperationLaneKey: String] = [:]
    @Published private(set) var rejectedOperationMessage: String?
    @Published private(set) var lanesRevision = 0

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

    func activeOperation(for key: OperationLaneKey) -> ActiveOperation? {
        lane(for: key).activeOperation
    }

    func activeOperation(for profile: DeviceProfile) -> ActiveOperation? {
        activeOperation(for: .device(profile.id))
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
            laneKey: profile.map { .device($0.id) } ?? .app
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
        let resolvedLaneKey = laneKey ?? activeDeviceID.map { .device($0) } ?? .app
        let lane = lane(for: resolvedLaneKey)
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

    func cancel() {
        primaryLane()?.cancel()
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
        let active = lanes.compactMapValues(\.activeOperation)
        activeOperations = active
        rejectedOperationMessages = lanes.compactMapValues(\.rejectedOperationMessage)
        rejectedOperationMessage = primaryRejection(from: rejectedOperationMessages)
        let primary = primaryLane()
        activeOperation = primary?.activeOperation
        activeDeviceID = activeOperation?.profileID
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

    private func primaryRejection(from messages: [OperationLaneKey: String]) -> String? {
        if let primaryKey = primaryLane()?.key, let message = messages[primaryKey] {
            return message
        }
        return messages.values.first
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
        case .device(let profileID):
            return "1:\(profileID)"
        case .candidateHost(let host):
            return "2:\(host)"
        case .localPath(let path):
            return "3:\(path)"
        }
    }
}
