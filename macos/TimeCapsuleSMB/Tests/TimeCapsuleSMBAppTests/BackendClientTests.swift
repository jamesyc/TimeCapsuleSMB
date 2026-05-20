import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class BackendClientTests: XCTestCase {
    func testRunPublishesEventsAndResetsState() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "stage", operation: "paths", stage: "start"),
                BackendEvent(type: "result", operation: "paths", ok: true, payload: .object(["ok": .bool(true)]))
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: 50_000_000
        )
        let client = BackendClient(runner: runner, helperPath: "  /tmp/tcapsule  ")

        client.run(operation: "paths", params: ["dry_run": .bool(true)])

        XCTAssertTrue(client.isRunning)
        try await waitUntil {
            !client.isRunning && client.events.count == 2
        }
        XCTAssertEqual(client.lastExitCode, 0)
        XCTAssertEqual(client.events.map(\.type), ["stage", "result"])
        XCTAssertEqual(
            runner.calls,
            [RecordingHelperRunner.Call(
                helperPath: "/tmp/tcapsule",
                operation: "paths",
                params: ["dry_run": .bool(true)]
            )]
        )
    }

    func testCancelCancelsDetachedRunAndResetsState() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: 1_000_000_000
        )
        let client = BackendClient(runner: runner)

        client.run(operation: "doctor")
        try await waitUntil {
            runner.calls.count == 1
        }

        client.cancel()

        try await waitUntil {
            !client.isRunning && client.lastExitCode == 130 && client.events.last?.code == "cancelled"
        }
        XCTAssertEqual(client.events.last?.type, "error")
    }

    private func waitUntil(
        timeoutNanoseconds: UInt64 = 2_000_000_000,
        _ condition: @escaping @MainActor () -> Bool
    ) async throws {
        let start = DispatchTime.now().uptimeNanoseconds
        while !condition() {
            if DispatchTime.now().uptimeNanoseconds - start > timeoutNanoseconds {
                XCTFail("Timed out waiting for BackendClient state change.")
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }
}

private final class RecordingHelperRunner: HelperRunning, @unchecked Sendable {
    struct Call: Equatable, Sendable {
        let helperPath: String?
        let operation: String
        let params: [String: JSONValue]
    }

    private let queue = DispatchQueue(label: "TimeCapsuleSMBAppTests.RecordingHelperRunner")
    private let events: [BackendEvent]
    private let result: HelperRunResult
    private let delayNanoseconds: UInt64
    private var storedCalls: [Call] = []

    init(events: [BackendEvent], result: HelperRunResult, delayNanoseconds: UInt64 = 0) {
        self.events = events
        self.result = result
        self.delayNanoseconds = delayNanoseconds
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
        queue.sync {
            storedCalls.append(Call(helperPath: helperPath, operation: operation, params: params))
        }

        if delayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: delayNanoseconds)
        }
        if Task.isCancelled {
            await onEvent(BackendEvent.error(operation: operation, code: "cancelled", message: L10n.string("helper.error.cancelled")))
            return HelperRunResult(exitCode: 130, sawTerminalEvent: true, stderr: "")
        }
        for event in events {
            await onEvent(event)
        }
        return result
    }
}
