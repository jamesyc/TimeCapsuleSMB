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
        for operation in ["activate", "uninstall", "fsck", "wipe-disk", "set-ssh", "flash"] {
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
            case "wipe-disk":
                store.wipeDiskStore.runWipe(password: "pw", profile: fixture.profile)
            case "flash":
                store.bootHookStore.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
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

    func testBootHookChangesReachTheMaintenanceStoreAndTheDashboard() async throws {
        let fixture = try await makeFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.patchNeedsPowerCycle)
        ))
        let maintenance = fixture.session.maintenanceStore
        let bootHook = maintenance.bootHookStore
        var maintenanceChanges = 0
        var sessionChanges = 0
        let observations = [
            maintenance.objectWillChange.sink { maintenanceChanges += 1 },
            fixture.session.objectWillChange.sink { sessionChanges += 1 }
        ]
        defer { observations.forEach { $0.cancel() } }

        // Straight to the boot hook's store: a start through the maintenance
        // store would announce a change of its own when it sets the selection.
        bootHook.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { bootHook.backup != nil && !bootHook.isBusy }
        XCTAssertGreaterThan(maintenanceChanges, 0)
        XCTAssertGreaterThan(sessionChanges, 0)

        bootHook.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { bootHook.plan != nil && !bootHook.isBusy }
        maintenanceChanges = 0
        sessionChanges = 0
        bootHook.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { bootHook.showsManualPowerCycleNotice && !bootHook.isBusy }
        XCTAssertGreaterThan(maintenanceChanges, 0)
        XCTAssertGreaterThan(sessionChanges, 0)

        // The dashboard's power-cycle alert closes when the notice goes.
        sessionChanges = 0
        bootHook.dismissManualPowerCycleNotice()
        XCTAssertEqual(sessionChanges, 1)
    }

    func testOnlyABootHookFailureOnTheDeviceRechecksSSH() async throws {
        let fixture = try await makeFixture(responses: testBootHookResponses(failure: [
            testBootHookError("refusing to patch primary because the secondary (backup) firmware bank is not a valid backup")
        ]) + [
            .init(events: [.error(operation: "flash", code: "auth_failed", message: "Password rejected.")]),
            .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())])
        ])
        let bootHook = fixture.session.maintenanceStore.bootHookStore

        bootHook.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { bootHook.backup != nil && !bootHook.isBusy }
        // A plan reads only the saved backup, so its refusal leaves SSH alone.
        bootHook.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState {
            bootHook.error != nil && !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile)
        }

        // A backup that fails on the device does recheck it. A wrong recheck
        // after the plan shows here as a set-ssh call before this backup, when
        // the session's queued task runs before the test goes on, as it does
        // on the main actor; had the backup started first, this failure would
        // replace the pending recheck and hide it.
        bootHook.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState {
            fixture.app.sshAccessStore.snapshot(for: fixture.profile) != nil
                && !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile)
        }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["flash", "flash", "flash", "set-ssh"])
    }

    func testAWriteThatReachedTheDeviceButFailedLeavesSSHAlone() async throws {
        // The helper reached the device over SSH.
        let failures: [(name: String, event: BackendEvent)] = [
            ("an unreadable confirmation", BackendEvent(type: "error", operation: "flash", code: "confirmation_required", message: "Confirm?")),
        ]
        for (name, failure) in failures {
            let fixture = try await makeFixture(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .patch), failure: [failure]
            ) + testBootHookResponses() + [
                .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())])
            ])
            let bootHook = fixture.session.maintenanceStore.bootHookStore
            let idle = { !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile) }
            bootHook.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { bootHook.backup != nil && idle() }
            bootHook.planFirmware(mode: .patch, profile: fixture.profile)
            try await waitUntilStoreState { bootHook.plan != nil && idle() }
            bootHook.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { bootHook.error != nil && idle() }

            // A wrong recheck queued by the failure runs before the next
            // backup, on the main actor, and shows as a set-ssh call.
            await Task.yield()
            bootHook.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { bootHook.backup != nil && idle() }
            await Task.yield()
            XCTAssertEqual(fixture.runner.calls.map(\.operation), ["flash", "flash", "flash", "flash"], name)
        }
    }

    func testARefusalTheAppMadeNeverRechecksSSH() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [.error(operation: "activate", code: "cancelled", message: "Cancelled.")]),
            .init(events: [BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallResultPayload(waited: true, verified: true))]),
            .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())])
        ])
        let store = fixture.session.maintenanceStore
        // A cancelled run leaves its error event unhandled, since a cancel
        // says nothing about SSH.
        store.runActivation(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState {
            store.error(for: .activate)?.code == "cancelled" && !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile)
        }

        store.activationStore.rejectAlreadyRunning()
        XCTAssertEqual(store.error(for: .activate)?.localError, .operationAlreadyRunning)
        // The session queues its recheck as a main-actor task; yielding lets
        // a wrong one start before the next run. That run is another
        // workflow's: a successful retry of activation would cancel the recheck.
        await Task.yield()
        store.runUninstall(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState {
            store.uninstallState == .succeeded && !fixture.app.operationCoordinator.isDeviceBusy(fixture.profile)
        }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["activate", "uninstall"])
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
