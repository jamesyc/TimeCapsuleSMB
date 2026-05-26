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
    private var activeOperations: [DeviceProfile.ID: ActiveOperation] = [:]
    private var lastProcessedEventCounts: [DeviceProfile.ID: Int] = [:]
    private var observedProfiles: Set<DeviceProfile.ID> = []
    private var cancellablesByProfile: [DeviceProfile.ID: Set<AnyCancellable>] = [:]

    init(coordinator: OperationCoordinator, now: @escaping () -> Date = Date.init) {
        self.coordinator = coordinator
        self.now = now
    }

    func refresh(profile: DeviceProfile, password: String?) {
        let laneKey = OperationLaneKey.device(profile.id)
        let lane = coordinator.lane(for: laneKey)
        observeLane(for: profile.id, lane: lane)
        guard !lane.isBusy else {
            activeOperations[profile.id] = nil
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
        lastProcessedEventCounts[profile.id] = 0
        switch coordinator.run(
            operation: "reachability",
            params: OperationParams.reachability(profile: profile, password: password),
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            laneKey: laneKey
        ) {
        case .started(let operation):
            activeOperations[profile.id] = operation
        case .rejected(let message):
            activeOperations[profile.id] = nil
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
        activeOperations[profile.id] != nil
            || coordinator.activeOperation(for: profile)?.operation == "reachability"
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
        let previousCount = lastProcessedEventCounts[profileID] ?? 0
        if events.count < previousCount {
            lastProcessedEventCounts[profileID] = 0
        }
        let start = lastProcessedEventCounts[profileID] ?? 0
        guard events.count > start else {
            return
        }
        for event in events.dropFirst(start) where event.operation == "reachability" {
            handle(event, profileID: profileID)
        }
        lastProcessedEventCounts[profileID] = events.count
    }

    private func handle(_ event: BackendEvent, profileID: DeviceProfile.ID) {
        if let stage = OperationStageState(event: event) {
            currentStages[profileID] = stage
            return
        }
        switch event.type {
        case "result":
            applyResult(event, profileID: profileID)
        case "error":
            errors[profileID] = BackendErrorViewModel(event: event)
            activeOperations[profileID] = nil
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
        activeOperations[profileID] = nil
    }

    private func finishIfLaneStopped(profileID: DeviceProfile.ID) {
        if coordinator.activeOperation(for: .device(profileID))?.operation != "reachability" {
            activeOperations[profileID] = nil
        }
    }
}
