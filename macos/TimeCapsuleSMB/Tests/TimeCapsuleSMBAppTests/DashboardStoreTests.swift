import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DashboardStoreTests: XCTestCase {
    func testNoDeviceRegistryLeavesNoSelectedProfile() async throws {
        let fixture = try await makeFixture(responses: [])

        XCTAssertEqual(fixture.registry.state, .empty)
        XCTAssertNil(fixture.appStore.selectedProfile)
    }

    func testPrimaryActionDerivesFromPasswordCheckupAndDeployState() async throws {
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

        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 2,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        ), for: profile.id)
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: checked).primaryAction, .installSMB)

        await fixture.registry.updateDeploy(DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 110),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        ), for: profile.id)
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: installed).primaryAction, .openSMB)

        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 120),
            state: .warning,
            passCount: 1,
            warnCount: 1,
            failCount: 0,
            summary: "warning"
        ), for: profile.id)
        let warning = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: warning).primaryAction, .viewCheckup)
    }

    func testDashboardOperationsUpdateLastCheckupAndDeploySnapshots() async throws {
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
            ])
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

        dashboard.runCheckup(profile: profile)

        try await waitUntilStoreState { dashboard.doctorStore.state == .warning }
        let checked = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(checked.lastCheckup?.state, .warning)
        XCTAssertEqual(checked.lastCheckup?.warnCount, 1)
        XCTAssertEqual(fixture.runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(fixture.runner.calls[0].context?.profileID, profile.id)

        dashboard.runInstallPlan(profile: checked)
        try await waitUntilStoreState { dashboard.deployStore.state == .planReady }
        dashboard.runInstall(profile: checked)

        try await waitUntilStoreState { dashboard.deployStore.state == .deployed }
        let installed = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(installed.lastDeploy?.state, .deployed)
        XCTAssertEqual(installed.lastDeploy?.payloadFamily, "netbsd6_samba4")
        XCTAssertEqual(installed.lastDeploy?.verified, true)
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[2].params["dry_run"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[2].context?.profileID, profile.id)
    }

    func testCheckupSnapshotUsesStartedProfileWhenSelectionChanges() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                ]))
            ], delayNanoseconds: 100_000_000)
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

        dashboard.runCheckup(profile: first)
        fixture.appStore.select(second)

        try await waitUntilStoreState {
            dashboard.doctorStore.state == .passed
                && fixture.registry.profile(id: first.id)?.lastCheckup?.state == .passed
        }
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.lastCheckup?.state, .passed)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.lastCheckup)
    }

    func testDeploySnapshotUsesStartedProfileWhenSelectionChanges() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload(payloadFamily: "netbsd6_samba4"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ], delayNanoseconds: 100_000_000)
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

        dashboard.runInstallPlan(profile: first)
        try await waitUntilStoreState { dashboard.deployStore.state == .planReady }
        dashboard.runInstall(profile: first)
        fixture.appStore.select(second)

        try await waitUntilStoreState { dashboard.deployStore.state == .deployed }
        XCTAssertEqual(fixture.registry.profile(id: first.id)?.lastDeploy?.state, .deployed)
        XCTAssertNil(fixture.registry.profile(id: second.id)?.lastDeploy)
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

        dashboard.runCheckup(profile: profile)

        XCTAssertEqual(dashboard.passwordError, "Password is required.")
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

        dashboard.runCheckup(profile: profile)

        try await waitUntilStoreState { dashboard.doctorStore.state == .runFailed }
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .invalid)
        XCTAssertEqual(fixture.appStore.dashboardSummary(for: fixture.registry.profile(id: profile.id)!).primaryAction, .replacePassword)
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
        let error = BackendErrorViewModel(operation: "doctor", code: "operation_failed", message: "Needs recovery.")

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Run Disk Repair", kind: .diskRepair),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(dashboard.selectedTab, .maintenance)
        XCTAssertEqual(dashboard.maintenanceStore.selectedWorkflow, .fsck)

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Repair File Metadata", kind: .metadataRepair),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(dashboard.maintenanceStore.selectedWorkflow, .repairXattrs)

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Start SMB", kind: .startSMB),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(dashboard.maintenanceStore.selectedWorkflow, .activate)

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Replace Password", kind: .replacePassword),
            error: error,
            profile: profile
        ))
        XCTAssertEqual(dashboard.selectedTab, .overview)
    }

    func testRecoveryRunCheckupAndInstallActionsStartBackendOperations() async throws {
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
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "Needs recovery.")

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Run Checkup", kind: .runCheckup),
            error: error,
            profile: profile
        ))
        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !fixture.appStore.backend.isRunning }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(fixture.runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(dashboard.selectedTab, .checkup)

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Install SMB", kind: .installSMB),
            error: error,
            profile: profile
        ))
        try await waitUntilStoreState { fixture.runner.calls.count == 2 && !fixture.appStore.backend.isRunning }
        XCTAssertEqual(fixture.runner.calls[1].operation, "deploy")
        XCTAssertEqual(fixture.runner.calls[1].params["dry_run"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[1].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(dashboard.selectedTab, .install)
    }

    func testRecoveryRetryUsesFailedOperation() async throws {
        let fixture = try await makeFixture(responses: [
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
        let doctorError = BackendErrorViewModel(operation: "doctor", code: "operation_failed", message: "Doctor failed.")

        XCTAssertTrue(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Retry", kind: .retry),
            error: doctorError,
            profile: profile
        ))

        try await waitUntilStoreState { fixture.runner.calls.count == 1 && !fixture.appStore.backend.isRunning }
        XCTAssertEqual(fixture.runner.calls[0].operation, "doctor")
        XCTAssertEqual(dashboard.selectedTab, .checkup)
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
        let error = BackendErrorViewModel(operation: "validate-install", code: "operation_failed", message: "Needs diagnostics.")

        XCTAssertFalse(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Open Diagnostics", kind: .diagnostics),
            error: error,
            profile: profile
        ))
        XCTAssertFalse(dashboard.handleRecoveryAction(
            RecoveryAction(title: "Unknown", kind: .generic),
            error: error,
            profile: profile
        ))
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

    private func makeFixture(responses: [StoreTestRunner.Response]) async throws -> (
        appStore: AppStore,
        registry: DeviceRegistryStore,
        passwordStore: InMemoryPasswordStore,
        runner: StoreTestRunner
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = StoreTestRunner(responses: responses)
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
