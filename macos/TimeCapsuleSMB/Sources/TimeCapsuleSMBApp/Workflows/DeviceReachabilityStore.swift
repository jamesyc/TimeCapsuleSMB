import Combine
import Foundation

struct DeviceReachabilitySnapshot: Equatable {
    let refreshedAt: Date
    let payload: ReachabilityPayload
}

@MainActor
final class DeviceReachabilityStore: ObservableObject {
    @Published private(set) var snapshots: [DeviceProfile.ID: DeviceReachabilitySnapshot] = [:]
    @Published private(set) var errors: [DeviceProfile.ID: BackendErrorViewModel] = [:]
    @Published private(set) var currentStages: [DeviceProfile.ID: OperationStageState] = [:]

    private let coordinator: OperationCoordinator
    private let now: () -> Date
    private var operationObservers: [DeviceProfile.ID: BackendOperationObserver] = [:]
    private var observedProfiles: Set<DeviceProfile.ID> = []
    private var cancellablesByProfile: [DeviceProfile.ID: Set<AnyCancellable>] = [:]

    init(coordinator: OperationCoordinator, now: @escaping () -> Date = Date.init) {
        self.coordinator = coordinator
        self.now = now
    }

    func refresh(profile: DeviceProfile, password: String?) {
        let laneKey = OperationLaneKey.deviceWorkflow(profile.id, .reachability)
        let lane = coordinator.lane(for: laneKey)
        observeLane(for: profile.id, lane: lane)
        guard !lane.isBusy else {
            operationObservers[profile.id]?.clear()
            errors[profile.id] = BackendErrorViewModel(
                operation: "reachability",
                code: "operation_rejected",
                message: L10n.string("operation.error.already_running")
            )
            return
        }
        lane.clear()
        errors[profile.id] = nil
        currentStages[profile.id] = nil
        observer(for: profile.id).clear()
        switch coordinator.run(
            operation: "reachability",
            params: OperationParams.reachability(profile: profile, password: password),
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            laneKey: laneKey
        ) {
        case .started(let operation):
            observer(for: profile.id).start(operation)
            process(lane.backend.events, profileID: profile.id)
        case .rejected(let message):
            operationObservers[profile.id]?.clear()
            errors[profile.id] = BackendErrorViewModel(
                operation: "reachability",
                code: "operation_rejected",
                message: message
            )
        }
    }

    func snapshot(for profile: DeviceProfile) -> DeviceReachabilitySnapshot? {
        snapshots[profile.id]
    }

    func error(for profile: DeviceProfile) -> BackendErrorViewModel? {
        errors[profile.id]
    }

    func currentStage(for profile: DeviceProfile) -> OperationStageState? {
        currentStages[profile.id]
    }

    func isRunning(profile: DeviceProfile) -> Bool {
        operationObservers[profile.id]?.activeOperation != nil
            || coordinator.activeOperation(for: .deviceWorkflow(profile.id, .reachability))?.operation == "reachability"
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
        guard event.operation == "reachability" else {
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
            let payload = try event.decodePayload(ReachabilityPayload.self)
            snapshots[profileID] = DeviceReachabilitySnapshot(refreshedAt: now(), payload: payload)
            errors[profileID] = nil
        } catch {
            errors[profileID] = BackendErrorViewModel(
                operation: "reachability",
                code: "contract_error",
                message: error.localizedDescription
            )
        }
        operationObservers[profileID]?.finish()
    }

    private func finishIfLaneStopped(profileID: DeviceProfile.ID) {
        if coordinator.activeOperation(for: .deviceWorkflow(profileID, .reachability))?.operation != "reachability" {
            operationObservers[profileID]?.finish()
        }
    }
}
