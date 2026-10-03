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

    func testCancelledInstallFinishesDashboardAndSurvivesReload() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [.init(events: [confirmation("deploy", id: "cancel")])]
        ])
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runInstall(profile: fixture.profile)
        try await waitUntilStoreState {
            fixture.coordinator.readyConfirmation != nil &&
                fixture.app.deviceRegistry.profile(id: fixture.profile.id)?.lastDeployState?.status == .awaitingConfirmation
        }
        let pending = try XCTUnwrap(fixture.coordinator.readyConfirmation)
        fixture.coordinator.cancel(pending)
        fixture.coordinator.cancel(pending) // The alert binding also dismisses after Cancel.

        try await waitUntilStoreState {
            fixture.app.deviceRegistry.profile(id: fixture.profile.id)?.lastDeployState?.status == .failed
        }
        XCTAssertEqual(session.deployStore.state, .deployFailed)
        XCTAssertEqual(session.deployStore.error?.code, "confirmation_cancelled")
        XCTAssertNil(session.deployStore.currentStage)
        XCTAssertNil(DeployFailureGuidancePolicy.guidance(for: session.deployStore.error))
        XCTAssertFalse(fixture.coordinator.isDeviceBusy(fixture.profile))
        XCTAssertTrue(session.deployStore.canDeploy)
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy"])

        let saved = try XCTUnwrap(fixture.app.deviceRegistry.profile(id: fixture.profile.id))
        XCTAssertEqual(saved.runtimeState?.state, .installFailed)
        XCTAssertEqual(saved.lastDeployState?.errorCode, "confirmation_cancelled")
        XCTAssertNotNil(saved.lastDeployState?.finishedAt)
        assertDashboardStopped(saved, app: fixture.app)
        let reloaded = DeviceRegistryStore(applicationSupportURL: fixture.app.deviceRegistry.applicationSupportURL)
        await reloaded.load()
        let restored = try XCTUnwrap(reloaded.profile(id: saved.id))
        XCTAssertEqual(restored.lastDeployState?.status, .failed)
        XCTAssertEqual(restored.runtimeState?.state, .installFailed)
        assertDashboardStopped(restored, app: fixture.app)
    }

    func testCancelledInstallCannotOverwriteImmediateRetryOrCheckupWithBlockedPersistence() async throws {
        for nextOperation in ["deploy", "doctor"] {
            let files = BlockingRegistryFileManager()
            var responses: [OperationKeyedStoreTestRunner.Key: [StoreTestRunner.Response]] = [
                .init("deploy"): [.init(events: [confirmation("deploy", id: "cancel")])]
            ]
            let result = BackendEvent(type: "result", operation: nextOperation, ok: true,
                                      payload: nextOperation == "deploy" ? testDeployResultPayload() : testDoctorPayload(checks: [
                                        testDoctorCheck(status: "PASS", message: "Running", domain: "Runtime")
                                      ]))
            responses[.init(nextOperation), default: []].append(.init(events: [result]))
            let fixture = try await makeFixture(responses: responses, fileManager: files)
            defer { files.release(); fixture.runner.finishAll() }
            let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            files.arm(at: fixture.app.deviceRegistry.applicationSupportURL)
            session.runInstall(profile: fixture.profile)
            try await waitUntilStoreState { files.isBlocked && fixture.coordinator.readyConfirmation != nil }
            let oldConfirmation = try XCTUnwrap(fixture.coordinator.readyConfirmation)
            fixture.coordinator.cancel(oldConfirmation)
            // No yield: a new start must not erase the old cancellation event.
            let reopened = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            if nextOperation == "deploy" { reopened.runInstall(profile: fixture.profile) }
            else { reopened.runCheckup(profile: fixture.profile) }
            fixture.coordinator.cancel(oldConfirmation)
            try await waitUntilStoreState { fixture.runner.calls.count == 2 && !fixture.coordinator.isDeviceBusy(fixture.profile) }
            files.release()
            try await waitUntilStoreState {
                fixture.app.deviceRegistry.profile(id: fixture.profile.id)?.runtimeState?.state == .installedVerified
            }
            let saved = try XCTUnwrap(fixture.app.deviceRegistry.profile(id: fixture.profile.id))
            XCTAssertEqual(saved.lastDeployState?.status, nextOperation == "deploy" ? .succeeded : .failed)
            XCTAssertEqual(saved.lastCheckup?.state, nextOperation == "doctor" ? .passed : nil)
            assertDashboardStopped(saved, app: fixture.app)
        }
    }

    func testDelayedCheckupCannotOverwriteNewInstall() async throws {
        let files = BlockingRegistryFileManager()
        let fixture = try await makeFixture(responses: [
            .init("doctor"): [.init(events: [BackendEvent(type: "result", operation: "doctor", ok: true,
                payload: testDoctorPayload(checks: [testDoctorCheck(status: "PASS", message: "Running", domain: "Runtime")]))])],
            .init("deploy"): [.init(events: [confirmation("deploy", id: "new-install")])]
        ], fileManager: files)
        defer { files.release(); fixture.runner.finishAll() }
        let registry = fixture.app.deviceRegistry
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        files.arm(at: registry.applicationSupportURL)
        session.runCheckup(profile: fixture.profile)
        try await waitUntilStoreState { files.isBlocked && !fixture.coordinator.isDeviceBusy(fixture.profile) }
        let reopened = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        reopened.runInstall(profile: fixture.profile)
        try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
        files.release()
        var drained = false
        registry.enqueueOperationUpdate { drained = true }
        try await waitUntilStoreState { drained }
        let saved = try XCTUnwrap(registry.profile(id: fixture.profile.id))
        XCTAssertEqual(saved.runtimeState?.state, .installing)
        XCTAssertEqual(saved.lastDeployState?.status, .awaitingConfirmation)
        XCTAssertNil(saved.lastCheckup)
        XCTAssertTrue(fixture.coordinator.isDeviceBusy(saved))
        fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
        try await waitUntilStoreState { registry.profile(id: saved.id)?.lastDeployState?.status == .failed }
        assertDashboardStopped(try XCTUnwrap(registry.profile(id: saved.id)), app: fixture.app)
    }

    func testDelayedUninstallCannotEraseAnImmediateInstallFromAnotherSession() async throws {
        let files = BlockingRegistryFileManager()
        let fixture = try await makeFixture(responses: [
            .init("uninstall"): [.init(events: [BackendEvent(type: "result", operation: "uninstall", ok: true,
                payload: testUninstallResultPayload(waited: true, verified: true))], pauseAfterEvents: true)],
            .init("deploy"): [.init(events: [BackendEvent(type: "result", operation: "deploy", ok: true,
                payload: testDeployResultPayload())])]
        ], fileManager: files)
        defer { files.release(); fixture.runner.finishAll() }
        let registry = fixture.app.deviceRegistry
        await registry.updateInstallOperationState(deployState: testDeployState(status: .succeeded),
            runtimeState: testRuntimeState(state: .installedVerified), for: fixture.profile.id)
        let installed = try XCTUnwrap(registry.profile(id: fixture.profile.id))
        let session = DeviceDashboardSession(profile: installed, appStore: fixture.app)
        // With no Checkup to invalidate, the first write is the completed
        // uninstall clearing the saved installation. Hold it until Install ends.
        files.arm(at: registry.applicationSupportURL)
        session.performMaintenanceAction(.runUninstall, profile: installed, showDiagnostics: {})
        try await waitUntilStoreState { files.isBlocked && session.maintenanceStore.uninstallState == .succeeded }
        XCTAssertTrue(fixture.coordinator.isDeviceBusy(installed))
        fixture.runner.finish(.init("uninstall"))
        try await waitUntilStoreState { !fixture.coordinator.isDeviceBusy(installed) }

        let reopened = DeviceDashboardSession(profile: installed, appStore: fixture.app)
        reopened.runInstall(profile: installed)
        let operation = try XCTUnwrap(fixture.coordinator.activeOperation(for: .deviceWorkflow(installed.id, .deploy)))
        try await waitUntilStoreState { reopened.deployStore.state == .deployed && !fixture.coordinator.isDeviceBusy(installed) }
        files.release()
        try await drainOperationUpdates(registry)

        let reloaded = DeviceRegistryStore(applicationSupportURL: registry.applicationSupportURL)
        await reloaded.load()
        for saved in [try XCTUnwrap(registry.profile(id: installed.id)), try XCTUnwrap(reloaded.profile(id: installed.id))] {
            XCTAssertEqual(saved.lastDeployState?.operationID, operation.id.uuidString)
            XCTAssertEqual(saved.lastDeployState?.status, .succeeded)
            XCTAssertEqual(saved.runtimeState?.state, .installedVerified)
            assertDashboardStopped(saved, app: fixture.app)
        }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["uninstall", "deploy"])
    }

    func testMaintenanceCancellationWaitsForHelperAndPreservesInstallation() async throws {
        let cases: [(String, MaintenanceWorkflow, MaintenanceUserAction)] = [
            ("activate", .activate, .runActivation),
            ("uninstall", .uninstall, .runUninstall),
            ("set-ssh", .sshAccess, .enableSSHAccess)
        ]
        for (operation, workflow, action) in cases {
            let fixture = try await makeFixture(responses: [
                .init(operation): [.init(events: [confirmation(operation, id: "cancel")], pauseAfterEvents: true)]
            ])
            defer { fixture.runner.finishAll() }
            let registry = fixture.app.deviceRegistry
            let installed = testRuntimeState(state: .installedVerified)
            let previousDeploy = testDeployState(status: .succeeded)
            await registry.updateInstallOperationState(deployState: previousDeploy, runtimeState: installed, for: fixture.profile.id)
            let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            session.performMaintenanceAction(action, profile: fixture.profile, showDiagnostics: {})
            try await waitUntilStoreState { session.maintenanceStore.pendingConfirmation(for: workflow) != nil }
            session.maintenanceStore.cancelPendingConfirmation(for: workflow)
            XCTAssertNotNil(session.maintenanceStore.pendingConfirmation(for: workflow))
            XCTAssertTrue(fixture.coordinator.isDeviceBusy(fixture.profile))
            if workflow == .uninstall { XCTAssertEqual(session.maintenanceStore.uninstallStore.state, .awaitingConfirmation) }

            fixture.runner.finish(.init(operation))
            try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
            let pending = try XCTUnwrap(fixture.coordinator.readyConfirmation)
            fixture.coordinator.cancel(pending)
            fixture.coordinator.cancel(pending)
            try await waitUntilStoreState { !session.maintenanceStore.isBusy }
            let saved = try XCTUnwrap(registry.profile(id: fixture.profile.id))
            XCTAssertEqual(saved.runtimeState, installed)
            XCTAssertEqual(saved.lastDeployState, previousDeploy)
            XCTAssertFalse(fixture.coordinator.isDeviceBusy(saved))
            assertDashboardStopped(saved, app: fixture.app)
            XCTAssertEqual(fixture.runner.calls.count, 1)
        }
    }

    func testRunningCancellationKeepsDashboardBusyUntilHelperCleanup() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [.init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "check_compatibility", cancellable: true)
            ], pauseAfterEvents: true)]
        ])
        defer { fixture.runner.finishAll() }
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runInstall(profile: fixture.profile)
        try await waitUntilStoreState { session.deployStore.currentStage != nil }
        let lane = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .deploy))
        let operation = try XCTUnwrap(lane.activeOperation)
        fixture.coordinator.cancel(profileID: fixture.profile.id)
        // The helper acknowledges cancellation, then remains paused in cleanup.
        lane.backend.events.append(.error(operation: "deploy", code: "cancelled", message: "Operation cancelled.",
                                          requestId: operation.id.uuidString))
        XCTAssertEqual(session.deployStore.state, .deployFailed)
        XCTAssertTrue(fixture.coordinator.isDeviceBusy(fixture.profile))
        XCTAssertFalse(session.deployStore.canDeploy)
        XCTAssertEqual(fixture.app.dashboardSummary(for: fixture.profile).displayStatus, .installing)
        fixture.runner.finish(.init("deploy"))
        try await waitUntilStoreState {
            !fixture.coordinator.isDeviceBusy(fixture.profile) &&
                fixture.app.deviceRegistry.profile(id: fixture.profile.id)?.runtimeState?.state == .installFailed
        }
        assertDashboardStopped(try XCTUnwrap(fixture.app.deviceRegistry.profile(id: fixture.profile.id)), app: fixture.app)
        XCTAssertTrue(session.deployStore.canDeploy)
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    func testPlannedMaintenanceCancellationUsesCurrentOptionsThroughCoordinator() async throws {
        for workflow in [MaintenanceWorkflow.fsck, .repairXattrs] {
            let fsck = workflow == .fsck
            let name = fsck ? "fsck" : "repair-xattrs"
            var responses: [StoreTestRunner.Response] = []
            if fsck {
                responses.append(.init(events: [BackendEvent(type: "result", operation: name, ok: true,
                    payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))]))
            }
            responses.append(.init(events: [BackendEvent(type: "result", operation: name, ok: true,
                payload: fsck ? testFsckPlanPayload() : testRepairXattrsPayload(findings: 2, repairable: 1))]))
            responses.append(.init(events: [confirmation(name, id: "cancel")], pauseAfterEvents: true))
            let fixture = try await makeFixture(responses: [.init(name): responses])
            defer { fixture.runner.finishAll() }
            let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
            let store = session.maintenanceStore
            if fsck {
                session.performMaintenanceAction(.findVolumes, profile: fixture.profile, showDiagnostics: {})
                try await waitUntilStoreState { store.fsckState == .listReady && !store.isBusy }
                session.performMaintenanceAction(.planFsck, profile: fixture.profile, showDiagnostics: {})
                try await waitUntilStoreState { store.fsckState == .planReady && !store.isBusy }
            } else {
                store.repairPath = "/Volumes/Data"
                session.performMaintenanceAction(.scanMetadata, profile: fixture.profile, showDiagnostics: {})
                try await waitUntilStoreState { store.repairState == .scanReady && !store.isBusy }
            }
            session.performMaintenanceAction(fsck ? .runFsck : .repairMetadata, profile: fixture.profile, showDiagnostics: {})
            try await waitUntilStoreState { store.pendingConfirmation(for: workflow) != nil }
            if fsck { store.noWait = true } else { store.repairPath = "/Volumes/Other" }
            store.cancelPendingConfirmation(for: workflow)
            XCTAssertEqual(fsck ? store.fsckStore.state : store.repairXattrsStore.state, .awaitingConfirmation)
            fixture.runner.finish(.init(name))
            try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
            fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
            try await waitUntilStoreState { !store.isBusy }
            XCTAssertEqual(fsck ? store.fsckStore.state : store.repairXattrsStore.state, fsck ? .planStale : .scanStale)
            XCTAssertNil(store.pendingConfirmation(for: workflow))
            assertDashboardStopped(fixture.profile, app: fixture.app)
        }
    }

    func testCancelledInstallStopsDashboardEvenWhenTerminalPersistenceFails() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [.init(events: [confirmation("deploy", id: "cancel")])]
        ])
        let registry = fixture.app.deviceRegistry
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runInstall(profile: fixture.profile)
        try await waitUntilStoreState {
            fixture.coordinator.readyConfirmation != nil && registry.profile(id: fixture.profile.id)?.lastDeployState?.status == .awaitingConfirmation
        }
        // Make atomic replacement fail in this test's disposable registry.
        try FileManager.default.removeItem(at: registry.registryURL)
        try FileManager.default.createDirectory(at: registry.registryURL, withIntermediateDirectories: false)
        fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
        try await waitUntilStoreState { registry.state == .failed }
        XCTAssertNotNil(registry.error)
        XCTAssertEqual(session.deployStore.error?.code, "confirmation_cancelled")
        let saved = try XCTUnwrap(registry.profile(id: fixture.profile.id))
        XCTAssertEqual(saved.runtimeState?.state, .installing, "A failed write must not pretend to have persisted")
        assertDashboardStopped(saved, app: fixture.app)
    }

    func testDeployCompletionPersistsAfterInitialRegistryWriteFails() async throws {
        for hadPreviousAttempt in [false, true] {
            for completion in ["success", "failure", "confirmation_cancelled"] {
                let cancelled = completion == "confirmation_cancelled"
                let succeeded = completion == "success"
                let initial = cancelled ? confirmation("deploy", id: "recover")
                    : BackendEvent(type: "stage", operation: "deploy", stage: "check_compatibility", cancellable: true)
                let fixture = try await makeFixture(responses: [
                    .init("deploy"): [.init(events: [initial], pauseAfterEvents: true)],
                    .init("set-ssh"): [.init(events: [sshResult()])]
                ])
                defer { fixture.runner.finishAll() }
                let registry = fixture.app.deviceRegistry
                if hadPreviousAttempt {
                    var previous = testDeployState(status: .succeeded)
                    previous.operationID = UUID().uuidString
                    await registry.updateInstallOperationState(deployState: previous,
                        runtimeState: testRuntimeState(state: .installedVerified), for: fixture.profile.id)
                }
                let before = try XCTUnwrap(registry.profile(id: fixture.profile.id))
                let originalData = try Data(contentsOf: registry.registryURL)
                // Fail the real atomic write without changing the in-memory registry.
                // Restore the file only after every initial queued write has drained.
                try FileManager.default.removeItem(at: registry.registryURL)
                try FileManager.default.createDirectory(at: registry.registryURL, withIntermediateDirectories: false)
                let session = DeviceDashboardSession(profile: before, appStore: fixture.app)
                session.deployStore.rsyncEnabled = true
                session.runInstall(profile: before)
                let lane = fixture.coordinator.lane(for: .deviceWorkflow(before.id, .deploy))
                let operation = try XCTUnwrap(lane.activeOperation)
                try await waitUntilStoreState {
                    cancelled ? session.deployStore.state == .awaitingConfirmation : session.deployStore.currentStage != nil
                }
                try await drainOperationUpdates(registry)
                XCTAssertEqual(registry.state, .failed)
                XCTAssertNotNil(registry.error)
                XCTAssertEqual(registry.profile(id: before.id)?.lastDeployState, before.lastDeployState)
                XCTAssertEqual(registry.profile(id: before.id)?.runtimeState, before.runtimeState)
                try FileManager.default.removeItem(at: registry.registryURL)
                try originalData.write(to: registry.registryURL, options: .atomic)

                if cancelled {
                    fixture.runner.finish(.init("deploy"))
                    try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
                    fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
                } else {
                    let terminal = succeeded
                        ? BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
                        : failure("deploy")
                    lane.backend.events.append(terminal.withRequestId(operation.id.uuidString))
                    fixture.runner.finish(.init("deploy"))
                }
                try await waitUntilStoreState {
                    !fixture.coordinator.isDeviceBusy(before)
                        && (completion != "failure" || fixture.app.sshAccessStore.snapshot(for: before) != nil)
                }
                try await drainOperationUpdates(registry)
                XCTAssertEqual(session.deployStore.state, succeeded ? .deployed : .deployFailed)
                let expectedError = succeeded ? nil : (cancelled ? "confirmation_cancelled" : "remote_error")
                XCTAssertEqual(session.deployStore.error?.code, expectedError)

                let reloaded = DeviceRegistryStore(applicationSupportURL: registry.applicationSupportURL)
                await reloaded.load()
                for saved in [try XCTUnwrap(registry.profile(id: before.id)), try XCTUnwrap(reloaded.profile(id: before.id))] {
                    let context = "\(completion), previous attempt: \(hadPreviousAttempt)"
                    XCTAssertEqual(saved.lastDeployState?.operationID, operation.id.uuidString, context)
                    XCTAssertEqual(saved.lastDeployState?.status, succeeded ? .succeeded : .failed, context)
                    XCTAssertNotNil(saved.lastDeployState?.finishedAt, context)
                    XCTAssertEqual(saved.lastDeployState?.errorCode, expectedError, context)
                    XCTAssertEqual(saved.runtimeState?.state, succeeded ? .installedVerified : .installFailed, context)
                    XCTAssertEqual(saved.runtimeState?.errorCode, expectedError, context)
                    XCTAssertEqual(saved.settings.rsyncEnabled, succeeded, context)
                    assertDashboardStopped(saved, app: fixture.app)
                }
            }
        }
    }

    func testOldDeployEventsCannotChangeNewAttemptOrItsPersistedState() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [
                .init(events: [confirmation("deploy", id: "old")]),
                .init(events: [BackendEvent(type: "stage", operation: "deploy", stage: "check_compatibility", cancellable: true)],
                      pauseAfterEvents: true)
            ]
        ])
        defer { fixture.runner.finishAll() }
        let registry = fixture.app.deviceRegistry
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        let lane = fixture.coordinator.lane(for: .deviceWorkflow(fixture.profile.id, .deploy))
        session.runInstall(profile: fixture.profile)
        let oldOperation = try XCTUnwrap(lane.activeOperation)
        try await waitUntilStoreState { fixture.coordinator.readyConfirmation != nil }
        fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
        try await drainOperationUpdates(registry)
        XCTAssertFalse(fixture.coordinator.isDeviceBusy(fixture.profile))

        session.runInstall(profile: fixture.profile)
        let currentOperation = try XCTUnwrap(lane.activeOperation)
        XCTAssertNotEqual(currentOperation.id, oldOperation.id)
        try await waitUntilStoreState { session.deployStore.currentStage?.stage == "check_compatibility" }
        try await drainOperationUpdates(registry)
        let before = try XCTUnwrap(registry.profile(id: fixture.profile.id))
        XCTAssertEqual(before.lastDeployState?.operationID, currentOperation.id.uuidString)
        XCTAssertEqual(before.lastDeployState?.status, .deploying)
        XCTAssertTrue(lane.backend.canCancel)

        for event in [
            BackendEvent(type: "stage", operation: "deploy", stage: "reboot", cancellable: false),
            confirmation("deploy", id: "stale"),
            failure("deploy"),
            BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
        ] {
            await fixture.runner.send(event.withRequestId(oldOperation.id.uuidString), to: currentOperation.id.uuidString)
        }
        try await drainOperationUpdates(registry)
        XCTAssertEqual(session.deployStore.state, .deploying)
        XCTAssertEqual(session.deployStore.currentStage?.stage, "check_compatibility")
        XCTAssertNil(session.deployStore.error)
        XCTAssertNil(session.deployStore.result)
        XCTAssertEqual(lane.backend.currentStage, "check_compatibility")
        XCTAssertTrue(lane.backend.canCancel)
        XCTAssertNil(lane.backend.pendingConfirmation)
        XCTAssertNil(fixture.coordinator.readyConfirmation)
        XCTAssertEqual(registry.profile(id: before.id)?.lastDeployState, before.lastDeployState)
        XCTAssertEqual(registry.profile(id: before.id)?.runtimeState, before.runtimeState)
        XCTAssertTrue(fixture.coordinator.isDeviceBusy(before))

        await fixture.runner.send(BackendEvent(type: "result", operation: "deploy", ok: true,
            payload: testDeployResultPayload()).withRequestId(currentOperation.id.uuidString), to: currentOperation.id.uuidString)
        fixture.runner.finish(.init("deploy"))
        try await waitUntilStoreState { !lane.backend.isRunning }
        XCTAssertFalse(fixture.coordinator.isDeviceBusy(before))
        XCTAssertNil(lane.backend.pendingConfirmation)
        try await drainOperationUpdates(registry)
        XCTAssertEqual(session.deployStore.state, .deployed)
        let reloaded = DeviceRegistryStore(applicationSupportURL: registry.applicationSupportURL)
        await reloaded.load()
        let saved = try XCTUnwrap(reloaded.profile(id: before.id))
        XCTAssertEqual(saved.lastDeployState?.operationID, currentOperation.id.uuidString)
        XCTAssertEqual(saved.lastDeployState?.status, .succeeded)
        XCTAssertEqual(saved.runtimeState?.state, .installedVerified)
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "deploy"])
    }

    func testCheckupReplacesOrphanedInstallingStateAndSurvivesReload() async throws {
        let fixture = try await makeFixture(responses: [
            .init("doctor"): [.init(events: [BackendEvent(type: "result", operation: "doctor", ok: true,
                payload: testDoctorPayload(checks: [testDoctorCheck(status: "PASS", message: "Running", domain: "Runtime")]))])]
        ])
        let registry = fixture.app.deviceRegistry
        await registry.updateInstallOperationState(
            deployState: testDeployState(status: .awaitingConfirmation, finishedAt: nil),
            runtimeState: testRuntimeState(state: .installing), for: fixture.profile.id)
        assertDashboardStopped(try XCTUnwrap(registry.profile(id: fixture.profile.id)), app: fixture.app)
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runCheckup(profile: fixture.profile)
        try await waitUntilStoreState {
            !fixture.coordinator.isDeviceBusy(fixture.profile) && registry.profile(id: fixture.profile.id)?.runtimeState?.state == .installedVerified
        }
        XCTAssertEqual(registry.profile(id: fixture.profile.id)?.lastDeployState?.status, .interrupted)
        let reloaded = DeviceRegistryStore(applicationSupportURL: registry.applicationSupportURL)
        await reloaded.load()
        let saved = try XCTUnwrap(reloaded.profile(id: fixture.profile.id))
        XCTAssertEqual(saved.runtimeState?.state, .installedVerified)
        XCTAssertEqual(saved.lastCheckup?.state, .passed)
        assertDashboardStopped(saved, app: fixture.app)
    }

    func testCancelledInstallCannotRecreateDeletedProfileOrChangeSelection() async throws {
        let fixture = try await makeFixture(responses: [
            .init("deploy"): [.init(events: [confirmation("deploy", id: "cancel")])]
        ])
        let registry = fixture.app.deviceRegistry
        let other = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil, passwordState: .available, preferredID: "other")
        let session = DeviceDashboardSession(profile: fixture.profile, appStore: fixture.app)
        session.runInstall(profile: fixture.profile)
        try await waitUntilStoreState {
            fixture.coordinator.readyConfirmation != nil && registry.profile(id: fixture.profile.id)?.lastDeployState?.status == .awaitingConfirmation
        }
        fixture.app.select(other)
        try await registry.delete(fixture.profile)
        fixture.coordinator.cancel(try XCTUnwrap(fixture.coordinator.readyConfirmation))
        // A queued sentinel waits for the attempted cancellation write to drain.
        var drained = false
        registry.enqueueOperationUpdate { drained = true }
        try await waitUntilStoreState { drained }
        XCTAssertNil(registry.profile(id: fixture.profile.id))
        XCTAssertEqual(registry.profile(id: other.id), other)
        XCTAssertEqual(fixture.app.selectedProfile?.id, other.id)
        XCTAssertFalse(fixture.coordinator.isDeviceBusy(fixture.profile))
    }

    private func assertDashboardStopped(_ profile: DeviceProfile, app: AppStore,
                                        file: StaticString = #filePath, line: UInt = #line) {
        let overview = DeviceDashboardOverviewPresentation(summary: app.dashboardSummary(for: profile))
        for section in overview.healthSections where section.domain == .connection || section.domain == .runtime {
            XCTAssertFalse(section.rows.contains { $0.status == .running }, file: file, line: line)
        }
        XCTAssertTrue(overview.isEnabled(.runCheckup), file: file, line: line)
        XCTAssertTrue(overview.isEnabled(.installUpdate), file: file, line: line)
    }

    private func drainOperationUpdates(_ registry: DeviceRegistryStore) async throws {
        var drained = false
        registry.enqueueOperationUpdate { drained = true }
        try await waitUntilStoreState { drained }
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
        let profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(), discoveredDevice: nil,
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
