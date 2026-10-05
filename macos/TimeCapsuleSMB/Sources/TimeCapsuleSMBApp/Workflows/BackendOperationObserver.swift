import Foundation

@MainActor
final class BackendOperationObserver {
    private(set) var activeOperation: ActiveOperation?
    private var lastProcessedEventCount = 0
    // BackendClient replaces a run of progress events in place instead of
    // appending each one; the last event's id shows when that happened.
    private var lastProcessedEventID: UUID?

    func start(_ operation: ActiveOperation) {
        activeOperation = operation
        lastProcessedEventCount = 0
        lastProcessedEventID = nil
    }

    func clear() {
        activeOperation = nil
        lastProcessedEventCount = 0
        lastProcessedEventID = nil
    }

    func ignoreExistingEvents(_ events: [BackendEvent]) {
        lastProcessedEventCount = events.count
        lastProcessedEventID = events.last?.id
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
            lastProcessedEventID = nil
        }
        var start = lastProcessedEventCount
        if start > 0, events[start - 1].id != lastProcessedEventID {
            start -= 1
        }
        guard events.count > start else {
            return
        }
        defer {
            lastProcessedEventCount = events.count
            lastProcessedEventID = events.last?.id
        }
        guard let activeOperation else {
            return
        }
        for event in events.dropFirst(start) where accepts(event, for: activeOperation) {
            handler(event, activeOperation)
        }
    }

    private func accepts(_ event: BackendEvent, for activeOperation: ActiveOperation) -> Bool {
        if let requestId = event.requestId {
            return requestId == activeOperation.id.uuidString
        }
        return event.operation == activeOperation.operation
    }
}
