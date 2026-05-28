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
}
