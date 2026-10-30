import Combine
import Foundation

struct DeviceReachabilitySnapshot: Equatable {
    /// When the check that read it started, not when its result arrived: an
    /// install can start while a background check runs, and that check read
    /// the device before it.
    let refreshedAt: Date
    let payload: ReachabilityPayload
}

/// One snapshot per device, fed by two lanes: Refresh Status's ordinary
/// `.reachability` run, whose stages and errors the page shows, and the
/// device page's background `.reachabilityStatus` check, which only updates
/// the snapshot.
@MainActor
final class DeviceReachabilityStore: ObservableObject {
    @Published private(set) var snapshots: [DeviceProfile.ID: DeviceReachabilitySnapshot] = [:]
    @Published private(set) var errors: [DeviceProfile.ID: BackendErrorViewModel] = [:]
    @Published private(set) var currentStages: [DeviceProfile.ID: OperationStageState] = [:]

    private static let automaticRefreshInterval: TimeInterval = 60

    private let coordinator: OperationCoordinator
    private let now: () -> Date
    private var operationObservers: [OperationLaneKey: BackendOperationObserver] = [:]
    private var observedLanes: Set<OperationLaneKey> = []
    private var cancellablesByLane: [OperationLaneKey: Set<AnyCancellable>] = [:]
    /// Each run's place in start order, and that of the run each snapshot
    /// came from. Refresh Status may start while the background check runs;
    /// whichever finishes last, the later-started run's result stays.
    private var startedRuns = 0
    private var runNumbers: [OperationLaneKey: Int] = [:]
    private var runStarts: [OperationLaneKey: Date] = [:]
    private var snapshotRunNumbers: [DeviceProfile.ID: Int] = [:]

    init(coordinator: OperationCoordinator, now: @escaping () -> Date = Date.init) {
        self.coordinator = coordinator
        self.now = now
    }

    /// Refresh Status.
    func refresh(profile: DeviceProfile, password: String?) {
        run(profile: profile, password: password, laneKey: .deviceWorkflow(profile.id, .reachability))
    }

    /// The device page's background check. Skipped while the device runs
    /// something you started or any background check, while the last result
    /// is under a minute old, and without a password, whose failed login
    /// would read as a wrong one. `password` is read from the keychain only
    /// when the check would run.
    func refreshIfNeeded(profile: DeviceProfile, password: @autoclosure () -> String?) {
        guard !coordinator.isDeviceBusy(profile.id), !coordinator.isRunningBackgroundCheck(profile.id) else {
            return
        }
        if let snapshot = snapshots[profile.id],
           now().timeIntervalSince(snapshot.refreshedAt) < Self.automaticRefreshInterval {
            return
        }
        guard let password = password() else { return }
        run(profile: profile, password: password, laneKey: .deviceWorkflow(profile.id, .reachabilityStatus))
    }

    private func run(profile: DeviceProfile, password: String?, laneKey: OperationLaneKey) {
        let lane = coordinator.lane(for: laneKey)
        let background = laneKey.isBackground
        observeLane(laneKey, profileID: profile.id, lane: lane)
        guard !lane.isBusy else {
            // The background check already running answers a second request.
            guard !background else { return }
            operationObservers[laneKey]?.clear()
            errors[profile.id] = BackendErrorViewModel(
                operation: "reachability",
                code: "operation_rejected",
                message: L10n.string("operation.error.already_running")
            )
            return
        }
        lane.clear()
        if !background {
            errors[profile.id] = nil
            currentStages[profile.id] = nil
        }
        observer(for: laneKey).clear()
        switch coordinator.run(
            operation: "reachability",
            params: OperationParams.Reachability.check(profile: profile),
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            password: password,
            laneKey: laneKey
        ) {
        case .started(let operation):
            startedRuns += 1
            runNumbers[laneKey] = startedRuns
            runStarts[laneKey] = now()
            observer(for: laneKey).start(operation)
            process(lane.backend.events, laneKey: laneKey, profileID: profile.id)
        case .rejected(let message):
            operationObservers[laneKey]?.clear()
            guard !background else { return }
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

    /// Whether Refresh Status runs; the background check gives way to the
    /// page's actions, so it does not count.
    func isRunning(profile: DeviceProfile) -> Bool {
        let laneKey = OperationLaneKey.deviceWorkflow(profile.id, .reachability)
        return operationObservers[laneKey]?.activeOperation != nil
            || coordinator.activeOperation(for: laneKey)?.operation == "reachability"
    }

    private func observer(for laneKey: OperationLaneKey) -> BackendOperationObserver {
        if let observer = operationObservers[laneKey] {
            return observer
        }
        let observer = BackendOperationObserver()
        operationObservers[laneKey] = observer
        return observer
    }

    private func observeLane(_ laneKey: OperationLaneKey, profileID: DeviceProfile.ID, lane: OperationLane) {
        guard observedLanes.insert(laneKey).inserted else {
            return
        }
        var cancellables: Set<AnyCancellable> = []
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events, laneKey: laneKey, profileID: profileID)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .sink { [weak self] isRunning in
                guard !isRunning else { return }
                Task { @MainActor in
                    self?.finishIfLaneStopped(laneKey)
                }
            }
            .store(in: &cancellables)
        cancellablesByLane[laneKey] = cancellables
    }

    private func process(_ events: [BackendEvent], laneKey: OperationLaneKey, profileID: DeviceProfile.ID) {
        observer(for: laneKey).process(events) { event, _ in
            handle(event, laneKey: laneKey, profileID: profileID)
        }
    }

    private func handle(_ event: BackendEvent, laneKey: OperationLaneKey, profileID: DeviceProfile.ID) {
        guard event.operation == "reachability" else {
            return
        }
        let background = laneKey.isBackground
        if let stage = OperationStageState(event: event) {
            if !background {
                currentStages[profileID] = stage
            }
            return
        }
        switch event.type {
        case "result":
            applyResult(event, laneKey: laneKey, profileID: profileID)
        case "error":
            if !background {
                errors[profileID] = BackendErrorViewModel(event: event)
            }
            operationObservers[laneKey]?.finish()
        default:
            break
        }
    }

    private func applyResult(_ event: BackendEvent, laneKey: OperationLaneKey, profileID: DeviceProfile.ID) {
        let background = laneKey.isBackground
        do {
            let payload = try event.decodePayload(ReachabilityPayload.self)
            let run = runNumbers[laneKey] ?? 0
            if run >= snapshotRunNumbers[profileID, default: 0] {
                snapshots[profileID] = DeviceReachabilitySnapshot(refreshedAt: runStarts[laneKey] ?? now(), payload: payload)
                snapshotRunNumbers[profileID] = run
            }
            if !background {
                errors[profileID] = nil
            }
        } catch {
            if !background {
                errors[profileID] = BackendErrorViewModel(
                    operation: "reachability",
                    code: "contract_error",
                    message: error.localizedDescription
                )
            }
        }
        operationObservers[laneKey]?.finish()
    }

    private func finishIfLaneStopped(_ laneKey: OperationLaneKey) {
        if coordinator.activeOperation(for: laneKey)?.operation != "reachability" {
            operationObservers[laneKey]?.finish()
        }
    }
}
