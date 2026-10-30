import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceReachabilityStoreTests: XCTestCase {
    func testRefreshRunsReachabilityOnWorkflowLaneAndStoresSnapshot() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "reachability", stage: "check_ssh_port"),
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator, now: { Date(timeIntervalSince1970: 123) })
        let profile = try makeProfile(host: "10.0.0.2")

        store.refresh(profile: profile, password: "pw")
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }

        XCTAssertEqual(runner.calls.map(\.operation), ["reachability"])
        XCTAssertEqual(runner.calls[0].context?.profileID, profile.id)
        XCTAssertEqual(runner.calls[0].params["ssh_host"], .string("root@10.0.0.2"))
        // Only the saved target: the helper checks SMB there as well.
        XCTAssertNil(runner.calls[0].params["smb_hosts"])
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(store.snapshot(for: profile)?.payload.status, "reachable")
        XCTAssertEqual(store.snapshot(for: profile)?.refreshedAt, Date(timeIntervalSince1970: 123))
        XCTAssertNil(store.error(for: profile))
        XCTAssertEqual(coordinator.lane(for: .deviceWorkflow(profile.id, .reachability)).backend.events.last?.operation, "reachability")
    }

    func testRefreshCanRunWithoutPassword() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload(
                    status: "partial",
                    summary: "SSH reachable, SMB port closed."
                ))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile(host: "root@10.0.0.2")

        store.refresh(profile: profile, password: nil)
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }

        XCTAssertNil(runner.calls[0].params["credentials"])
        XCTAssertEqual(store.snapshot(for: profile)?.payload.status, "partial")
    }

    func testErrorEventIsStoredPerProfile() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "reachability", code: "operation_failed", message: "failed")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()

        store.refresh(profile: profile, password: "pw")
        try await waitUntilStoreState { store.error(for: profile) != nil }

        XCTAssertEqual(store.error(for: profile)?.message, "failed")
        XCTAssertNil(store.snapshot(for: profile))
    }

    func testRefreshDoesNotClearBusyDeviceLane() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "doctor", stage: "run_checks")
            ], pauseAfterEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()

        coordinator.run(
            operation: "doctor",
            params: [:],
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            laneKey: .device(profile.id)
        )
        try await waitUntilStoreState {
            coordinator.activeOperation(for: profile)?.operation == "doctor" &&
            runner.calls.map(\.operation) == ["doctor"]
        }

        store.refresh(profile: profile, password: "pw")

        XCTAssertEqual(coordinator.activeOperation(for: profile)?.operation, "doctor")
        XCTAssertEqual(store.error(for: profile)?.code, "operation_rejected")
        XCTAssertEqual(runner.calls.map(\.operation), ["doctor"])
        runner.finishAll()
    }

    func testRefreshIsRejectedWhileDeployWorkflowOwnsSameDeviceResource() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd")
            ], pauseAfterEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()
        let deployLane = OperationLaneKey.deviceWorkflow(profile.id, .deploy)

        coordinator.run(
            operation: "deploy",
            params: [:],
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            laneKey: deployLane
        )
        try await waitUntilStoreState {
            coordinator.lane(for: deployLane).backend.isRunning &&
            runner.calls.map(\.operation) == ["deploy"]
        }

        store.refresh(profile: profile, password: "pw")

        XCTAssertEqual(store.error(for: profile)?.code, "operation_rejected")
        XCTAssertEqual(runner.calls.map(\.operation), ["deploy"])
        XCTAssertTrue(coordinator.lane(for: deployLane).backend.isRunning)
        XCTAssertTrue(coordinator.lane(for: .deviceWorkflow(profile.id, .reachability)).backend.events.isEmpty)
        runner.finishAll()
    }

    func testAutomaticCheckRunsInTheBackgroundAndSkipsARecentSnapshot() async throws {
        var now = Date(timeIntervalSince1970: 100)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())]),
            .init(events: [BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload(status: "partial"))])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator, now: { now })
        let profile = try makeProfile()
        let background = coordinator.lane(for: .deviceWorkflow(profile.id, .reachabilityStatus))

        store.refreshIfNeeded(profile: profile, password: "pw")
        try await waitUntilStoreState { store.snapshot(for: profile) != nil && !background.isBusy }
        XCTAssertEqual(background.backend.events.last?.operation, "reachability")
        XCTAssertTrue(coordinator.lane(for: .deviceWorkflow(profile.id, .reachability)).backend.events.isEmpty)
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))

        now = Date(timeIntervalSince1970: 159)
        store.refreshIfNeeded(profile: profile, password: "pw")
        XCTAssertEqual(runner.calls.count, 1)

        now = Date(timeIntervalSince1970: 161)
        store.refreshIfNeeded(profile: profile, password: "pw")
        try await waitUntilStoreState { store.snapshot(for: profile)?.payload.status == "partial" }
        XCTAssertEqual(runner.calls.count, 2)
    }

    func testAutomaticCheckReadsThePasswordOnlyWhenItRuns() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()
        var keychainReads = 0
        func password() -> String? { keychainReads += 1; return "pw" }
        defer { runner.finishAll() }

        store.refreshIfNeeded(profile: profile, password: password())
        XCTAssertEqual(keychainReads, 1)
        // Already running: skipped before the keychain is read.
        store.refreshIfNeeded(profile: profile, password: password())
        XCTAssertEqual(keychainReads, 1)
        runner.finishAll()
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }
        // A fresh snapshot: skipped too.
        store.refreshIfNeeded(profile: profile, password: password())
        XCTAssertEqual(keychainReads, 1)
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertNil(store.error(for: profile))
    }

    func testASnapshotRecordsWhenItsCheckStarted() async throws {
        var now = Date(timeIntervalSince1970: 100)
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())],
                  pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator, now: { now })
        let profile = try makeProfile()

        store.refreshIfNeeded(profile: profile, password: "pw")
        try await waitUntilStoreState { runner.calls.count == 1 }
        now = Date(timeIntervalSince1970: 108)
        runner.finishAll()
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }

        // Its start, not the result's arrival at 108.
        XCTAssertEqual(store.snapshot(for: profile)?.refreshedAt, Date(timeIntervalSince1970: 100))
    }

    func testAutomaticCheckNeedsAPassword() async throws {
        let runner = StoreTestRunner(responses: [])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()

        // Without one the login would fail and read as a wrong password.
        store.refreshIfNeeded(profile: profile, password: nil)

        await runQueuedStoreUpdates()
        XCTAssertTrue(runner.calls.isEmpty)
        XCTAssertNil(store.snapshot(for: profile))
        XCTAssertNil(store.error(for: profile))
    }

    func testAutomaticCheckSkipsADeviceBusyWithYourRun() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd")], pauseAfterEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()
        defer { runner.finishAll() }
        coordinator.run(operation: "deploy", params: [:], context: profile.runtimeContext, activeDeviceID: profile.id,
                        laneKey: .deviceWorkflow(profile.id, .deploy))

        store.refreshIfNeeded(profile: profile, password: "pw")

        try await waitUntilStoreState { runner.calls.count == 1 }
        await runQueuedStoreUpdates()
        XCTAssertEqual(runner.calls.map(\.operation), ["deploy"])
        XCTAssertNil(store.error(for: profile))
    }

    func testAutomaticCheckShowsNoProgressOrErrorAndCountsAsNoWork() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "reachability", stage: "check_ssh_port"),
                BackendEvent(type: "error", operation: "reachability", code: "operation_failed", message: "failed")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""), pauseAfterEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let store = DeviceReachabilityStore(coordinator: coordinator)
        let profile = try makeProfile()
        let background = coordinator.lane(for: .deviceWorkflow(profile.id, .reachabilityStatus))
        defer { runner.finishAll() }

        store.refreshIfNeeded(profile: profile, password: "pw")
        try await waitUntilStoreState { background.backend.events.count == 2 }
        await runQueuedStoreUpdates()

        XCTAssertTrue(background.isBusy)
        // The page's buttons, quitting and Activity all ignore it.
        XCTAssertFalse(store.isRunning(profile: profile))
        XCTAssertFalse(coordinator.isDeviceBusy(profile.id))
        XCTAssertFalse(coordinator.hasBlockingWork)
        activity.refresh()
        XCTAssertTrue(activity.laneSnapshots.isEmpty)
        // Its progress and failure stay off the page.
        XCTAssertNil(store.currentStage(for: profile))
        XCTAssertNil(store.error(for: profile))
        XCTAssertNil(store.snapshot(for: profile))
    }

    func testOverlappingRunsKeepTheLaterStartedResult() async throws {
        for refreshFinishesFirst in [true, false] {
            let runner = OperationKeyedStoreTestRunner(responses: [
                .init("reachability", profileID: "device-one"): [
                    .init(events: [], pauseAfterEvents: true),
                    .init(events: [], pauseAfterEvents: true)
                ]
            ])
            let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
            let store = DeviceReachabilityStore(coordinator: coordinator)
            let profile = try makeProfile()
            let backgroundKey = OperationLaneKey.deviceWorkflow(profile.id, .reachabilityStatus)
            let refreshKey = OperationLaneKey.deviceWorkflow(profile.id, .reachability)

            // The background check starts first; Refresh Status starts while it runs.
            store.refreshIfNeeded(profile: profile, password: "pw")
            store.refresh(profile: profile, password: "pw")
            try await waitUntilStoreState { runner.calls.count == 2 }
            let backgroundID = try XCTUnwrap(coordinator.activeOperation(for: backgroundKey)?.id.uuidString)
            let refreshID = try XCTUnwrap(coordinator.activeOperation(for: refreshKey)?.id.uuidString)
            func result(_ status: String, to requestID: String) async {
                await runner.send(BackendEvent(requestId: requestID, type: "result", operation: "reachability", ok: true,
                                               payload: testReachabilityPayload(status: status)), to: requestID)
            }
            if refreshFinishesFirst {
                await result("reachable", to: refreshID)
                await result("partial", to: backgroundID)
            } else {
                await result("partial", to: backgroundID)
                await result("reachable", to: refreshID)
            }
            runner.finishAll()
            try await waitUntilStoreState {
                !coordinator.lane(for: backgroundKey).isBusy && !coordinator.lane(for: refreshKey).isBusy
            }
            await runQueuedStoreUpdates()

            XCTAssertEqual(store.snapshot(for: profile)?.payload.status, "reachable", "refresh finishes first: \(refreshFinishesFirst)")
        }
    }

    private func makeProfile(host: String = "10.0.0.2") throws -> DeviceProfile {
        DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: host),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }
}
