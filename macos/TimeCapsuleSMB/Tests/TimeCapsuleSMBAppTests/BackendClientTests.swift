import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class BackendClientTests: XCTestCase {
    func testRunPublishesEventsAndResetsState() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "stage", operation: "capabilities", stage: "start"),
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: .object(["ok": .bool(true)]))
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: 50_000_000
        )
        let client = BackendClient(runner: runner, helperPath: "  /tmp/tcapsule  ")

        client.run(operation: "capabilities", params: ["dry_run": .bool(true)], requestID: "request-1")

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
                operation: "capabilities",
                params: ["dry_run": .bool(true)],
                requestID: "request-1",
                context: nil
            )]
        )
        XCTAssertEqual(Set(client.events.compactMap(\.requestId)), Set(["request-1"]))
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

    func testDeinitCancelsActiveRun() async throws {
        let recorder = CancellationRecorder()
        let runner = CancellationObservingRunner(recorder: recorder)
        var client: BackendClient? = BackendClient(runner: runner)

        client?.run(operation: "doctor")
        try await waitUntilAsync {
            await recorder.started
        }

        client = nil

        try await waitUntilAsync {
            await recorder.cancelled
        }
    }

    func testStagePolicyControlsCancellation() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "deploy", ok: true)
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: 50_000_000
        )
        let client = BackendClient(runner: runner)

        client.run(operation: "deploy")
        try await waitUntil {
            client.currentStage == "upload_payload"
        }

        XCTAssertFalse(client.canCancel)
        client.cancel()

        try await waitUntil {
            !client.isRunning
        }
        XCTAssertEqual(client.lastExitCode, 0)
    }

    func testConfirmationRequiredEventPublishesPendingConfirmation() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "confirmation_required",
                    message: "Confirm deploy.",
                    details: .object([
                        "title": .string("Confirm deployment"),
                        "message": .string("Deploy and reboot."),
                        "action_title": .string("Deploy"),
                        "confirmation_id": .string("confirm-1")
                    ])
                )
            ],
            result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        )
        let client = BackendClient(runner: runner)

        client.run(operation: "deploy", params: ["dry_run": .bool(false)])

        try await waitUntil {
            client.pendingConfirmation != nil && !client.isRunning
        }
        XCTAssertEqual(client.pendingConfirmation?.operation, "deploy")
        XCTAssertEqual(client.pendingConfirmation?.params["confirmation_id"], .string("confirm-1"))
        XCTAssertEqual(client.pendingConfirmation?.params["dry_run"], .bool(false))
    }

    func testCancelPendingConfirmationClearsPendingStateAndPublishesCancellationEvent() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "confirmation_required",
                    message: "Confirm deploy.",
                    details: .object(["confirmation_id": .string("confirm-1")])
                )
            ],
            result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        )
        let client = BackendClient(runner: runner)

        client.run(operation: "deploy", params: ["dry_run": .bool(false)])
        try await waitUntil {
            client.pendingConfirmation != nil && !client.isRunning
        }

        client.cancelPendingConfirmation()

        XCTAssertNil(client.pendingConfirmation)
        XCTAssertEqual(client.events.last?.type, "error")
        XCTAssertEqual(client.events.last?.operation, "deploy")
        XCTAssertEqual(client.events.last?.code, "confirmation_cancelled")
        XCTAssertEqual(client.events.last?.message, "Operation cancelled.")
    }

    func testProfileContextInjectsConfigAndPreservesExplicitConfig() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: "")
        )
        let client = BackendClient(runner: runner)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        client.run(operation: "doctor", params: [:], context: context)

        try await waitUntil { !client.isRunning && runner.calls.count == 1 }
        XCTAssertEqual(runner.calls[0].context, context)
        XCTAssertEqual(runner.calls[0].params["config"], .string("/tmp/device-one/.env"))

        client.run(
            operation: "doctor",
            params: ["config": .string("/tmp/manual.env")],
            context: context
        )

        try await waitUntil { !client.isRunning && runner.calls.count == 2 }
        XCTAssertEqual(runner.calls[1].params["config"], .string("/tmp/manual.env"))
    }

    func testConfirmationReplayPreservesDeviceContext() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "confirmation_required",
                    message: "Confirm deploy.",
                    details: .object([
                        "confirmation_id": .string("confirm-1")
                    ])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
            ])
        ])
        let client = BackendClient(runner: runner)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        client.run(operation: "deploy", params: ["dry_run": .bool(false)], context: context)
        try await waitUntil { client.pendingConfirmation != nil && !client.isRunning }
        XCTAssertEqual(client.pendingConfirmation?.context, context)

        client.confirmPending()

        try await waitUntil { !client.isRunning && runner.calls.count == 2 }
        XCTAssertEqual(runner.calls[0].context, context)
        XCTAssertEqual(runner.calls[1].context, context)
        XCTAssertEqual(runner.calls[1].params["confirmation_id"], .string("confirm-1"))
        XCTAssertEqual(runner.calls[1].params["config"], .string("/tmp/device-one/.env"))
    }

    func testOperationCoordinatorRejectsSecondOperationWhileActive() async throws {
        let runner = RecordingHelperRunner(
            events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ],
            result: HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: 200_000_000
        )
        let client = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: client)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))
        let laneKey = OperationLaneKey.device("device-one")
        let deviceLane = coordinator.lane(for: laneKey)

        guard case .started(let activeOperation) = coordinator.run(
            operation: "doctor",
            context: context,
            activeDeviceID: "device-one",
            laneKey: laneKey
        ) else {
            XCTFail("Expected first operation to start.")
            return
        }
        guard case .rejected(let rejectionMessage) = coordinator.run(
            operation: "deploy",
            context: context,
            activeDeviceID: "device-one",
            laneKey: laneKey
        ) else {
            XCTFail("Expected second operation to be rejected.")
            return
        }
        XCTAssertEqual(activeOperation.operation, "doctor")
        XCTAssertEqual(activeOperation.profileID, "device-one")
        XCTAssertEqual(rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(coordinator.rejectedOperationMessage, "Another operation is already running.")
        XCTAssertEqual(coordinator.activeOperation, activeOperation)
        XCTAssertEqual(coordinator.activeDeviceID, "device-one")

        try await waitUntil { !deviceLane.backend.isRunning }
        XCTAssertNil(coordinator.activeOperation)
        XCTAssertNil(coordinator.activeDeviceID)
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

    private func waitUntilAsync(
        timeoutNanoseconds: UInt64 = 2_000_000_000,
        _ condition: @escaping () async -> Bool
    ) async throws {
        let start = DispatchTime.now().uptimeNanoseconds
        while !(await condition()) {
            if DispatchTime.now().uptimeNanoseconds - start > timeoutNanoseconds {
                XCTFail("Timed out waiting for async BackendClient state change.")
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }
}

private actor CancellationRecorder {
    private var didStart = false
    private var didCancel = false

    var started: Bool {
        didStart
    }

    var cancelled: Bool {
        didCancel
    }

    func markStarted() {
        didStart = true
    }

    func markCancelled() {
        didCancel = true
    }
}

private final class CancellationObservingRunner: HelperRunning, @unchecked Sendable {
    private let recorder: CancellationRecorder

    init(recorder: CancellationRecorder) {
        self.recorder = recorder
    }

    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        requestID: String,
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        await recorder.markStarted()
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        await recorder.markCancelled()
        return HelperRunResult(exitCode: 130, sawTerminalEvent: false, stderr: "")
    }
}

private final class RecordingHelperRunner: HelperRunning, @unchecked Sendable {
    struct Call: Equatable, Sendable {
        let helperPath: String?
        let operation: String
        let params: [String: JSONValue]
        let requestID: String
        let context: DeviceRuntimeContext?
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
        requestID: String,
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        queue.sync {
            storedCalls.append(Call(
                helperPath: helperPath,
                operation: operation,
                params: params,
                requestID: requestID,
                context: context
            ))
        }

        if delayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: delayNanoseconds)
        }
        if Task.isCancelled {
            await onEvent(BackendEvent.error(
                operation: operation,
                code: "cancelled",
                message: L10n.string("helper.error.cancelled"),
                requestId: requestID
            ))
            return HelperRunResult(exitCode: 130, sawTerminalEvent: true, stderr: "")
        }
        for event in events {
            await onEvent(event.withRequestId(requestID))
        }
        return result
    }
}
