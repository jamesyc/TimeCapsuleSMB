import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class StoreTestRunner: HelperRunning, @unchecked Sendable {
    struct Call: Equatable, Sendable {
        let helperPath: String?
        let operation: String
        let params: [String: JSONValue]
    }

    struct Response: Sendable {
        let events: [BackendEvent]
        let result: HelperRunResult

        init(
            events: [BackendEvent],
            result: HelperRunResult = HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: "")
        ) {
            self.events = events
            self.result = result
        }
    }

    private let queue = DispatchQueue(label: "TimeCapsuleSMBAppTests.StoreTestRunner")
    private var storedResponses: [Response]
    private var storedCalls: [Call] = []

    init(responses: [Response]) {
        self.storedResponses = responses
    }

    var calls: [Call] {
        queue.sync { storedCalls }
    }

    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        let response = queue.sync {
            storedCalls.append(Call(helperPath: helperPath, operation: operation, params: params))
            if storedResponses.isEmpty {
                return Response(
                    events: [BackendEvent.error(operation: operation, code: "missing_test_response", message: "No test response queued.")],
                    result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
                )
            }
            return storedResponses.removeFirst()
        }

        for event in response.events {
            await onEvent(event)
        }
        return response.result
    }
}

@MainActor
func waitUntilStoreState(
    timeoutNanoseconds: UInt64 = 2_000_000_000,
    _ condition: @escaping @MainActor () -> Bool
) async throws {
    let start = DispatchTime.now().uptimeNanoseconds
    while !condition() {
        if DispatchTime.now().uptimeNanoseconds - start > timeoutNanoseconds {
            XCTFail("Timed out waiting for store state change.")
            return
        }
        try await Task.sleep(nanoseconds: 10_000_000)
    }
}

func recoveryValue(title: String, actions: [String], suggestedOperation: String = "doctor") -> JSONValue {
    .object([
        "title": .string(title),
        "message": .string(title),
        "actions": .array(actions.map(JSONValue.string)),
        "retryable": .bool(true),
        "suggested_operation": .string(suggestedOperation)
    ])
}
