import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ActivityStoreTests: XCTestCase {
    func testActivitySnapshotTracksActiveOperationTimelineAndDevice() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "deploy",
                    stage: "upload_payload",
                    description: "Upload managed Samba payload files."
                ),
                BackendEvent(
                    type: "result",
                    operation: "deploy",
                    ok: true,
                    payload: .object(["summary": .string("deployment completed.")])
                )
            ], delayNanoseconds: 80_000_000)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        _ = coordinator.run(operation: "deploy", context: context, activeDeviceID: "device-one")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "Install / Update")
        XCTAssertEqual(activity.snapshot.scope, .device("device-one"))

        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.timeline.map(\.title), ["Uploading", "Done"])
        XCTAssertEqual(activity.snapshot.latestMessage, "deployment completed.")
    }

    func testActivitySnapshotTracksBackendOnlyReadinessOperationAsAppScoped() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "capabilities",
                    stage: "start",
                    description: "Inspect helper capabilities."
                ),
                BackendEvent(
                    type: "result",
                    operation: "capabilities",
                    ok: true,
                    payload: .object(["schema_version": .number(1)])
                )
            ], delayNanoseconds: 80_000_000)
        ])
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let activity = ActivityStore(coordinator: coordinator)

        backend.run(operation: "capabilities")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "App Readiness")
        XCTAssertEqual(activity.snapshot.scope, .app)

        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.scope, .app)
        XCTAssertEqual(activity.snapshot.operationTitle, "App Readiness")
    }
}
