import Combine
import Foundation

enum ActivityScope: Equatable {
    case app
    case device(DeviceProfile.ID)
    case unknown
}

struct ActivitySnapshot: Equatable {
    let isRunning: Bool
    let scope: ActivityScope
    let operation: String?
    let operationTitle: String
    let latestMessage: String?
    let timeline: [OperationTimelineItem]
}

struct ActivityLaneSnapshot: Equatable, Identifiable {
    let laneKey: OperationLaneKey
    let snapshot: ActivitySnapshot
    let isPendingConfirmation: Bool
    let updateSequence: Int

    var id: OperationLaneKey {
        laneKey
    }
}

struct ActivityDisplayContext: Equatable {
    let selectedDeviceID: DeviceProfile.ID?
    let showingAddDevice: Bool
    let showingActivity: Bool

    static let none = ActivityDisplayContext(
        selectedDeviceID: nil,
        showingAddDevice: false,
        showingActivity: false
    )
}

struct ActivityCompactStatus: Equatable {
    let isRunning: Bool
    let requiresAttention: Bool
    let scope: ActivityScope
    let operationTitle: String
    let latestMessage: String?
    let latestTimelineTitle: String?
    let activeLaneCount: Int

    static func from(_ laneSnapshot: ActivityLaneSnapshot, activeLaneCount: Int) -> ActivityCompactStatus {
        ActivityCompactStatus(
            isRunning: laneSnapshot.snapshot.isRunning,
            requiresAttention: laneSnapshot.isPendingConfirmation,
            scope: laneSnapshot.snapshot.scope,
            operationTitle: laneSnapshot.snapshot.operationTitle,
            latestMessage: laneSnapshot.snapshot.latestMessage,
            latestTimelineTitle: laneSnapshot.snapshot.timeline.last?.title,
            activeLaneCount: activeLaneCount
        )
    }
}

@MainActor
final class ActivityStore: ObservableObject {
    @Published private(set) var snapshot = ActivitySnapshot(
        isRunning: false,
        scope: .unknown,
        operation: nil,
        operationTitle: L10n.string("activity.no_active_operation"),
        latestMessage: nil,
        timeline: []
    )
    @Published private(set) var laneSnapshots: [ActivityLaneSnapshot] = []

    private let coordinator: OperationCoordinator
    private var cancellables: Set<AnyCancellable> = []
    private var previousSnapshots: [OperationLaneKey: ActivitySnapshot] = [:]
    private var previousPendingStates: [OperationLaneKey: Bool] = [:]
    private var laneUpdateSequences: [OperationLaneKey: Int] = [:]
    private var nextUpdateSequence = 1

    init(coordinator: OperationCoordinator) {
        self.coordinator = coordinator
        coordinator.$lanesRevision
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        refresh()
    }

    func refresh() {
        laneSnapshots = coordinator.allLanes
            .map { lane in
                let snapshot = snapshot(for: lane)
                let isPendingConfirmation = lane.backend.pendingConfirmation != nil
                let updateSequence = updateSequence(
                    for: lane.key,
                    snapshot: snapshot,
                    isPendingConfirmation: isPendingConfirmation
                )
                return ActivityLaneSnapshot(
                    laneKey: lane.key,
                    snapshot: snapshot,
                    isPendingConfirmation: isPendingConfirmation,
                    updateSequence: updateSequence
                )
            }
            .filter { laneSnapshot in
                laneSnapshot.snapshot.isRunning
                    || laneSnapshot.isPendingConfirmation
                    || !laneSnapshot.snapshot.timeline.isEmpty
            }
            .sorted(by: sortLaneSnapshots)
        snapshot = primarySnapshot(from: laneSnapshots) ?? emptySnapshot()
    }

    var activeLaneSnapshots: [ActivityLaneSnapshot] {
        laneSnapshots.filter { $0.snapshot.isRunning || $0.isPendingConfirmation }
    }

