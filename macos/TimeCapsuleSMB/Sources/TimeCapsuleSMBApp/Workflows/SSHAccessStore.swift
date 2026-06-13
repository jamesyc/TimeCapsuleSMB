import Combine
import Foundation

struct DeviceSSHAccessSnapshot: Equatable {
    let refreshedAt: Date
    let payload: SSHAccessPayload
}

struct SSHAccessNotice: Equatable {
    let profileID: DeviceProfile.ID
    let deviceName: String
    let host: String
}

@MainActor
final class DeviceSSHAccessStore: ObservableObject {
    @Published private(set) var snapshots: [DeviceProfile.ID: DeviceSSHAccessSnapshot] = [:]
    @Published private(set) var errors: [DeviceProfile.ID: BackendErrorViewModel] = [:]
    @Published private(set) var currentStages: [DeviceProfile.ID: OperationStageState] = [:]

    private static let automaticRefreshInterval: TimeInterval = 60

    private let coordinator: OperationCoordinator
    private let now: () -> Date
    private var operationObservers: [DeviceProfile.ID: BackendOperationObserver] = [:]
    private var observedProfiles: Set<DeviceProfile.ID> = []
    private var cancellablesByProfile: [DeviceProfile.ID: Set<AnyCancellable>] = [:]

    init(coordinator: OperationCoordinator, now: @escaping () -> Date = Date.init) {
        self.coordinator = coordinator
        self.now = now
    }

    func refresh(profile: DeviceProfile) {
        runRefresh(profile: profile)
    }

    func refreshIfNeeded(profile: DeviceProfile) {
        guard !coordinator.isDeviceBusy(profile.id) else {
            return
        }
        if let snapshot = snapshots[profile.id],
           now().timeIntervalSince(snapshot.refreshedAt) < Self.automaticRefreshInterval {
            return
        }
        runRefresh(profile: profile)
    }

    private func runRefresh(profile: DeviceProfile) {
        let laneKey = OperationLaneKey.deviceWorkflow(profile.id, .sshAccess)
        let lane = coordinator.lane(for: laneKey)
        observeLane(for: profile.id, lane: lane)
        guard !lane.isBusy else {
            reject(profileID: profile.id, message: L10n.string("operation.error.already_running"))
            return
        }
        lane.clear()
        errors[profile.id] = nil
        currentStages[profile.id] = nil
        observer(for: profile.id).clear()
        switch coordinator.run(
            operation: "ssh-access",
            params: OperationParams.SSHAccess.status(),
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            laneKey: laneKey
        ) {
        case .started(let operation):
            observer(for: profile.id).start(operation)
            process(lane.backend.events, profileID: profile.id)
        case .rejected(let message):
            reject(profileID: profile.id, message: message)
        }
    }

    func notice(for profile: DeviceProfile, staleEndpointNotice: StaleEndpointNotice?) -> SSHAccessNotice? {
        guard staleEndpointNotice == nil,
              let snapshot = snapshots[profile.id],
              snapshot.payload.isSSHDisabledLikely else {
            return nil
        }
        return SSHAccessNotice(
            profileID: profile.id,
            deviceName: profile.title,
            host: snapshot.payload.host
        )
    }

    func snapshot(for profile: DeviceProfile) -> DeviceSSHAccessSnapshot? {
        snapshots[profile.id]
    }

    func apply(payload: SSHAccessPayload, profile: DeviceProfile) {
        snapshots[profile.id] = DeviceSSHAccessSnapshot(refreshedAt: now(), payload: payload)
        errors[profile.id] = nil
    }

    func error(for profile: DeviceProfile) -> BackendErrorViewModel? {
        errors[profile.id]
    }

    private func reject(profileID: DeviceProfile.ID, message: String) {
        errors[profileID] = BackendErrorViewModel(
            operation: "ssh-access",
            code: "operation_rejected",
            message: message
        )
    }

    private func observer(for profileID: DeviceProfile.ID) -> BackendOperationObserver {
        if let observer = operationObservers[profileID] {
            return observer
        }
        let observer = BackendOperationObserver()
        operationObservers[profileID] = observer
        return observer
    }

