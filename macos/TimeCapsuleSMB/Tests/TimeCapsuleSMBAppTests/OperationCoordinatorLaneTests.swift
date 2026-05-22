import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class OperationCoordinatorLaneTests: XCTestCase {
    func testAppAndDeviceOperationsRunInParallel() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], delayNanoseconds: 200_000_000)
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 200_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let deviceContext = context("device-one")

        XCTAssertStarted(coordinator.run(operation: "discover", laneKey: .app))
        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: deviceContext,
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        ))

        let deviceLane = coordinator.lane(for: .device("device-one"))
        try await waitUntilStoreState {
            runner.calls.count == 2 && coordinator.appLane.backend.isRunning && deviceLane.backend.isRunning
        }
        XCTAssertNil(coordinator.rejectedOperationMessage)
        XCTAssertEqual(Set(coordinator.activeOperations.keys), [.app, .device("device-one")])

        try await waitUntilStoreState {
            !coordinator.appLane.backend.isRunning && !deviceLane.backend.isRunning
        }
    }

    func testSameDeviceLaneRejectsSecondOperation() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 200_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let laneKey = OperationLaneKey.device("device-one")
        let deviceContext = context("device-one")

        XCTAssertStarted(coordinator.run(operation: "doctor", context: deviceContext, activeDeviceID: "device-one", laneKey: laneKey))
        try await waitUntilStoreState { coordinator.lane(for: laneKey).backend.isRunning && runner.calls.count == 1 }
        let second = coordinator.run(operation: "deploy", context: deviceContext, activeDeviceID: "device-one", laneKey: laneKey)

        XCTAssertEqual(second.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(coordinator.rejectedOperationMessages[laneKey], "Another operation is already running.")
        XCTAssertEqual(runner.calls.count, 1)
    }

    func testDifferentDeviceLanesRunSameOperationInParallel() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 200_000_000)
            ],
            .init("doctor", profileID: "device-two"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 200_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))

        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        ))
        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-two"),
            activeDeviceID: "device-two",
            laneKey: .device("device-two")
        ))

        try await waitUntilStoreState {
            runner.calls.count == 2
                && coordinator.lane(for: .device("device-one")).backend.isRunning
                && coordinator.lane(for: .device("device-two")).backend.isRunning
        }
        XCTAssertEqual(Set(runner.calls.compactMap { $0.context?.profileID }), ["device-one", "device-two"])
        XCTAssertEqual(Set(coordinator.activeOperations.keys), [.device("device-one"), .device("device-two")])
    }

    func testAppLaneRejectsSecondAppOperation() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], delayNanoseconds: 200_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))

        XCTAssertStarted(coordinator.run(operation: "discover", laneKey: .app))
        try await waitUntilStoreState { coordinator.appLane.backend.isRunning && runner.calls.count == 1 }
        let second = coordinator.run(operation: "capabilities", laneKey: .app)

        XCTAssertEqual(second.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(coordinator.rejectedOperationMessages[.app], "Another operation is already running.")
        XCTAssertEqual(runner.calls.map(\.operation), ["discover"])
    }

    func testPendingConfirmationBlocksSameLaneButNotOtherLaneAndReplayKeepsContext() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("deploy", profileID: "device-one"): [
                .init(events: [
                    confirmationRequiredEvent(operation: "deploy", id: "deploy-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
                ])
            ],
            .init("doctor", profileID: "device-two"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 100_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let firstLane = OperationLaneKey.device("device-one")

        XCTAssertStarted(coordinator.run(
            operation: "deploy",
            params: ["dry_run": .bool(false)],
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: firstLane
        ))
        try await waitUntilStoreState {
            coordinator.lane(for: firstLane).backend.pendingConfirmation != nil
                && !coordinator.lane(for: firstLane).backend.isRunning
        }

        let sameLaneResult = coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: firstLane
        )
        XCTAssertEqual(sameLaneResult.rejectionMessage, "Another operation is already running.")

        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-two"),
            activeDeviceID: "device-two",
            laneKey: .device("device-two")
        ))
        try await waitUntilStoreState { runner.calls.count == 2 }

        coordinator.confirmPending()
        try await waitUntilStoreState { runner.calls.count == 3 && coordinator.pendingConfirmation == nil }
        XCTAssertEqual(runner.calls[2].operation, "deploy")
        XCTAssertEqual(runner.calls[2].context, context("device-one"))
        XCTAssertEqual(runner.calls[2].params["confirmation_id"], .string("deploy-confirm"))
    }

    func testCancelPendingConfirmationClearsTargetLaneAndPublishesCancellationEvent() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("deploy", profileID: "device-one"): [
                .init(events: [
                    confirmationRequiredEvent(operation: "deploy", id: "deploy-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let laneKey = OperationLaneKey.device("device-one")

        XCTAssertStarted(coordinator.run(
            operation: "deploy",
            params: ["dry_run": .bool(false)],
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: laneKey
        ))
        try await waitUntilStoreState {
            coordinator.lane(for: laneKey).backend.pendingConfirmation != nil
                && !coordinator.lane(for: laneKey).backend.isRunning
        }

        coordinator.cancelPendingConfirmation()

        try await waitUntilStoreState {
            coordinator.pendingConfirmation == nil && coordinator.activeOperations[laneKey] == nil
        }
        let events = coordinator.lane(for: laneKey).backend.events
        XCTAssertEqual(events.last?.code, "confirmation_cancelled")
        XCTAssertEqual(runner.calls.count, 1)
    }

    func testHasActiveWorkTracksRunningAndPendingConfirmation() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("deploy", profileID: "device-one"): [
                .init(events: [
                    confirmationRequiredEvent(operation: "deploy", id: "deploy-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""), delayNanoseconds: 100_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let laneKey = OperationLaneKey.device("device-one")

        XCTAssertFalse(coordinator.hasActiveWork)
        XCTAssertStarted(coordinator.run(
            operation: "deploy",
            params: ["dry_run": JSONValue.bool(false)],
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: laneKey
        ))
        XCTAssertTrue(coordinator.hasActiveWork)

        try await waitUntilStoreState {
            coordinator.lane(for: laneKey).backend.pendingConfirmation != nil
                && !coordinator.lane(for: laneKey).backend.isRunning
        }
        XCTAssertTrue(coordinator.hasActiveWork)

        coordinator.cancelPendingConfirmation()

        try await waitUntilStoreState { !coordinator.hasActiveWork }
    }

    func testCancelOnlyCancelsTargetLane() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], delayNanoseconds: 1_000_000_000)
            ],
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], delayNanoseconds: 500_000_000)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let deviceLaneKey = OperationLaneKey.device("device-one")

        XCTAssertStarted(coordinator.run(operation: "discover", laneKey: .app))
        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: deviceLaneKey
        ))
        try await waitUntilStoreState {
            coordinator.appLane.backend.isRunning && coordinator.lane(for: deviceLaneKey).backend.isRunning
        }

        coordinator.cancel(laneKey: deviceLaneKey)

        try await waitUntilStoreState {
            !coordinator.lane(for: deviceLaneKey).backend.isRunning && coordinator.appLane.backend.isRunning
        }
        XCTAssertEqual(coordinator.lane(for: deviceLaneKey).backend.events.last?.code, "cancelled")
    }

    func testClearingOneLaneDoesNotClearOtherLaneEvents() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ])
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ])
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let deviceLaneKey = OperationLaneKey.device("device-one")

        XCTAssertStarted(coordinator.run(operation: "discover", laneKey: .app))
        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: deviceLaneKey
        ))
        try await waitUntilStoreState {
            !coordinator.appLane.backend.isRunning
                && !coordinator.lane(for: deviceLaneKey).backend.isRunning
                && !coordinator.appLane.backend.events.isEmpty
                && !coordinator.lane(for: deviceLaneKey).backend.events.isEmpty
        }

        coordinator.clear(laneKey: .app)

        XCTAssertTrue(coordinator.appLane.backend.events.isEmpty)
        XCTAssertFalse(coordinator.lane(for: deviceLaneKey).backend.events.isEmpty)
    }

    func testHelperPathChangesSyncToExistingAndNewLanes() async throws {
        let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let existingLane = coordinator.lane(for: .device("device-one"))

        coordinator.backend.helperPath = "/tmp/tcapsule"

        try await waitUntilStoreState { existingLane.backend.helperPath == "/tmp/tcapsule" }
        XCTAssertEqual(existingLane.backend.helperPath, "/tmp/tcapsule")
        XCTAssertEqual(coordinator.lane(for: .device("device-two")).backend.helperPath, "/tmp/tcapsule")
    }

    func testPasswordCredentialInjectionIsScopedToStartedLane() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ])
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))

        XCTAssertStarted(coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            password: "secret",
            laneKey: .device("device-one")
        ))

        try await waitUntilStoreState { runner.calls.count == 1 }
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("secret")]))
        XCTAssertEqual(runner.calls[0].context?.profileID, "device-one")
    }

    private func XCTAssertStarted(
        _ result: OperationStartResult,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        guard case .started = result else {
            XCTFail("Expected operation to start, got \(result).", file: file, line: line)
            return
        }
    }

    private func context(_ profileID: String) -> DeviceRuntimeContext {
        DeviceRuntimeContext(
            profileID: profileID,
            configURL: URL(fileURLWithPath: "/tmp/\(profileID)/.env")
        )
    }

    private func doctorPayload() -> JSONValue {
        testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
        ])
    }

    private func confirmationRequiredEvent(operation: String, id: String) -> BackendEvent {
        BackendEvent(
            type: "error",
            operation: operation,
            code: "confirmation_required",
            message: "Confirm operation.",
            details: .object(["confirmation_id": .string(id)])
        )
    }
}