    var recentLaneSnapshots: [ActivityLaneSnapshot] {
        laneSnapshots.filter { !$0.snapshot.isRunning && !$0.isPendingConfirmation }
    }

    var hasActiveActivity: Bool {
        !activeLaneSnapshots.isEmpty
    }

    func compactStatus(for context: ActivityDisplayContext) -> ActivityCompactStatus {
        let active = activeLaneSnapshots
        let activeCount = active.count

        if let selectedDeviceID = context.selectedDeviceID,
           let selected = laneSnapshots.first(where: { isDeviceLane($0, selectedDeviceID: selectedDeviceID) }),
           selected.snapshot.isRunning || selected.isPendingConfirmation {
            return .from(selected, activeLaneCount: activeCount)
        }

        if context.showingAddDevice,
           let configureLane = active.first(where: { laneSnapshot in
               laneSnapshot.laneKey != .app
                   && laneSnapshot.snapshot.operation == "configure"
           }) {
            return .from(configureLane, activeLaneCount: activeCount)
        }

        if context.showingAddDevice || (context.selectedDeviceID == nil && !context.showingActivity),
           let appLane = active.first(where: { $0.laneKey == .app }) {
            return .from(appLane, activeLaneCount: activeCount)
        }

        if activeCount > 1 {
            return ActivityCompactStatus(
                isRunning: active.contains { $0.snapshot.isRunning },
                requiresAttention: active.contains { $0.isPendingConfirmation },
                scope: .unknown,
                operationTitle: L10n.format("activity.multiple_active", activeCount),
                latestMessage: L10n.string("activity.multiple_active.message"),
                latestTimelineTitle: nil,
                activeLaneCount: activeCount
            )
        }

        if let activeLane = active.first {
            return .from(activeLane, activeLaneCount: activeCount)
        }

        if let selectedDeviceID = context.selectedDeviceID,
           let selected = laneSnapshots.first(where: { isDeviceLane($0, selectedDeviceID: selectedDeviceID) }) {
            return .from(selected, activeLaneCount: activeCount)
        }

        if context.showingAddDevice || (context.selectedDeviceID == nil && !context.showingActivity),
           let appLane = laneSnapshots.first(where: { $0.laneKey == .app }) {
            return .from(appLane, activeLaneCount: activeCount)
        }

        if let recent = laneSnapshots.first {
            return .from(recent, activeLaneCount: activeCount)
        }

        let empty = emptySnapshot()
        return ActivityCompactStatus(
            isRunning: empty.isRunning,
            requiresAttention: false,
            scope: empty.scope,
            operationTitle: empty.operationTitle,
            latestMessage: empty.latestMessage,
            latestTimelineTitle: empty.timeline.last?.title,
            activeLaneCount: activeCount
        )
    }

    private func snapshot(for lane: OperationLane) -> ActivitySnapshot {
        let events = lane.backend.events
        let timeline = OperationTimelineBuilder.timeline(from: events)
        let operation = lane.activeOperation?.operation
            ?? lane.backend.activeOperationName
            ?? latestOperation(from: events)
        let isRunning = lane.backend.isRunning
        let presentation = presentation(
            operation: operation,
            events: events,
            timeline: timeline,
            isRunning: isRunning
        )
        return ActivitySnapshot(
            isRunning: isRunning,
            scope: scope(for: lane.key, operation: operation),
            operation: operation,
            operationTitle: presentation.title,
            latestMessage: presentation.message,
            timeline: timeline
        )
    }

    private func presentation(
        operation: String?,
        events: [BackendEvent],
        timeline: [OperationTimelineItem],
        isRunning: Bool
    ) -> (title: String, message: String?) {
        if appReadinessPassed(operation: operation, events: events, isRunning: isRunning) {
            return (L10n.string("activity.app_ready"), nil)
        }

        let title = operation.map(OperationTimelineBuilder.operationTitle)
            ?? (timeline.isEmpty ? L10n.string("activity.no_active_operation") : L10n.string("activity.last_operation"))
        let message = timeline.last?.detail ?? events.last?.summary
        return (title, message)
    }

