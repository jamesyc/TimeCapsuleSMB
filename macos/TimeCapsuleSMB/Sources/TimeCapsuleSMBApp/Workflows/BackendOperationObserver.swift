import Foundation

@MainActor
final class BackendOperationObserver {
    private(set) var activeOperation: ActiveOperation?
    private var lastProcessedEventCount = 0

    func start(_ operation: ActiveOperation) {
        activeOperation = operation
        lastProcessedEventCount = 0
    }

    func clear() {
        activeOperation = nil
        lastProcessedEventCount = 0
    }

    func ignoreExistingEvents(_ events: [BackendEvent]) {
        lastProcessedEventCount = events.count
    }

    func finish() {
        activeOperation = nil
    }

    func process(
        _ events: [BackendEvent],
        handler: (BackendEvent, ActiveOperation) -> Void
    ) {
        if events.count < lastProcessedEventCount {
            lastProcessedEventCount = 0
        }
        guard events.count > lastProcessedEventCount else {
            return
        }
        guard let activeOperation else {
            lastProcessedEventCount = events.count
            return
        }
        for event in events.dropFirst(lastProcessedEventCount) where accepts(event, for: activeOperation) {
            handler(event, activeOperation)
        }
        lastProcessedEventCount = events.count
    }

    private func accepts(_ event: BackendEvent, for activeOperation: ActiveOperation) -> Bool {
        if let requestId = event.requestId {
            return requestId == activeOperation.id.uuidString
        }
        return event.operation == activeOperation.operation
    }
}
