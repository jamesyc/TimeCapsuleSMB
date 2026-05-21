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
    let operationTitle: String
    let latestMessage: String?
    let timeline: [OperationTimelineItem]
}

@MainActor
final class ActivityStore: ObservableObject {
    @Published private(set) var snapshot = ActivitySnapshot(
        isRunning: false,
        scope: .unknown,
        operationTitle: L10n.string("activity.no_active_operation"),
        latestMessage: nil,
        timeline: []
    )

    private let coordinator: OperationCoordinator
    private var cancellables: Set<AnyCancellable> = []

    init(coordinator: OperationCoordinator) {
        self.coordinator = coordinator
        coordinator.$activeOperation
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        coordinator.$activeDeviceID
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        coordinator.backend.$events
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        coordinator.backend.$isRunning
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        coordinator.backend.$activeOperationName
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.refresh()
                }
            }
            .store(in: &cancellables)
        refresh()
    }

    func refresh() {
        let events = coordinator.backend.events
        let timeline = OperationTimelineBuilder.timeline(from: events)
        let latestMessage = timeline.last?.detail ?? events.last?.summary
        let operation = coordinator.activeOperation?.operation
            ?? coordinator.backend.activeOperationName
            ?? latestOperation(from: events)
        let scope: ActivityScope
        if let activeDeviceID = coordinator.activeDeviceID {
            scope = .device(activeDeviceID)
        } else if isAppOperation(operation) {
            scope = .app
        } else {
            scope = .unknown
        }
        snapshot = ActivitySnapshot(
            isRunning: coordinator.backend.isRunning,
            scope: scope,
            operationTitle: operation.map(OperationTimelineBuilder.operationTitle)
                ?? (timeline.isEmpty ? L10n.string("activity.no_active_operation") : L10n.string("activity.last_operation")),
            latestMessage: latestMessage,
            timeline: timeline
        )
    }

    private func latestOperation(from events: [BackendEvent]) -> String? {
        events.last?.operation
    }

    private func isAppOperation(_ operation: String?) -> Bool {
        guard let operation else {
            return false
        }
        return ["capabilities", "validate-install", "paths"].contains(operation)
    }
}
