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

    private func makeProfile(host: String = "10.0.0.2") throws -> DeviceProfile {
        DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: host),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }
}
