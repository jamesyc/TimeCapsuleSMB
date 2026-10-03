import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class MaintenanceStateForwardingTests: XCTestCase {
    func testChildEventsAreImmediatelyReadableAndNotifyTheParent() async throws {
        let backend = BackendClient(runner: StoreTestRunner(responses: [.init(events: [])]))
        let store = MaintenanceStore(backend: backend)
        var changes = 0
        let observation = store.objectWillChange.sink { changes += 1 }
        defer { observation.cancel() }

        let operation = try XCTUnwrap(store.activationStore.runActivation(password: "pw").operation)
        XCTAssertEqual(store.activateState, .running)
        changes = 0
        store.backend.events.append(BackendEvent(
            requestId: operation.id.uuidString, type: "stage", operation: "activate", stage: "probe_runtime"
        ))
        XCTAssertEqual(store.currentStage(for: .activate)?.stage, "probe_runtime")
        XCTAssertGreaterThan(changes, 0)

        changes = 0
        store.backend.events.append(BackendEvent(
            requestId: operation.id.uuidString, type: "result", operation: "activate", ok: true,
            payload: testActivationResultPayload(alreadyActive: true)
        ))
        XCTAssertEqual(store.activateState, .succeeded)
        XCTAssertEqual(store.activationResult?.alreadyActive, true)
        XCTAssertGreaterThan(changes, 0)
        XCTAssertTrue(store.isBusy, "A result does not finish helper cleanup")

        try await waitUntilStoreState { !store.isBusy }
        store.activationStore.clear()
        XCTAssertEqual(store.activateState, .idle)
        XCTAssertNil(store.activationResult)
        XCTAssertNil(store.currentStage)
    }

    func testFsckSelectionHasOneOwnerAndInvalidatesThePlanImmediately() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "fsck", ok: true,
                payload: testFsckListPayload(targets: [
                    testFsckTargetPayload(name: "Data"),
                    testFsckTargetPayload(name: "Backup", device: "/dev/dk3", mountpoint: "/Volumes/dk3")
                ]))]),
            .init(events: [BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.refreshFsckTargets(password: "pw")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isBusy }
        XCTAssertNil(store.selectedFsckTargetID)

        store.selectedFsckTargetID = store.fsckTargets[0].id
        XCTAssertEqual(store.fsckStore.selectedTarget?.name, "Data")
        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isBusy }

        var changes = 0
        let observation = store.objectWillChange.sink { changes += 1 }
        defer { observation.cancel() }
        store.fsckStore.selectTarget(
            id: store.fsckTargets[1].id,
            options: MaintenanceOptions(noReboot: false, noWait: false, mountWait: 30)
        )
        XCTAssertEqual(store.selectedFsckTarget?.name, "Backup")
        XCTAssertEqual(store.selectedFsckTargetID, store.fsckTargets[1].id)
        XCTAssertEqual(store.fsckState, .planStale)
        XCTAssertFalse(store.canRunFsck)
        XCTAssertGreaterThan(changes, 0)

        store.fsckStore.clear()
        XCTAssertNil(store.selectedFsckTargetID)
        XCTAssertTrue(store.fsckTargets.isEmpty)
        XCTAssertNil(store.fsckPlan)
        XCTAssertEqual(store.fsckState, .idle)
    }

    func testEachRemoteChildInvalidatesCredentialsAndRefreshesSSHDespiteAnotherSelectedError() async throws {
        for operation in ["activate", "uninstall", "fsck", "set-ssh"] {
            let fixture = try await makeFixture(responses: [
                .init(events: [.error(operation: operation, code: "auth_failed", message: "Password rejected.")]),
                .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())])
            ])
            let store = fixture.session.maintenanceStore
            store.activationStore.rejectAlreadyRunning()
            XCTAssertEqual(store.selectedWorkflow, .activate)
            switch operation {
            case "activate":
                store.activationStore.runActivation(password: "pw", profile: fixture.profile)
            case "uninstall":
                store.uninstallStore.runUninstall(
                    options: MaintenanceOptions(noReboot: false, noWait: false, mountWait: 30),
                    password: "pw", profile: fixture.profile
                )
            case "fsck":
                store.fsckStore.refreshTargets(mountWaitValue: 30, password: "pw", profile: fixture.profile)
            default:
                store.sshAccessStore.enable(password: "pw", noWait: false, profile: fixture.profile)
            }

            try await waitUntilStoreState {
                fixture.app.deviceRegistry.profile(id: fixture.profile.id)?.passwordState == .invalid &&
                    fixture.app.sshAccessStore.snapshot(for: fixture.profile) != nil &&
                    !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile)
            }
            XCTAssertEqual(fixture.runner.calls.map(\.operation), [operation, "set-ssh"])
            XCTAssertEqual(fixture.runner.calls.last?.params["action"], .string("status"))
        }
    }

    func testOtherChildChangesDoNotRepublishAnOldSSHObservation() async throws {
        var now = Date(timeIntervalSince1970: 100)
        let fixture = try await makeFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())]),
            .init(events: [BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))])
        ], now: { now })
        let store = fixture.session.maintenanceStore
        store.checkSSHAccess(profile: fixture.profile)
        try await waitUntilStoreState {
            fixture.app.sshAccessStore.snapshot(for: fixture.profile) != nil && !store.isBusy
        }
        XCTAssertEqual(fixture.app.sshAccessStore.snapshot(for: fixture.profile)?.refreshedAt, now)

        now = Date(timeIntervalSince1970: 200)
        store.runActivation(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { store.activateState == .succeeded && !store.isBusy }
        XCTAssertEqual(
            fixture.app.sshAccessStore.snapshot(for: fixture.profile)?.refreshedAt,
            Date(timeIntervalSince1970: 100)
        )
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["set-ssh", "activate"])
    }

    private func makeFixture(
        responses: [StoreTestRunner.Response],
        now: @escaping () -> Date = Date.init
    ) async throws -> (app: AppStore, profile: DeviceProfile, session: DeviceDashboardSession, runner: StoreTestRunner) {
        let directory = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: directory.url)
        await registry.load()
        let profile = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(), discoveredDevice: nil,
            passwordState: .available, preferredID: "device-one"
        )
        let runner = StoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let app = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: registry, operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore(),
            sshAccessStore: DeviceSSHAccessStore(coordinator: coordinator, now: now)
        )
        return (app, profile, DeviceDashboardSession(profile: profile, appStore: app), runner)
    }
}
