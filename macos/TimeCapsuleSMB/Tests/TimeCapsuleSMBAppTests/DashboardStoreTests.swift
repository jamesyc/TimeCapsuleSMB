import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DashboardStoreTests: XCTestCase {
    func testNoDeviceRegistryLeavesNoSelectedProfile() async throws {
        let fixture = try await makeFixture(responses: [])

        XCTAssertEqual(fixture.registry.state, .empty)
        XCTAssertNil(fixture.appStore.selectedProfile)
    }

    func testPrimaryActionDerivesFromPasswordAndRuntimeState() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )

        XCTAssertEqual(fixture.appStore.dashboardSummary(for: profile).primaryAction, .replacePassword)

        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: profile).primaryAction, .runCheckup)

        await fixture.registry.updateRuntimeState(testRuntimeState(source: .doctor, summary: ""), for: profile.id)
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: checked).primaryAction, .openSMB)

        await fixture.registry.updateRuntimeState(testRuntimeState(summary: "Install completed."), for: profile.id)
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: installed).primaryAction, .openSMB)

        await fixture.registry.updateRuntimeState(testRuntimeState(state: .installedUnverified, source: .doctor, verified: false, summary: "warning"), for: profile.id)
        let warning = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: warning).primaryAction, .viewCheckup)
    }

    func testDashboardSummaryChecksAvailabilityWithoutReadingPasswordSecret() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.passwordStore.resetTracking()

        let summary = fixture.appStore.dashboardSummary(for: profile)

        XCTAssertEqual(summary.passwordState, .available)
        XCTAssertEqual(summary.primaryAction, .runCheckup)
        XCTAssertEqual(fixture.passwordStore.passwordReadCount, 0)
        XCTAssertEqual(fixture.passwordStore.availabilityReadCount, 1)
    }

    func testLaunchPasswordStateRefreshDoesNotReadPasswordSecrets() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.passwordStore.resetTracking()

        await fixture.appStore.refreshPasswordStates()

        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .available)
        XCTAssertEqual(fixture.passwordStore.passwordReadCount, 0)
        XCTAssertEqual(fixture.passwordStore.availabilityReadCount, 1)
    }

    func testPrimaryActionsRouteThroughDashboardSession() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let opener = RecordingURLOpener()
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore, urlOpener: opener)

        session.performPrimaryAction(.runCheckup, profile: profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(session.selectedTab, .checkup)

        session.performPrimaryAction(.installSMB, profile: profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 2 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[1].operation, "deploy")
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(session.selectedTab, .install)

        session.performPrimaryAction(.viewCheckup, profile: profile)
        XCTAssertEqual(session.selectedTab, .checkup)

        session.profileEditorStore.replacementPassword = "draft"
        session.performPrimaryAction(.replacePassword, profile: profile)
        XCTAssertEqual(session.selectedTab, .settings)
        XCTAssertEqual(session.profileEditorStore.replacementPassword, "draft")
        XCTAssertNil(session.profileEditorStore.passwordError)

        session.performPrimaryAction(.openSMB, profile: profile)
        XCTAssertEqual(opener.openedURLs.map(\.absoluteString), ["smb://10.0.0.2"])
    }

    func testOpenSMBPrimaryActionUsesBonjourHostnameWhenAvailable() async throws {
        let fixture = try await makeFixture(responses: [])
        let discovered = DiscoveredDevice(
            payload: try testDiscoveredDevice(
                host: "10.0.0.2",
                hostname: "office-capsule.local.",
                fullname: "Office Capsule._airport._tcp.local."
            ).decode(DiscoveredDevicePayload.self),
            index: 0
        )
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: discovered,
            passwordState: .available,
            preferredID: "device-one"
        )
        let opener = RecordingURLOpener()
        let session = DeviceDashboardSession(
            profile: profile,
            appStore: fixture.appStore,
            urlOpener: opener,
            smbAccountResolver: StaticSMBAccountResolver(accounts: [profile.id: "jameschang"])
        )

        session.performPrimaryAction(.openSMB, profile: profile)

        XCTAssertEqual(opener.openedURLs.map(\.absoluteString), ["smb://jameschang@Office%20Capsule._smb._tcp.local"])
    }

    func testRefreshStatusSecondaryActionRunsReachability() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

        session.performSecondaryAction(.refreshStatus, profile: profile)
        try await waitUntilStoreState { fixture.appStore.reachabilityStore.snapshot(for: profile) != nil }

        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["reachability"])
        XCTAssertEqual(fixture.runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(fixture.appStore.reachabilityStore.snapshot(for: profile)?.payload.status, "reachable")
    }

    func testRefreshStatusDoesNotClearSuccessfulDeployTimeline() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd"),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

        session.runInstallPlan(profile: profile)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: profile)
        try await waitUntilStoreState { session.deployStore.state == .deployed }

        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        let beforeRefresh = InstallWorkflowPresentation(
            state: session.deployStore.state,
            plan: session.deployStore.plan,
            result: session.deployStore.result,
            error: session.deployStore.error,
            events: session.deployStore.events,
            currentStage: session.deployStore.currentStage,
            profile: installed
        )
        XCTAssertEqual(beforeRefresh.timeline?.items.map(\.title), ["Upload smbd", "Done"])

        session.performSecondaryAction(.refreshStatus, profile: installed)
        try await waitUntilStoreState { fixture.appStore.reachabilityStore.snapshot(for: installed) != nil }

        let afterRefresh = InstallWorkflowPresentation(
            state: session.deployStore.state,
            plan: session.deployStore.plan,
            result: session.deployStore.result,
            error: session.deployStore.error,
            events: session.deployStore.events,
            currentStage: session.deployStore.currentStage,
            profile: installed
        )
        XCTAssertEqual(afterRefresh.timeline?.items.map(\.title), ["Upload smbd", "Done"])
        XCTAssertEqual(afterRefresh.timeline?.items.last?.detail, "Samba installation or update completed.")
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "deploy", "reachability"])
    }

    func testDeployFailureRefreshesSSHAccessStatusWithoutSSHSpecificErrorText() async throws {
        let fixture = try await makeFixture(responses: [
            .init(
                events: [
                    BackendEvent.error(
                        operation: "deploy",
                        code: "remote_error",
                        message: "Compatibility check failed."
                    )
                ],
                result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
            ),
            .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

        session.runInstall(profile: profile)
        try await waitUntilStoreState {
            fixture.runner.calls.map(\.operation) == ["deploy", "set-ssh"] &&
                fixture.appStore.sshAccessStore.snapshot(for: profile) != nil
        }

        XCTAssertEqual(fixture.runner.calls[1].params["action"], .string("status"))
        XCTAssertEqual(session.sshAccessNotice(for: profile)?.host, "10.0.0.2")
    }

    func testCheckupDoesNotClearSuccessfulDeployTimeline() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd"),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

        session.runInstallPlan(profile: profile)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: profile)
        try await waitUntilStoreState { session.deployStore.state == .deployed }
        session.runCheckup(profile: profile)
        try await waitUntilStoreState { session.doctorStore.state == .passed }

        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        let presentation = InstallWorkflowPresentation(
            state: session.deployStore.state,
            plan: session.deployStore.plan,
            result: session.deployStore.result,
            error: session.deployStore.error,
            events: session.deployStore.events,
            currentStage: session.deployStore.currentStage,
            profile: installed
        )
        XCTAssertEqual(presentation.timeline?.items.map(\.title), ["Upload smbd", "Done"])
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["deploy", "deploy", "doctor"])
    }

    func testProfileEditorPasswordSaveUpdatesPasswordStateAndClearsDraft() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)
        session.performPrimaryAction(.replacePassword, profile: profile)
        session.profileEditorStore.replacementPassword = "new-password"

        await session.profileEditorStore.save(profile: profile)

        XCTAssertEqual(session.profileEditorStore.replacementPassword, "")
        XCTAssertNil(session.profileEditorStore.passwordError)
        XCTAssertEqual(try fixture.passwordStore.password(for: profile.keychainAccount), "new-password")
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .available)
    }

    func testProfileEditorPasswordSaveFailureKeepsDraft() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        fixture.passwordStore.saveFailure = .save
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)
        session.profileEditorStore.replacementPassword = "new-password"

        await session.profileEditorStore.save(profile: profile)

        XCTAssertEqual(session.profileEditorStore.replacementPassword, "new-password")
        XCTAssertEqual(session.profileEditorStore.passwordError, "In-memory password store save failed.")
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .missing)
    }

    func testDashboardSessionsAreIsolatedByProfile() async throws {
        let fixture = try await makeFixture(responses: [])
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)

        let firstSession = dashboard.session(for: first)
        firstSession.selectedTab = .maintenance
        firstSession.profileEditorStore.replacementPassword = "draft"
        firstSession.deployStore.mountWait = "77"
        firstSession.maintenanceStore.selectedWorkflow = .fsck

        let secondSession = dashboard.session(for: second)

        XCTAssertFalse(firstSession === secondSession)
        XCTAssertEqual(secondSession.selectedTab, .overview)
        XCTAssertEqual(secondSession.profileEditorStore.replacementPassword, "")
        XCTAssertEqual(secondSession.deployStore.mountWait, "30")
        XCTAssertEqual(secondSession.maintenanceStore.selectedWorkflow, .activate)
    }

    func testSessionDefaultsComeFromProfileSettingsAndDoNotResetOnSnapshotUpdates() async throws {
        let fixture = try await makeFixture(responses: [])
        var profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        profile.settings = DeviceProfileSettings(
            nbnsEnabled: false,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            mdnsAdvertiseAFP: true,
            anyProtocol: true,
            fruitMetadataNetatalk: true,
            debugLogging: true,
            mountWaitSeconds: 45,
            ataIdleSeconds: 0,
            ataStandby: 0
        )
        profile = try await fixture.registry.updateProfile(profile)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        XCTAssertEqual(session.deployStore.nbnsEnabled, false)
        XCTAssertEqual(session.deployStore.internalShareUseDiskRoot, true)
        XCTAssertEqual(session.deployStore.smbBrowseCompatibility, true)
        XCTAssertEqual(session.deployStore.mdnsAdvertiseAFP, true)
        XCTAssertEqual(session.deployStore.anyProtocol, true)
        XCTAssertEqual(session.deployStore.fruitMetadataNetatalk, true)
        XCTAssertEqual(session.deployStore.debugLogging, true)
        XCTAssertEqual(session.deployStore.ataIdleSeconds, "0")
        XCTAssertEqual(session.deployStore.ataStandby, "0")
        XCTAssertEqual(session.deployStore.mountWait, "45")
        XCTAssertEqual(session.maintenanceStore.mountWait, "45")

        session.deployStore.mountWait = "12"
        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 1,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        ), for: profile.id)

        XCTAssertEqual(session.deployStore.mountWait, "12")
    }

    func testProfileEditorSaveAppliesSettingsBackToSessionDefaults() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.profileEditorStore.draft.nbnsEnabled = false
        session.profileEditorStore.draft.internalShareUseDiskRoot = true
        session.profileEditorStore.draft.smbBrowseCompatibility = true
        session.profileEditorStore.draft.mdnsAdvertiseAFP = true
        session.profileEditorStore.draft.anyProtocol = true
        session.profileEditorStore.draft.fruitMetadataNetatalk = true
        session.profileEditorStore.draft.debugLogging = true
        session.profileEditorStore.draft.mountWaitSeconds = "64"
        session.profileEditorStore.draft.ataIdleSeconds = "0"
        session.profileEditorStore.draft.ataStandby = "0"

        await session.profileEditorStore.save(profile: profile)

        XCTAssertEqual(session.profileEditorStore.state, .saved)
        XCTAssertEqual(session.deployStore.nbnsEnabled, false)
        XCTAssertEqual(session.deployStore.internalShareUseDiskRoot, true)
        XCTAssertEqual(session.deployStore.smbBrowseCompatibility, true)
        XCTAssertEqual(session.deployStore.mdnsAdvertiseAFP, true)
        XCTAssertEqual(session.deployStore.anyProtocol, true)
        XCTAssertEqual(session.deployStore.fruitMetadataNetatalk, true)
        XCTAssertEqual(session.deployStore.debugLogging, true)
        XCTAssertEqual(session.deployStore.ataIdleSeconds, "0")
        XCTAssertEqual(session.deployStore.ataStandby, "0")
        XCTAssertEqual(session.deployStore.mountWait, "64")
        XCTAssertEqual(session.maintenanceStore.mountWait, "64")
    }

    func testDeletingProfilePrunesInactiveSession() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)
        var session: DeviceDashboardSession? = dashboard.session(for: profile)
        weak var weakSession = session
        XCTAssertNotNil(weakSession)
        session = nil

        try await fixture.registry.delete(profile)

        try await waitUntilStoreState { weakSession == nil }
    }

    func testDeletedProfileSessionStaysUntilStartedOperationFinishes() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ], pauseBeforeEvents: true)
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        var session: DeviceDashboardSession? = dashboard.session(for: profile)
        weak var weakSession = session

        session?.runCheckup(profile: profile)
        try await waitUntilStoreState { self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        try await fixture.registry.delete(profile)
        session = nil

        XCTAssertNotNil(weakSession)
        fixture.runner.finishAll()
        try await waitUntilStoreState { !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        try await waitUntilStoreState { weakSession == nil }
    }

    func testOperationRunningOnAnotherDeviceAllowsNewSessionOperation() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ], pauseBeforeEvents: true),
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ])
        ])
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        try fixture.passwordStore.save("pw1", for: first.keychainAccount)
        try fixture.passwordStore.save("pw2", for: second.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let firstSession = dashboard.session(for: first)
        let secondSession = dashboard.session(for: second)

        firstSession.runCheckup(profile: first)
        try await waitUntilStoreState { self.deviceLaneIsRunning(first, appStore: fixture.appStore) }
        secondSession.runCheckup(profile: second)

        try await waitUntilStoreState { fixture.runner.calls.count == 2 }
        XCTAssertEqual(secondSession.doctorStore.state, .running)
        try await waitUntilStoreState { secondSession.doctorStore.state == .passed }
        XCTAssertEqual(Set(fixture.runner.calls.map { $0.context?.profileID }), ["device-one", "device-two"])
        fixture.runner.finishAll()
        try await waitUntilStoreState { !self.deviceLaneIsRunning(first, appStore: fixture.appStore) }
    }

    func testDashboardOperationsUpdateCheckupDeployAndRuntimeSnapshots() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime"),
                    testDoctorCheck(status: "WARN", message: "bonjour missing", domain: "Bonjour")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ], pauseBeforeEvents: true)
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.appStore.select(profile)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.runCheckup(profile: profile)

        try await waitUntilStoreState {
            session.doctorStore.state == .warning
                && fixture.registry.profile(id: profile.id)?.lastCheckup?.state == .warning
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .installedUnverified
        }
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(checked.lastCheckup?.state, .warning)
        XCTAssertEqual(checked.lastCheckup?.warnCount, 1)
        XCTAssertEqual(checked.runtimeState?.source, .doctor)
        XCTAssertEqual(checked.runtimeState?.verified, false)
        XCTAssertEqual(fixture.runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(fixture.runner.calls[0].context?.profileID, profile.id)

        session.runInstallPlan(profile: checked)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: checked)

        try await waitUntilStoreState {
            session.deployStore.state == .deploying
                && fixture.registry.profile(id: profile.id)?.lastCheckup == nil
                && session.doctorStore.summary == nil
        }
        fixture.runner.finishAll()
        try await waitUntilStoreState {
            session.deployStore.state == .deployed
                && fixture.registry.profile(id: profile.id)?.lastDeployState?.status == .succeeded
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .installedVerified
        }
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertNil(installed.lastCheckup)
        XCTAssertEqual(installed.lastDeployState?.status, .succeeded)
        XCTAssertEqual(installed.lastDeployState?.payloadFamily, "netbsd6_samba4")
        XCTAssertEqual(installed.lastDeployState?.verified, true)
        XCTAssertEqual(installed.runtimeState?.state, .installedVerified)
        XCTAssertEqual(installed.runtimeState?.payloadFamily, "netbsd6_samba4")
        XCTAssertEqual(installed.runtimeState?.verified, true)
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[2].params["dry_run"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[2].context?.profileID, profile.id)
    }

    func testFailedInstallPersistsUnifiedDeployState() async throws {
        let recovery: JSONValue = .object([
            "title": .string("No HFS volumes found"),
            "message": .string("The device did not report a deployable HFS disk through MaSt."),
            "actions": .array([]),
            "action_ids": .array([]),
            "retryable": .bool(true),
            "suggested_operation": .string("deploy")
        ])
        let failure = "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart."
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "read_mast"),
                BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: failure, recovery: recovery)
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await fixture.registry.updateDeployState(testDeployState(), for: profile.id)
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: installed.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: installed)

        session.runInstallPlan(profile: installed)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: installed)

        try await waitUntilStoreState {
            session.deployStore.state == .deployFailed
                && fixture.registry.profile(id: profile.id)?.lastDeployState?.status == .failed
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .installFailed
        }
        let failed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(failed.lastDeployState?.status, .failed)
        XCTAssertEqual(failed.lastDeployState?.stage, "read_mast")
        XCTAssertEqual(failed.lastDeployState?.errorMessage, failure)
        XCTAssertEqual(failed.lastDeployState?.recovery?.title, "No HFS volumes found")
        XCTAssertEqual(failed.runtimeState?.state, .installFailed)
        XCTAssertEqual(failed.runtimeState?.stage, "read_mast")
        XCTAssertEqual(failed.runtimeState?.errorMessage, failure)
        XCTAssertEqual(failed.runtimeState?.recovery?.title, "No HFS volumes found")
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: failed).displayStatus, .failed)
    }

    func testSuccessfulCheckupAfterFailedInstallUpdatesDeviceRuntimeStateWithoutClearingDeployHistory() async throws {
        let failure = "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart."
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "read_mast"),
                BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: failure)
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.runInstallPlan(profile: profile)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: profile)
        try await waitUntilStoreState {
            session.deployStore.state == .deployFailed
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .installFailed
        }
        let failed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: failed).displayStatus, .failed)

        session.runCheckup(profile: failed)

        try await waitUntilStoreState {
            session.doctorStore.state == .passed
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .installedVerified
        }
        let recovered = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(recovered.lastDeployState?.status, .failed)
        XCTAssertEqual(recovered.lastDeployState?.errorMessage, failure)
        XCTAssertEqual(recovered.runtimeState?.source, .doctor)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: recovered).displayStatus, .healthy)
    }

    func testFactoryDeviceCheckupStoresNotInstalledRuntimeStateWithoutFailedSidebarStatus() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(fatal: true, checks: [
                    testDoctorCheck(
                        status: "FAIL",
                        message: "deployed payload config not found; please run deploy to install on your device",
                        domain: "Runtime",
                        code: DoctorSummary.runtimeNotInstalledResultCode
                    )
                ]))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.runCheckup(profile: profile)

        try await waitUntilStoreState {
            session.doctorStore.state == .failed
                && fixture.registry.profile(id: profile.id)?.runtimeState?.state == .notInstalled
        }
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(checked.lastCheckup?.state, .failed)
        XCTAssertEqual(checked.runtimeState?.source, .doctor)
        XCTAssertEqual(checked.runtimeState?.errorCode, DoctorSummary.runtimeNotInstalledResultCode)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: checked).displayStatus, .readyToInstall)
    }

    func testInstallPlanDoesNotChangePersistedInstallState() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let failedState = testDeployState(
            status: .failed,
            startedAt: Date(timeIntervalSince1970: 120),
            updatedAt: Date(timeIntervalSince1970: 120),
            finishedAt: Date(timeIntervalSince1970: 120),
            stage: "read_mast",
            verified: nil,
            summary: "",
            errorCode: "remote_error",
            errorMessage: "No deployable HFS disk was found."
        )
        let failedRuntimeState = testRuntimeState(
            state: .installFailed,
            stage: "read_mast",
            verified: false,
            summary: "",
            errorCode: "remote_error",
            errorMessage: "No deployable HFS disk was found."
        )
        await fixture.registry.updateDeployState(failedState, for: profile.id)
        await fixture.registry.updateRuntimeState(failedRuntimeState, for: profile.id)
        let failed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: failed.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: failed)

        session.runInstallPlan(profile: failed)

        try await waitUntilStoreState { session.deployStore.state == .planReady }
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.lastDeployState, failedState)
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.runtimeState, failedRuntimeState)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: failed).displayStatus, .failed)
    }

    func testSuccessfulUninstallClearsInstalledSnapshot() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallResultPayload(waited: true, verified: true))
            ], pauseBeforeEvents: true)
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await fixture.registry.updateDeployState(testDeployState(), for: profile.id)
        await fixture.registry.updateRuntimeState(testRuntimeState(), for: profile.id)
        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 130),
            state: .failed,
            passCount: 1,
            warnCount: 0,
            failCount: 1,
            summary: "failed"
        ), for: profile.id)
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: installed.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: installed)

        session.performMaintenanceAction(.runUninstall, profile: installed) {}

        try await waitUntilStoreState {
            session.maintenanceStore.uninstallState == .running
                && fixture.registry.profile(id: installed.id)?.lastCheckup == nil
        }
        fixture.runner.finishAll()
        try await waitUntilStoreState {
            session.maintenanceStore.uninstallState == .succeeded
                && fixture.registry.profile(id: installed.id)?.lastDeployState == nil
                && fixture.registry.profile(id: installed.id)?.runtimeState == nil
        }
        XCTAssertNil(fixture.registry.profile(id: installed.id)?.lastDeployState)
        XCTAssertNil(fixture.registry.profile(id: installed.id)?.runtimeState)
        XCTAssertNil(fixture.registry.profile(id: installed.id)?.lastCheckup)
        XCTAssertEqual(fixture.runner.calls[0].params["dry_run"], .bool(false))
    }

    func testActivationInvalidatesCheckupWhenRunStarts() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: false))
            ], pauseBeforeEvents: true)
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 130),
            state: .failed,
            passCount: 1,
            warnCount: 0,
            failCount: 1,
            summary: "failed"
        ), for: profile.id)
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: checked.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: checked)

        session.performMaintenanceAction(.runActivation, profile: checked) {}

        try await waitUntilStoreState {
            session.maintenanceStore.activateState == .running
                && fixture.registry.profile(id: checked.id)?.lastCheckup == nil
        }
        fixture.runner.finishAll()
        try await waitUntilStoreState { session.maintenanceStore.activateState == .succeeded }
        XCTAssertEqual(fixture.runner.calls[0].params["dry_run"], .bool(false))
    }

    func testCheckupSnapshotUsesStartedProfileWhenSelectionChanges() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ], pauseBeforeEvents: true)
        ])
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        try fixture.passwordStore.save("pw", for: first.keychainAccount)
        fixture.appStore.select(first)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: first)

        session.runCheckup(profile: first)
        fixture.appStore.select(second)
        fixture.runner.finishAll()

        try await waitUntilStoreState {
            session.doctorStore.state == .passed
                && fixture.registry.profile(id: first.id)?.lastCheckup?.state == .passed
                && fixture.registry.profile(id: first.id)?.runtimeState?.state == .installedVerified
        }
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.lastCheckup?.state, .passed)
        XCTAssertNil(fixture.registry.profile(id: first.id)?.lastDeployState)
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.runtimeState?.source, .doctor)
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.runtimeState?.verified, true)
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.runtimeState?.summary, "")
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.runtimeState?.localizedSummary, "Installed and verified by checkup.")
        XCTAssertNil(fixture.registry.profile(id: second.id)?.lastCheckup)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.lastDeployState)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.runtimeState)
    }

    func testSkippedSSHCheckupDoesNotMarkRuntimeInstalled() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "local checks passed", domain: "General")
                ]))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)
        session.doctorStore.skipSSH = true

        session.runCheckup(profile: profile)

        try await waitUntilStoreState {
            session.doctorStore.state == .passed
                && fixture.registry.profile(id: profile.id)?.lastCheckup?.state == .passed
        }
        XCTAssertNil(fixture.registry.profile(id: profile.id)?.lastDeployState)
        XCTAssertNil(fixture.registry.profile(id: profile.id)?.runtimeState)
    }

    func testDeploySnapshotUsesStartedProfileWhenSelectionChanges() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ], pauseBeforeEvents: true)
        ])
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        try fixture.passwordStore.save("pw", for: first.keychainAccount)
        fixture.appStore.select(first)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: first)

        session.runInstallPlan(profile: first)
        try await waitUntilStoreState { session.deployStore.state == .planReady }
        session.runInstall(profile: first)
        fixture.appStore.select(second)
        fixture.runner.finishAll()

        try await waitUntilStoreState { session.deployStore.state == .deployed }
        try await waitUntilStoreState {
            fixture.registry.profile(id: first.id)?.lastDeployState?.status == .succeeded
                && fixture.registry.profile(id: first.id)?.runtimeState?.state == .installedVerified
        }
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.lastDeployState?.status, .succeeded)
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.runtimeState?.state, .installedVerified)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.lastDeployState)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.runtimeState)
    }

    func testPasswordLookupFailureMarksProfileMissing() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .unknown,
            preferredID: "device-one"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.runCheckup(profile: profile)

        XCTAssertEqual(session.profileEditorStore.passwordError, "Password is required.")
        XCTAssertEqual(session.selectedTab, .settings)
        try await waitUntilStoreState {
            fixture.registry.profile(id: profile.id)?.passwordState == .missing
        }
    }

    func testAuthFailureMarksSavedPasswordInvalid() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "doctor", code: "auth_failed", message: "Password rejected.")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("bad-password", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)

        session.runCheckup(profile: profile)

        try await waitUntilStoreState {
            fixture.registry.profile(id: profile.id)?.passwordState == .invalid
        }
        let updated = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(session.doctorStore.state, .runFailed)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: updated).primaryAction, .replacePassword)
    }

    func testRecoveryActionsRouteToMaintenanceAndPasswordWorkflows() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)
        let error = BackendErrorViewModel(operation: "doctor", code: "operation_failed", message: "Needs recovery.")

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Run Disk Repair", kind: .diskRepair),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(session.selectedTab, .maintenance)
        XCTAssertEqual(session.maintenanceStore.selectedWorkflow, .fsck)

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Repair File Metadata", kind: .metadataRepair),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(session.maintenanceStore.selectedWorkflow, .repairXattrs)

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Activate", kind: .startSMB),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(session.maintenanceStore.selectedWorkflow, .activate)

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Replace Password", kind: .replacePassword),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(session.selectedTab, .settings)
        XCTAssertNil(session.profileEditorStore.passwordError)
    }

    func testRecoveryRunCheckupAndInstallActionsStartBackendOperations() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "Needs recovery.")

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Run Checkup", kind: .runCheckup),
            error: error,
            profile: profile
        ))
        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(fixture.runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(session.selectedTab, .checkup)

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Install Samba", kind: .installSMB),
            error: error,
            profile: profile
        ))
        try await waitUntilStoreState { fixture.runner.calls.count == 2 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[1].operation, "deploy")
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[1].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(session.selectedTab, .install)
    }

    func testRecoveryRetryUsesFailedOperation() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)
        let doctorError = BackendErrorViewModel(operation: "doctor", code: "operation_failed", message: "Doctor failed.")

        XCTAssertTrue(session.handleRecoveryAction(
            RecoveryAction(title: "Retry", kind: .retry),
            error: doctorError,
            profile: profile
        ))

        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(session.selectedTab, .checkup)
    }

    func testNonActionableRecoveryKindsReturnFalse() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let dashboard = DashboardStore(appStore: fixture.appStore)
        let session = dashboard.session(for: profile)
        let error = BackendErrorViewModel(operation: "validate-install", code: "operation_failed", message: "Needs diagnostics.")

        XCTAssertFalse(session.handleRecoveryAction(
            RecoveryAction(title: "Open Diagnostics", kind: .diagnostics),
            error: error,
            profile: profile
        ))
        XCTAssertFalse(session.handleRecoveryAction(
            RecoveryAction(title: "Unknown", kind: .generic),
            error: error,
            profile: profile
        ))
    }

    func testInstallCompletionActionsRunThroughSession() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let opener = RecordingURLOpener()
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore, urlOpener: opener)
        var diagnosticsShown = false

        session.performInstallAction(.openFinder, profile: profile) {
            diagnosticsShown = true
        }
        XCTAssertEqual(opener.openedURLs.map(\.absoluteString), ["smb://10.0.0.2"])
        XCTAssertFalse(diagnosticsShown)

        session.performInstallAction(.runCheckup, profile: profile) {
            diagnosticsShown = true
        }
        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(session.selectedTab, .checkup)

        session.performInstallAction(.reinstall, profile: profile) {
            diagnosticsShown = true
        }
        try await waitUntilStoreState { fixture.runner.calls.count == 2 && !self.deviceLaneIsRunning(profile, appStore: fixture.appStore) }
        XCTAssertEqual(fixture.runner.calls[1].operation, "deploy")
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(session.selectedTab, .install)

        session.performInstallAction(.viewDiagnostics, profile: profile) {
            diagnosticsShown = true
        }
        XCTAssertTrue(diagnosticsShown)
    }

    func testForgetProfileDeletesRegistryConfigDirectoryAndPassword() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let configDirectory = URL(fileURLWithPath: profile.configPath).deletingLastPathComponent()
        XCTAssertTrue(FileManager.default.fileExists(atPath: configDirectory.path))
        fixture.appStore.select(profile)

        try await fixture.appStore.forget(profile)

        XCTAssertEqual(fixture.registry.profiles, [])
        XCTAssertNil(fixture.appStore.selectedProfile)
        XCTAssertNil(fixture.appStore.selectedDeviceID)
        XCTAssertFalse(FileManager.default.fileExists(atPath: configDirectory.path))
        XCTAssertEqual(fixture.passwordStore.state(for: profile.keychainAccount), .missing)
    }

    private func deviceLaneIsRunning(_ profile: DeviceProfile, appStore: AppStore) -> Bool {
        appStore.operationCoordinator.isDeviceBusy(profile)
    }

    private func makeFixture(responses: [StoreTestRunner.Response]) async throws -> (
        appStore: AppStore,
        registry: DeviceRegistryStore,
        passwordStore: InMemoryPasswordStore,
        runner: PausingStoreTestRunner
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = PausingStoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let passwordStore = InMemoryPasswordStore()
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: passwordStore
        )
        return (appStore, registry, passwordStore, runner)
    }
}

private final class RecordingURLOpener: URLOpening {
    private(set) var openedURLs: [URL] = []

    func open(_ url: URL) {
        openedURLs.append(url)
    }
}

private struct StaticSMBAccountResolver: SMBAccountResolving {
    let accounts: [DeviceProfile.ID: String]

    func account(for profile: DeviceProfile) -> String? {
        accounts[profile.id]
    }
}