    private func observeLane(for profileID: DeviceProfile.ID, lane: OperationLane) {
        guard observedProfiles.insert(profileID).inserted else {
            return
        }
        var cancellables: Set<AnyCancellable> = []
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events, profileID: profileID)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .sink { [weak self] isRunning in
                guard !isRunning else { return }
                Task { @MainActor in
                    self?.finishIfLaneStopped(profileID: profileID)
                }
            }
            .store(in: &cancellables)
        cancellablesByProfile[profileID] = cancellables
    }

    private func process(_ events: [BackendEvent], profileID: DeviceProfile.ID) {
        observer(for: profileID).process(events) { event, _ in
            handle(event, profileID: profileID)
        }
    }

    private func handle(_ event: BackendEvent, profileID: DeviceProfile.ID) {
        guard event.operation == "ssh-access" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStages[profileID] = stage
            return
        }
        switch event.type {
        case "result":
            applyResult(event, profileID: profileID)
        case "error":
            errors[profileID] = BackendErrorViewModel(event: event)
            operationObservers[profileID]?.finish()
        default:
            break
        }
    }

    private func applyResult(_ event: BackendEvent, profileID: DeviceProfile.ID) {
        do {
            let payload = try event.decodePayload(SSHAccessPayload.self)
            snapshots[profileID] = DeviceSSHAccessSnapshot(refreshedAt: now(), payload: payload)
            errors[profileID] = nil
        } catch {
            errors[profileID] = BackendErrorViewModel(
                operation: "ssh-access",
                code: "contract_error",
                message: error.localizedDescription
            )
        }
        operationObservers[profileID]?.finish()
    }

    private func finishIfLaneStopped(profileID: DeviceProfile.ID) {
        if coordinator.activeOperation(for: .deviceWorkflow(profileID, .sshAccess))?.operation != "ssh-access" {
            operationObservers[profileID]?.finish()
        }
    }
}

@MainActor
final class SSHAccessMaintenanceStore: ObservableObject {
    @Published private(set) var state: MaintenanceOperationState = .idle
    @Published private(set) var payload: SSHAccessPayload?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    private let operation: MaintenanceWorkflowOperation

    init(backend: BackendClient, coordinator: OperationCoordinator? = nil, laneKey: OperationLaneKey? = nil) {
        self.operation = MaintenanceWorkflowOperation(
            name: "ssh-access",
            backend: backend,
            coordinator: coordinator,
            laneKey: laneKey
        )
        self.operation.bind(onEvent: { [weak self] event, activeOperation in
            self?.handle(event, activeOperation: activeOperation)
        }, onRunningChanged: { [weak self] in
            self?.objectWillChange.send()
        })
    }

    var events: [BackendEvent] { operation.events }
    var isRunning: Bool { operation.isRunning }
    var isBusy: Bool { operation.isBusy }
    var canCancel: Bool { operation.canCancel }
    var pendingConfirmation: PendingConfirmation? { operation.pendingConfirmation }

    var canCheck: Bool {
        !isBusy
    }

    var canEnable: Bool {
        !isBusy
    }

    func confirmPending() {
        operation.confirmPending()
    }

    func cancelPendingConfirmation() {
        operation.cancelPendingConfirmation()
    }

    func cancel() {
        operation.cancel()
    }

    func clear() {
        operation.clear()
        state = .idle
        payload = nil
        currentStage = nil
        error = nil
        passwordInvalidProfileID = nil
    }

    @discardableResult
    func check(profile: DeviceProfile? = nil) -> OperationStartResult {
        startRun(params: OperationParams.SSHAccess.status(), profile: profile, password: nil, runningState: .loading)
    }

    @discardableResult
    func enable(password: String, noWait: Bool, profile: DeviceProfile? = nil) -> OperationStartResult {
        startRun(
            params: OperationParams.SSHAccess.enable(noWait: noWait),
            profile: profile,
            password: password,
            runningState: .running
        )
    }

    @discardableResult
    func rejectAlreadyRunning() -> OperationStartResult {
        rejectRun(.operationAlreadyRunning)
        return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
    }

    private func startRun(
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String?,
        runningState: MaintenanceOperationState
    ) -> OperationStartResult {
        let start = operation.start(
            params: params,
            profile: profile,
            password: password,
            rejectAlreadyRunning: { rejectRun(.operationAlreadyRunning) },
            resetRunState: resetRunState,
            rejectRun: rejectRun(message:)
        )
        guard case .started = start else {
            return start
        }
        state = runningState
        payload = nil
        return start
    }

    private func resetRunState() {
        operation.resetForRun()
        error = nil
        currentStage = nil
        passwordInvalidProfileID = nil
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == operation.name else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .running
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

        do {
            payload = try event.decodePayload(SSHAccessPayload.self)
            state = .succeeded
            error = nil
            operation.finishObserver()
        } catch {
            failContract(error)
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            error = nil
            currentStage = nil
            operation.finishObserver()
            state = payload == nil ? .idle : .succeeded
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .failed
        operation.finishObserver()
    }

    private func applyFalseResult(_ event: BackendEvent) {
        error = operation.falseResultError(from: event)
        state = .failed
        operation.finishObserver()
    }

    private func failContract(_ decodeError: Error) {
        error = operation.contractDecodeError(decodeError)
        state = .failed
        operation.finishObserver()
    }

    private func rejectRun(_ localError: WorkflowLocalError) {
        error = operation.localError(localError)
        currentStage = nil
        state = .failed
        operation.finishObserver()
    }

    private func rejectRun(message: String) {
        error = operation.rejectedError(message: message)
        currentStage = nil
        state = .failed
        operation.finishObserver()
    }
}
