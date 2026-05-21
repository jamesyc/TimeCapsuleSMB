import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceProfileEditorStoreTests: XCTestCase {
    func testStateAndValidationInventoriesAreExplicit() {
        XCTAssertEqual(DeviceProfileEditorState.allCases.map(\.rawValue), [
            "clean",
            "dirty",
            "invalid",
            "saving",
            "reconfiguring",
            "saved",
            "authFailed",
            "unsupported",
            "failed"
        ])
        XCTAssertEqual(DeviceProfileEditorValidationError.allCases.map(\.rawValue), [
            "hostRequired",
            "duplicateHost",
            "mountWaitInvalid",
            "passwordRequired"
        ])
    }

    func testMountWaitValidationAcceptsZeroAndPositiveIntegersOnly() throws {
        var draft = DeviceProfileEditorDraft(
            displayName: "Office",
            host: "10.0.0.2",
            nbnsEnabled: true,
            debugLogging: false,
            mountWaitSeconds: "0"
        )
        XCTAssertEqual(try draft.validatedSettings().mountWaitSeconds, 0)

        draft.mountWaitSeconds = "45"
        XCTAssertEqual(try draft.validatedSettings().mountWaitSeconds, 45)

        for invalid in ["", "-1", "1.5", "abc"] {
            draft.mountWaitSeconds = invalid
            XCTAssertThrowsError(try draft.validatedSettings()) { error in
                XCTAssertEqual(error as? DeviceProfileEditorValidationError, .mountWaitInvalid)
            }
        }
    }

    func testUnchangedHostSaveUpdatesProfileSettingsWithoutBackendConfigure() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.displayName = "Media Capsule"
        store.draft.nbnsEnabled = false
        store.draft.debugLogging = true
        store.draft.mountWaitSeconds = "45"

        await store.save(profile: profile)

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(store.state, .saved)
        XCTAssertEqual(saved.displayName, "Media Capsule")
        XCTAssertEqual(saved.host, "root@10.0.0.2")
        XCTAssertEqual(saved.settings, DeviceProfileSettings(nbnsEnabled: false, debugLogging: true, mountWaitSeconds: 45))
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testBlankDisplayNameIsAllowedAndFallsBackThroughTitlePolicy() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "TimeCapsule8,119"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.displayName = ""

        await store.save(profile: profile)

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(store.state, .saved)
        XCTAssertEqual(saved.displayName, "")
        XCTAssertEqual(saved.title, "TimeCapsule8,119")
    }

    func testInvalidHostDuplicateHostAndInvalidMountWaitSaveNothing() async throws {
        let fixture = try await makeFixture(responses: [])
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        _ = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        let store = DeviceProfileEditorStore(profile: first, appStore: fixture.appStore)

        store.draft.host = " "
        await store.save(profile: first)
        XCTAssertEqual(store.state, .invalid)
        XCTAssertEqual(store.validationErrors, [.hostRequired])

        store.draft.host = "10.0.0.3"
        await store.save(profile: first)
        XCTAssertEqual(store.state, .invalid)
        XCTAssertEqual(store.validationErrors, [.duplicateHost])

        store.draft.host = first.host
        store.draft.mountWaitSeconds = "bad"
        await store.save(profile: first)
        XCTAssertEqual(store.state, .invalid)
        XCTAssertEqual(store.validationErrors, [.mountWaitInvalid])
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testChangedHostRequiresSavedPassword() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.host = "10.0.0.9"
        await store.save(profile: profile)

        XCTAssertEqual(store.state, .invalid)
        XCTAssertEqual(store.validationErrors, [.passwordRequired])
        XCTAssertEqual(fixture.runner.calls, [])
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.host, "10.0.0.2")
    }

    func testChangedHostRunsConfigureWithExistingProfileContextAndPreservesProfileData() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "configure",
                    ok: true,
                    payload: testConfigurePayload(host: "root@10.0.0.9", syap: "119", model: "TimeCapsule8,119")
                )
            ])
        ])
        var profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await fixture.registry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 1,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        ), for: profile.id)
        await fixture.registry.updateDeploy(DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 110),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        ), for: profile.id)
        profile = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.displayName = "Updated Capsule"
        store.draft.host = "10.0.0.9"
        store.draft.nbnsEnabled = false
        store.draft.debugLogging = true
        store.draft.mountWaitSeconds = "60"

        await store.save(profile: profile)

        try await waitUntilStoreState { store.state == .saved }
        let call = try XCTUnwrap(fixture.runner.calls.first)
        XCTAssertEqual(call.operation, "configure")
        XCTAssertEqual(call.context, profile.runtimeContext)
        XCTAssertEqual(call.params["config"], .string(profile.configPath))
        XCTAssertEqual(call.params["host"], .string("root@10.0.0.9"))
        XCTAssertEqual(call.params["password"], .string("pw"))
        XCTAssertEqual(call.params["persist_password"], .bool(false))

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(saved.id, profile.id)
        XCTAssertEqual(saved.keychainAccount, profile.keychainAccount)
        XCTAssertEqual(saved.displayName, "Updated Capsule")
        XCTAssertEqual(saved.host, "root@10.0.0.9")
        XCTAssertEqual(saved.lastCheckup?.state, .passed)
        XCTAssertEqual(saved.lastDeploy?.state, .deployed)
        XCTAssertEqual(saved.settings, DeviceProfileSettings(nbnsEnabled: false, debugLogging: true, mountWaitSeconds: 60))
    }

    func testAuthFailureAndUnsupportedDeviceSaveNothing() async throws {
        let auth = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "auth_failed", message: "bad password")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let authProfile = try await auth.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try auth.passwordStore.save("bad", for: authProfile.keychainAccount)
        let authStore = DeviceProfileEditorStore(profile: authProfile, appStore: auth.appStore)
        authStore.draft.host = "10.0.0.9"

        await authStore.save(profile: authProfile)

        try await waitUntilStoreState { authStore.state == .authFailed }
        XCTAssertEqual(auth.registry.profile(id: authProfile.id)?.host, "10.0.0.2")
        XCTAssertEqual(auth.registry.profile(id: authProfile.id)?.passwordState, .invalid)

        let unsupported = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "unsupported_device", message: "unsupported")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let unsupportedProfile = try await unsupported.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try unsupported.passwordStore.save("pw", for: unsupportedProfile.keychainAccount)
        let unsupportedStore = DeviceProfileEditorStore(profile: unsupportedProfile, appStore: unsupported.appStore)
        unsupportedStore.draft.host = "10.0.0.9"

        await unsupportedStore.save(profile: unsupportedProfile)

        try await waitUntilStoreState { unsupportedStore.state == .unsupported }
        XCTAssertEqual(unsupported.registry.profile(id: unsupportedProfile.id)?.host, "10.0.0.2")
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
