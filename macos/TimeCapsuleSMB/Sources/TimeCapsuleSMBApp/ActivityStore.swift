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
        let operation = coordinator.activeOperation?.operation
            ?? coordinator.backend.activeOperationName
            ?? latestOperation(from: events)
        let isRunning = coordinator.backend.isRunning
        let presentation = presentation(
            operation: operation,
            events: events,
            timeline: timeline,
            isRunning: isRunning
        )
        let scope: ActivityScope
        if let activeDeviceID = coordinator.activeDeviceID {
            scope = .device(activeDeviceID)
        } else if isAppOperation(operation) {
            scope = .app
        } else {
            scope = .unknown
        }
        snapshot = ActivitySnapshot(
            isRunning: isRunning,
            scope: scope,
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

    private func isAppOperation(_ operation: String?) -> Bool {
        guard let operation else {
            return false
        }
        return ["capabilities", "validate-install", "paths"].contains(operation)
    }
}
