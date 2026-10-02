import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class OperationCompletionTests: XCTestCase {
    func testFailedWorkflowDefersSSHRefreshUntilDeviceRelease() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [.init(events: [failure("deploy")], pauseAfterEvents: true)],
            .init("set-ssh"): [.init(events: [sshResult()])]
        ])
        defer { fixture.runner.finishAll() }
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runInstall(profile: fixture.profile)
        try await waitUntilStoreState { session.deployStore.error != nil }
        XCTAssertTrue(fixture.coordinator.isDeviceBusy(fixture.profile))
        fixture.runner.finish(.init("deploy"))
        try await waitUntilStoreState {
            fixture.app.sshAccessStore.snapshot(for: fixture.profile) != nil
                && !fixture.coordinator.isDeviceBusy(fixture.profile)
        }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "set-ssh"])
        XCTAssertNil(fixture.app.sshAccessStore.error(for: fixture.profile))
    }

    func testDeferredRefreshSurvivesAnotherWorkflowAndIsSupersededBySuccessfulRetry() async throws {
        for supersedingOperation in [nil, "deploy", "set-ssh"] as [String?] {
            let fixture = try await makeFixture(responses: [
                .init("deploy"): [
                    .init(events: [failure("deploy"), failure("deploy")], pauseAfterEvents: true),
                    .init(events: [BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())])
                ],
                .init("reachability"): [.init(events: [BackendEvent(type: "result", operation: "reachability", ok: true)], pauseAfterEvents: true)],
                .init("set-ssh"): [.init(events: [sshResult()])]
            ])
            defer { fixture.runner.finishAll() }
            let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            session.runInstall(profile: fixture.profile)
            try await waitUntilStoreState { session.deployStore.error != nil }
            let deploy = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .deploy)).backend
            var blockerStarted = false
            let blocker = deploy.$activeOperationName.sink { name in
                // This final cleanup publication is after isRunning=false. Start
                // another lane synchronously before queued diagnostic work drains.
                if name == nil, !deploy.isRunning, !blockerStarted {
                    blockerStarted = true
                    XCTAssertNotNil(fixture.coordinator.run(operation: "reachability", profile: fixture.profile).operation)
                }
            }
            fixture.runner.finish(.init("deploy"))
            try await waitUntilStoreState { fixture.runner.calls.count == 2 }
            XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "reachability"])
            XCTAssertNil(fixture.app.sshAccessStore.error(for: fixture.profile))
            let reachability = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .reachability)).backend
            var retryStarted = false
            let retry = reachability.$activeOperationName.sink { name in
                if name == nil, !reachability.isRunning, !retryStarted, let supersedingOperation {
                    retryStarted = true
                    XCTAssertNotNil(fixture.coordinator.run(operation: supersedingOperation, profile: fixture.profile).operation)
                }
            }
            fixture.runner.finish(.init("reachability"))
            try await waitUntilStoreState { fixture.runner.calls.count == 3 && !fixture.coordinator.isDeviceBusy(fixture.profile) }
            XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "reachability", supersedingOperation ?? "set-ssh"])
            XCTAssertNil(fixture.app.sshAccessStore.error(for: fixture.profile))
            withExtendedLifetime((session, blocker, retry)) {}
        }
    }

    func testDeferredRefreshIsDiscardedAfterProfileDeletionOrSessionDisposal() async throws {
        for deleteProfile in [false, true] {
            let fixture = try await makeFixture(responses: [
                .init("deploy"): [.init(events: [failure("deploy")], pauseAfterEvents: true)]
            ])
            defer { fixture.runner.finishAll() }
            var session: DeviceDashboardSession? = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            weak var weakSession = session
            session?.runInstall(profile: fixture.profile)
            try await waitUntilStoreState { session?.deployStore.error != nil }
            if deleteProfile {
                try await fixture.app.deviceRegistry.delete(fixture.profile)
            } else {
                session = nil
                try await waitUntilStoreState { weakSession == nil }
            }
            fixture.runner.finish(.init("deploy"))
            try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
            XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy"])
            XCTAssertNil(fixture.app.sshAccessStore.snapshot(for: fixture.profile))
            withExtendedLifetime(session) {}
        }
    }

    func testMaintenanceSSHResultsSupersedeAutomaticHistory() async throws {
        for maintenanceFails in [false, true] {
            let fixture = try await makeFixture(responses: [
                .init("set-ssh"): [
                    .init(events: [sshResult()]),
                    .init(events: [maintenanceFails ? failure("set-ssh") : sshResult()], pauseAfterEvents: true),
                    .init(events: [sshResult()])
                ],
                .init("deploy"): [.init(events: [failure("deploy")], pauseAfterEvents: true)]
            ])
            defer { fixture.runner.finishAll() }
            let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            fixture.app.sshAccessStore.refresh(profile: fixture.profile)
            try await waitUntilStoreState {
                fixture.app.sshAccessStore.snapshot(for: fixture.profile) != nil
                    && !fixture.coordinator.isDeviceBusy(fixture.profile)
            }
            var subscription: AnyCancellable?
            if maintenanceFails {
                session.maintenanceStore.checkSSHAccess(profile: fixture.profile)
                try await waitUntilStoreState { session.maintenanceStore.error != nil }
            } else {
                session.runInstall(profile: fixture.profile)
                try await waitUntilStoreState { session.deployStore.error != nil }
                let backend = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .deploy)).backend
                var started = false
                subscription = backend.$activeOperationName.sink { name in
                    if name == nil, !backend.isRunning, !started {
                        started = true
                        session.maintenanceStore.checkSSHAccess(profile: fixture.profile)
                    }
                }
                fixture.runner.finish(.init("deploy"))
                try await waitUntilStoreState { session.maintenanceStore.sshAccessPayload != nil }
            }
            fixture.runner.finish(.init("set-ssh"))
            if maintenanceFails {
                try await waitUntilStoreState { fixture.runner.calls.count == 3 }
            }
            try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
            XCTAssertEqual(fixture.runner.calls.map(\.operation), maintenanceFails
                           ? ["set-ssh", "set-ssh", "set-ssh"] : ["set-ssh", "deploy", "set-ssh"])
            XCTAssertNil(fixture.app.sshAccessStore.error(for: fixture.profile))
            withExtendedLifetime((session, subscription)) {}
        }
    }

    func testAddDeviceFailureKeepsOwnershipUntilHelperStops() async throws {
        let fixture = try await makeFixture(responses: [
            .init("configure"): [
                .init(events: [failure("configure")], pauseAfterEvents: true),
                .init(events: [BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload())])
            ]
        ])
        defer { fixture.runner.finishAll() }
        let store = DeviceSetupWorkflow(coordinator: fixture.coordinator, profilePersistence: fixture.app.profilePersistence)
        func start() {
            store.start(target: .manual(ManualDeviceTarget(host: fixture.profile.host)), password: "pw", existingProfile: fixture.profile,
                        settings: .default, newProfileSettings: .default)
        }
        start()
        try await waitUntilStoreState { store.error != nil }
        XCTAssertTrue(store.isRunning, "A visible failure must not release the configure operation")
        fixture.runner.finish(.init("configure"))
        try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
        XCTAssertFalse(store.isRunning)
        start()
        try await waitUntilStoreState { store.state == .saved && !store.isRunning }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["configure", "configure"])
        XCTAssertEqual(store.savedProfile?.id, fixture.profile.id)
    }

    func testProfileFailureKeepsRetryDisabledUntilHelperStops() async throws {
        let fixture = try await makeFixture(responses: [
            .init("update-config-settings"): [
                .init(events: [failure("update-config-settings")], pauseAfterEvents: true),
                .init(events: [BackendEvent(type: "result", operation: "update-config-settings", ok: true)])
            ]
        ])
        defer { fixture.runner.finishAll() }
        let store = DeviceProfileEditorStore(profile: fixture.profile, appStore: fixture.app)
        store.draft.displayName = "Changed"
        await store.save(profile: fixture.profile)
        try await waitUntilStoreState { store.state == .failed }
        XCTAssertFalse(store.canSave)
        XCTAssertTrue(store.isRunning)
        let published = expectation(description: "Retry availability is published after cleanup")
        var fulfilled = false
        let subscription = store.objectWillChange.sink { [weak store] in
            Task { @MainActor in
                if !fulfilled, store?.canSave == true {
                    fulfilled = true
                    published.fulfill()
                }
            }
        }
        fixture.runner.finish(.init("update-config-settings"))
        await fulfillment(of: [published], timeout: 2)
        try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
        await store.save(profile: fixture.profile)
        try await waitUntilStoreState { store.state == .saved && !store.isRunning }
        XCTAssertEqual(store.savedProfile?.displayName, "Changed")
        XCTAssertEqual(fixture.runner.calls.count, 2)
        withExtendedLifetime(subscription) {}
    }

    func testConfirmationWaitsForExitAndKeepsDeviceReserved() async throws {
        for accept in [false, true] {
            let fixture = try await makeFixture(responses: [
                .init("deploy"): [
                    .init(events: [confirmation("deploy", id: "first")], pauseAfterEvents: true),
                    .init(events: [BackendEvent(type: "result", operation: "deploy", ok: true)])
                ]
            ])
            defer { fixture.runner.finishAll() }
            let start = fixture.coordinator.run(operation: "deploy", profile: fixture.profile)
            XCTAssertNotNil(start.operation)
            let lane = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .deploy))
            try await waitUntilStoreState { lane.backend.pendingConfirmation != nil }
            XCTAssertNil(fixture.coordinator.readyConfirmation)
            XCTAssertTrue(fixture.coordinator.isDeviceBusy(fixture.profile))
            let pending = try XCTUnwrap(lane.backend.pendingConfirmation)
            fixture.coordinator.confirm(pending)
            fixture.coordinator.cancel(pending)
            XCTAssertEqual(lane.backend.pendingConfirmation?.id, pending.id)
            XCTAssertEqual(fixture.runner.calls.count, 1)
            fixture.runner.finish(.init("deploy"))
            try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
            XCTAssertTrue(fixture.coordinator.isDeviceBusy(fixture.profile))
            if accept { fixture.coordinator.confirm(pending) } else { fixture.coordinator.cancel(pending) }
            try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
            XCTAssertNil(fixture.coordinator.readyConfirmation)
            XCTAssertEqual(fixture.runner.calls.count, accept ? 2 : 1)
            if accept {
                XCTAssertEqual(fixture.runner.calls.last?.params["confirmation_id"], .string("first"))
                XCTAssertEqual(fixture.runner.calls.last?.context?.profileID, fixture.profile.id)
            }
        }
    }

    func testConfirmationStaysBoundToDisplayedRequestAcrossDevicesAndDismissal() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy", profileID: "device-one"): [.init(events: [confirmation("deploy", id: "one")], pauseAfterEvents: true)],
            .init("deploy", profileID: "device-two"): [
                .init(events: [confirmation("deploy", id: "two")]),
                .init(events: [confirmation("deploy", id: "two-next")])
            ]
        ])
        defer { fixture.runner.finishAll() }
        XCTAssertNotNil(fixture.coordinator.run(operation: "deploy", profile: fixture.profile).operation)
        let secondContext = DeviceRuntimeContext(profileID: "device-two", configURL: fixture.profile.configURL)
        XCTAssertNotNil(fixture.coordinator.run(operation: "deploy", context: secondContext, activeDeviceID: "device-two",
                                               laneKey: .deviceWorkflow("device-two", .deploy)).operation)
        try await waitUntilStoreState { fixture.coordinator.readyConfirmation?.context?.profileID == "device-two" }
        let displayed = try XCTUnwrap(fixture.coordinator.readyConfirmation)
        fixture.runner.finish(.init("deploy", profileID: "device-one"))
        try await waitUntilStoreState { !fixture.coordinator.lane(for: .deviceWorkflow("device-one", .deploy)).backend.isRunning }
        XCTAssertEqual(fixture.coordinator.readyConfirmation?.id, displayed.id)
        fixture.coordinator.confirm(displayed)
        try await waitUntilStoreState { fixture.runner.calls.count == 3 && !fixture.coordinator.lane(for: .deviceWorkflow("device-two", .deploy)).backend.isRunning }
        XCTAssertEqual(fixture.runner.calls.last?.context?.profileID, "device-two")
        XCTAssertEqual(fixture.runner.calls.last?.params["confirmation_id"], .string("two"))
        // SwiftUI dismisses the old alert after its button action. It must not
        // cancel either device's newly selected/current confirmation.
        fixture.coordinator.cancel(displayed)
        let first = try XCTUnwrap(fixture.coordinator.readyConfirmation)
        XCTAssertEqual(first.context?.profileID, "device-one")
        fixture.coordinator.cancel(first)
        let second = try XCTUnwrap(fixture.coordinator.readyConfirmation)
        XCTAssertEqual(second.params["confirmation_id"], .string("two-next"))
        fixture.coordinator.cancel(displayed)
        XCTAssertEqual(fixture.coordinator.readyConfirmation?.id, second.id)
        fixture.coordinator.cancel(second)
        XCTAssertFalse(fixture.coordinator.hasActiveWork)
    }

    func testPersistenceAndHelperMustBothFinishBeforeRetry() async throws {
        for setup in [false, true] {
            for helperFirst in [false, true] {
                let files = BlockingRegistryFileManager()
                let operation = setup ? "configure" : "update-config-settings"
                let fixture = try await makeFixture(responses: [
                    .init(operation): [.init(events: [BackendEvent(type: "result", operation: operation, ok: true,
                                                                   payload: setup ? testConfigurePayload() : nil)], pauseAfterEvents: true)]
                ], fileManager: files)
                defer { files.release(); fixture.runner.finishAll() }
                let workflow = DeviceSetupWorkflow(coordinator: fixture.coordinator, profilePersistence: fixture.app.profilePersistence)
                let editor = DeviceProfileEditorStore(profile: fixture.profile, appStore: fixture.app)
                files.arm(at: fixture.app.deviceRegistry.applicationSupportURL)
                if setup {
                    workflow.start(target: .manual(ManualDeviceTarget(host: fixture.profile.host)), password: "pw", existingProfile: fixture.profile,
                                   settings: .default, newProfileSettings: .default)
                } else {
                    editor.draft.displayName = "Saved name"
                    await editor.save(profile: fixture.profile)
                }
                try await waitUntilStoreState { files.isBlocked }
                XCTAssertTrue(setup ? workflow.isRunning : editor.isRunning)
                if helperFirst {
                    fixture.runner.finish(.init(operation))
                    try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(fixture.profile) }
                    XCTAssertTrue(setup ? workflow.isRunning : editor.isRunning)
                    files.release()
                } else {
                    files.release()
                    try await waitUntilStoreState { setup ? workflow.state == .saved : editor.state == .saved }
                    XCTAssertTrue(setup ? workflow.isRunning : editor.isRunning)
                    fixture.runner.finish(.init(operation))
                }
                try await waitUntilStoreState { setup ? !workflow.isRunning : !editor.isRunning }
                XCTAssertEqual(setup ? workflow.savedProfile?.id : editor.savedProfile?.id, fixture.profile.id)
                if !setup {
                    editor.draft.displayName = "Next edit"
                    XCTAssertTrue(editor.canSave)
                }
            }
        }
    }

    func testResetIgnoresOldPersistenceCompletion() async throws {
        for setup in [false, true] {
            let files = BlockingRegistryFileManager()
            let operation = setup ? "configure" : "update-config-settings"
            let fixture = try await makeFixture(responses: [
                .init(operation): [.init(events: [BackendEvent(type: "result", operation: operation, ok: true,
                                                               payload: setup ? testConfigurePayload() : nil)])]
            ], fileManager: files)
            defer { files.release(); fixture.runner.finishAll() }
            let workflow = DeviceSetupWorkflow(coordinator: fixture.coordinator, profilePersistence: fixture.app.profilePersistence)
            let editor = DeviceProfileEditorStore(profile: fixture.profile, appStore: fixture.app)
            files.arm(at: fixture.app.deviceRegistry.applicationSupportURL)
            if setup {
                workflow.start(target: .manual(ManualDeviceTarget(host: fixture.profile.host)), password: "pw", existingProfile: fixture.profile,
                               settings: .default, newProfileSettings: .default)
            } else {
                editor.draft.displayName = "Old edit"
                await editor.save(profile: fixture.profile)
            }
            try await waitUntilStoreState { files.isBlocked }
            if setup { workflow.reset() } else { editor.reset(to: fixture.profile) }
            XCTAssertTrue(setup ? workflow.isRunning : editor.isRunning)
            files.release()
            try await waitUntilStoreState { setup ? !workflow.isRunning : !editor.isRunning }
            if setup {
                XCTAssertEqual(workflow.state, .idle)
                XCTAssertNil(workflow.savedProfile)
            } else {
                XCTAssertEqual(editor.state, .clean)
                XCTAssertNil(editor.savedProfile)
                XCTAssertEqual(editor.draft.displayName, fixture.profile.displayName)
            }
        }
    }

    private func confirmation(_ operation: String, id: String) -> BackendEvent {
        BackendEvent(type: "error", operation: operation, code: "confirmation_required", message: "Confirm",
                     details: .object(["confirmation_id": .string(id)]))
    }

    @MainActor
    private struct Fixture {
        let app: AppStore
        let profile: DeviceProfile
        let runner: OperationKeyedStoreTestRunner
        var coordinator: OperationCoordinator { app.operationCoordinator }
    }

    private func makeFixture(responses: [OperationKeyedStoreTestRunner.Key: [StoreTestRunner.Response]], fileManager: FileManager = .default) async throws -> Fixture {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, fileManager: fileManager)
        await registry.load()
        let profile = try await registry.saveConfiguredDevice(configuredDevice: testConfiguredDevice(), discoveredDevice: nil,
                                                              passwordState: .available, preferredID: "device-one")
        let passwords = InMemoryPasswordStore()
        try passwords.save("pw", for: profile.keychainAccount)
        let runner = OperationKeyedStoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let app = AppStore(appReadinessStore: AppReadinessStore(backend: coordinator.backend), deviceRegistry: registry,
                           operationCoordinator: coordinator, passwordStore: passwords)
        return Fixture(app: app, profile: profile, runner: runner)
    }

    private func failure(_ operation: String) -> BackendEvent {
        BackendEvent(type: "error", operation: operation, code: "remote_error", message: "Controlled failure")
    }

    private func sshResult() -> BackendEvent {
        BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
    }
}

// Only the registry actor's final persist call is blocked; the main actor and
// helper remain free to finish in either order. No production test hooks needed.
private final class BlockingRegistryFileManager: FileManager, @unchecked Sendable {
    private let condition = NSCondition()
    private var blocked = false
    private var target: URL?

    var isBlocked: Bool {
        condition.lock()
        defer { condition.unlock() }
        return blocked
    }

    func arm(at url: URL) {
        condition.lock()
        target = url
        condition.unlock()
    }

    func release() {
        condition.lock()
        target = nil
        condition.broadcast()
        condition.unlock()
    }

    override func createDirectory(at url: URL, withIntermediateDirectories createIntermediates: Bool,
                                  attributes: [FileAttributeKey: Any]? = nil) throws {
        condition.lock()
        if target == url {
            blocked = true
            while target != nil { condition.wait() }
        }
        condition.unlock()
        try super.createDirectory(at: url, withIntermediateDirectories: createIntermediates, attributes: attributes)
    }
}
