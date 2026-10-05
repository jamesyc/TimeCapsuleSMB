import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class BackendOperationObserverTests: XCTestCase {
    func testObserverOnlyDeliversEventsForActiveRequestID() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000123")!,
            operation: "doctor",
            profileID: "device-one",
            context: nil
        )
        observer.start(operation)

        var handled: [BackendEvent] = []
        observer.process([
            BackendEvent(requestId: "stale-request", type: "result", operation: "doctor", ok: true),
            BackendEvent(requestId: operation.id.uuidString, type: "stage", operation: "doctor", stage: "probe"),
            BackendEvent(requestId: operation.id.uuidString, type: "result", operation: "doctor", ok: true)
        ]) { event, _ in
            handled.append(event)
        }

        XCTAssertEqual(handled.map(\.type), ["stage", "result"])
    }

    func testObserverAdvancesCursorAcrossCalls() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(operation: "deploy", profileID: nil, context: nil)
        let first = BackendEvent(requestId: operation.id.uuidString, type: "stage", operation: "deploy", stage: "plan")
        let second = BackendEvent(requestId: operation.id.uuidString, type: "result", operation: "deploy", ok: true)
        observer.start(operation)

        var handled: [BackendEvent] = []
        observer.process([first]) { event, _ in
            handled.append(event)
        }
        observer.process([first, second]) { event, _ in
            handled.append(event)
        }

        XCTAssertEqual(handled.map(\.type), ["stage", "result"])
    }

    // BackendClient replaces a run of progress events in place, keeping only
    // the latest; the observer must still deliver each replacement.
    private func progress(_ entries: Int64, for operation: ActiveOperation) -> BackendEvent {
        BackendEvent(requestId: operation.id.uuidString, type: "progress", operation: "deploy",
                     stage: "migrate_xattrs_copy", entries: entries)
    }

    func testObserverDeliversALastEventReplacedInPlace() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(operation: "deploy", profileID: nil, context: nil)
        let stage = BackendEvent(requestId: operation.id.uuidString, type: "stage", operation: "deploy", stage: "migrate_xattrs_copy")
        observer.start(operation)

        var handled: [Int64?] = []
        let record: (BackendEvent, ActiveOperation) -> Void = { event, _ in handled.append(event.entries) }
        observer.process([stage, progress(1000, for: operation)], handler: record)
        observer.process([stage, progress(2000, for: operation)], handler: record)
        observer.process([stage, progress(3000, for: operation)], handler: record)

        XCTAssertEqual(handled, [nil, 1000, 2000, 3000])
    }

    func testObserverDoesNotRedeliverAnUnchangedList() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(operation: "deploy", profileID: nil, context: nil)
        let events = [progress(1000, for: operation)]
        observer.start(operation)

        var handled: [Int64?] = []
        observer.process(events) { event, _ in handled.append(event.entries) }
        observer.process(events) { event, _ in handled.append(event.entries) }

        XCTAssertEqual(handled, [1000])
    }

    func testObserverDeliversAReplacementAndAnAppendedEventInOrder() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(operation: "deploy", profileID: nil, context: nil)
        let result = BackendEvent(requestId: operation.id.uuidString, type: "result", operation: "deploy", ok: true)
        observer.start(operation)

        var handled: [String] = []
        let record: (BackendEvent, ActiveOperation) -> Void = { event, _ in
            handled.append("\(event.type):\(event.entries.map(String.init) ?? "-")")
        }
        observer.process([progress(1000, for: operation)], handler: record)
        observer.process([progress(2000, for: operation), result], handler: record)

        XCTAssertEqual(handled, ["progress:1000", "progress:2000", "result:-"])
    }

    func testObserverDoesNotDeliverEventsItWasToldToIgnoreOrClearedOf() {
        let observer = BackendOperationObserver()
        let operation = ActiveOperation(operation: "deploy", profileID: nil, context: nil)
        let existing = [progress(1000, for: operation)]
        var handled: [Int64?] = []

        observer.start(operation)
        observer.ignoreExistingEvents(existing)
        observer.process(existing) { event, _ in handled.append(event.entries) }
        XCTAssertEqual(handled, [])

        // Without an active operation, events are skipped but still counted:
        // a later replacement of one of them is not delivered as new.
        observer.clear()
        observer.process(existing) { event, _ in handled.append(event.entries) }
        observer.start(operation)
        observer.ignoreExistingEvents(existing)
        observer.process(existing) { event, _ in handled.append(event.entries) }
        XCTAssertEqual(handled, [])
    }
}