    private func appReadinessPassed(operation: String?, events: [BackendEvent], isRunning: Bool) -> Bool {
        guard
            !isRunning,
            operation == "validate-install",
            let latestEvent = events.last,
            latestEvent.operation == "validate-install",
            latestEvent.type == "result",
            latestEvent.ok == true
        else {
            return false
        }
        return true
    }

    private func latestOperation(from events: [BackendEvent]) -> String? {
        events.last?.operation
    }

    private func primarySnapshot(from snapshots: [ActivityLaneSnapshot]) -> ActivitySnapshot? {
        if let runningDevice = snapshots.first(where: { laneSnapshot in
            laneSnapshot.snapshot.isRunning && isDeviceScope(laneSnapshot.snapshot.scope)
        }) {
            return runningDevice.snapshot
        }
        if let runningApp = snapshots.first(where: { laneSnapshot in
            laneSnapshot.snapshot.isRunning && laneSnapshot.snapshot.scope == .app
        }) {
            return runningApp.snapshot
        }
        if let pending = snapshots.first(where: \.isPendingConfirmation) {
            return pending.snapshot
        }
        return snapshots.first?.snapshot
    }

    private func scope(for laneKey: OperationLaneKey, operation: String?) -> ActivityScope {
        switch laneKey {
        case .app, .appWorkflow:
            return .app
        case .device(let profileID), .deviceWorkflow(let profileID, _):
            return .device(profileID)
        case .candidateHost, .localPath:
            return isAppOperation(operation) ? .app : .unknown
        }
    }

    private func isDeviceScope(_ scope: ActivityScope) -> Bool {
        if case .device = scope {
            return true
        }
        return false
    }

    private func emptySnapshot() -> ActivitySnapshot {
        ActivitySnapshot(
            isRunning: false,
            scope: .unknown,
            operation: nil,
            operationTitle: L10n.string("activity.no_active_operation"),
            latestMessage: nil,
            timeline: []
        )
    }

    private func isAppOperation(_ operation: String?) -> Bool {
        guard let operation else {
            return false
        }
        return [
            "capabilities",
            "discover",
            "set-telemetry",
            "validate-install",
            "version-check"
        ].contains(operation)
    }

    private func updateSequence(
        for laneKey: OperationLaneKey,
        snapshot: ActivitySnapshot,
        isPendingConfirmation: Bool
    ) -> Int {
        defer {
            previousSnapshots[laneKey] = snapshot
            previousPendingStates[laneKey] = isPendingConfirmation
        }

        if previousSnapshots[laneKey] != snapshot || previousPendingStates[laneKey] != isPendingConfirmation {
            let sequence = nextUpdateSequence
            laneUpdateSequences[laneKey] = sequence
            nextUpdateSequence += 1
            return sequence
        }
        return laneUpdateSequences[laneKey] ?? 0
    }

    private func sortLaneSnapshots(_ left: ActivityLaneSnapshot, _ right: ActivityLaneSnapshot) -> Bool {
        let leftPriority = lanePriority(left)
        let rightPriority = lanePriority(right)
        if leftPriority != rightPriority {
            return leftPriority < rightPriority
        }
        if left.updateSequence != right.updateSequence {
            return left.updateSequence > right.updateSequence
        }
        return left.laneKey.description < right.laneKey.description
    }

    private func lanePriority(_ laneSnapshot: ActivityLaneSnapshot) -> Int {
        if laneSnapshot.isPendingConfirmation {
            return 0
        }
        if laneSnapshot.snapshot.isRunning {
            return 1
        }
        return 2
    }

    private func isDeviceLane(_ laneSnapshot: ActivityLaneSnapshot, selectedDeviceID: DeviceProfile.ID) -> Bool {
        if case .device(let profileID) = laneSnapshot.snapshot.scope {
            return profileID == selectedDeviceID
        }
        if let profileID = laneSnapshot.laneKey.deviceProfileID {
            return profileID == selectedDeviceID
        }
        return false
    }
}
