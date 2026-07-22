import Combine
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
            "ataIdleSecondsInvalid",
            "ataStandbyInvalid",
            "passwordRequired"
        ])
    }

    func testIntegerSettingValidationAcceptsZeroAndPositiveIntegersOnly() throws {
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

        draft.mountWaitSeconds = "45"
        draft.ataIdleSeconds = "0"
        XCTAssertEqual(try draft.validatedSettings().ataIdleSeconds, 0)
        draft.ataIdleSeconds = "300"
        XCTAssertEqual(try draft.validatedSettings().ataIdleSeconds, 300)
        for invalid in ["", "-1", "1.5", "abc"] {
            draft.ataIdleSeconds = invalid
            XCTAssertThrowsError(try draft.validatedSettings()) { error in
                XCTAssertEqual(error as? DeviceProfileEditorValidationError, .ataIdleSecondsInvalid)
            }
        }

        draft.ataIdleSeconds = "300"
        draft.ataStandby = ""
        XCTAssertNil(try draft.validatedSettings().ataStandby)
        draft.ataStandby = "0"
        XCTAssertEqual(try draft.validatedSettings().ataStandby, 0)
        for invalid in ["-1", "1.5", "abc"] {
            draft.ataStandby = invalid
            XCTAssertThrowsError(try draft.validatedSettings()) { error in
                XCTAssertEqual(error as? DeviceProfileEditorValidationError, .ataStandbyInvalid)
            }
        }
    }

    func testUndoingDraftChangeReturnsEditorToCleanState() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        XCTAssertEqual(store.state, .clean)
        XCTAssertFalse(store.canSave)

        store.draft.nbnsEnabled.toggle()

        XCTAssertEqual(store.state, .dirty)
        XCTAssertTrue(store.canSave)

        store.draft.nbnsEnabled.toggle()

        XCTAssertEqual(store.state, .clean)
        XCTAssertFalse(store.canSave)
    }

    func testCleanEditorSyncsToUpdatedProfileBaseline() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)
        var updatedProfile = profile
        updatedProfile.displayName = "Renamed Capsule"

        store.sync(to: updatedProfile)

        XCTAssertEqual(store.draft.displayName, "Renamed Capsule")
        XCTAssertEqual(store.state, .clean)
        XCTAssertFalse(store.canSave)
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
        store.draft.internalShareUseDiskRoot = true
        store.draft.smbBrowseCompatibility = true
        store.draft.mdnsAdvertiseAFP = true
        store.draft.anyProtocol = true
        store.draft.forceDisableSMBSigningAndEncryption = true
        store.draft.fruitMetadataNetatalk = true
        store.draft.debugLogging = true
        store.draft.mountWaitSeconds = "45"
        store.draft.ataIdleSeconds = "0"
        store.draft.ataStandby = "0"

        await store.save(profile: profile)

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(store.state, .saved)
        XCTAssertEqual(saved.displayName, "Media Capsule")
        XCTAssertEqual(saved.host, "root@10.0.0.2")
        XCTAssertEqual(saved.settings, DeviceProfileSettings(
            nbnsEnabled: false,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            mdnsAdvertiseAFP: true,
            anyProtocol: true,
            forceDisableSMBSigningAndEncryption: true,
            fruitMetadataNetatalk: true,
            debugLogging: true,
            mountWaitSeconds: 45,
            ataIdleSeconds: 0,
            ataStandby: 0
        ))
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testEquivalentHostEditDoesNotRunBackendConfigure() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.host = " 10.0.0.2 "
        store.draft.displayName = "Media Capsule"

        await store.save(profile: profile)

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(store.state, .saved)
        XCTAssertEqual(saved.host, "root@10.0.0.2")
        XCTAssertEqual(saved.displayName, "Media Capsule")
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testPasswordOnlySaveUpdatesKeychainAndClearsDraft() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        XCTAssertFalse(store.canSave)
        store.replacementPassword = "   "
        XCTAssertFalse(store.canSave)
        XCTAssertEqual(store.state, .clean)

        store.replacementPassword = "new-password"
        XCTAssertTrue(store.canSave)

        await store.save(profile: profile)

        XCTAssertEqual(store.state, .saved)
        XCTAssertEqual(store.replacementPassword, "")
        XCTAssertNil(store.passwordError)
        XCTAssertEqual(try fixture.passwordStore.password(for: profile.keychainAccount), "new-password")
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .available)
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testPasswordSaveFailureKeepsDraftAndDoesNotMarkAvailable() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        fixture.passwordStore.saveFailure = .save
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)
        store.replacementPassword = "new-password"

        await store.save(profile: profile)

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.replacementPassword, "new-password")
        XCTAssertEqual(store.passwordError, "In-memory password store save failed.")
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .missing)
    }

    func testResetClearsPendingProfileAndPasswordChanges() async throws {
        let fixture = try await makeFixture(responses: [])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.nbnsEnabled.toggle()
        store.replacementPassword = "new-password"
        XCTAssertTrue(store.canSave)

        store.reset(to: profile)

        XCTAssertEqual(store.state, .clean)
        XCTAssertFalse(store.canSave)
        XCTAssertEqual(store.replacementPassword, "")
        XCTAssertNil(store.passwordError)
        XCTAssertEqual(store.draft, DeviceProfileEditorDraft(profile: profile))
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
        store.draft.ataIdleSeconds = "also-bad"
        store.draft.ataStandby = "still-bad"
        await store.save(profile: first)
        XCTAssertEqual(store.state, .invalid)
        XCTAssertEqual(store.validationErrors, [.mountWaitInvalid, .ataIdleSecondsInvalid, .ataStandbyInvalid])
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

    func testChangedHostUsesReplacementPasswordWhenSavedPasswordIsMissing() async throws {
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
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-one"
        )
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.host = "10.0.0.9"
        store.replacementPassword = "new-password"

        await store.save(profile: profile)

        try await waitUntilStoreState { store.state == .saved }
        let call = try XCTUnwrap(fixture.runner.calls.first)
        XCTAssertEqual(call.operation, "configure")
        XCTAssertEqual(call.params["password"], .string("new-password"))
        XCTAssertEqual(store.replacementPassword, "")
        XCTAssertNil(store.passwordError)
        XCTAssertEqual(try fixture.passwordStore.password(for: profile.keychainAccount), "new-password")
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.passwordState, .available)
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
        await fixture.registry.updateDeployState(testDeployState(
            startedAt: Date(timeIntervalSince1970: 110),
            updatedAt: Date(timeIntervalSince1970: 110),
            finishedAt: Date(timeIntervalSince1970: 110)
        ), for: profile.id)
        await fixture.registry.updateRuntimeState(testRuntimeState(summary: "Install completed."), for: profile.id)
        profile = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.displayName = "Updated Capsule"
        store.draft.host = "10.0.0.9"
        store.draft.nbnsEnabled = false
        store.draft.internalShareUseDiskRoot = true
        store.draft.smbBrowseCompatibility = true
        store.draft.mdnsAdvertiseAFP = true
        store.draft.anyProtocol = true
        store.draft.forceDisableSMBSigningAndEncryption = true
        store.draft.fruitMetadataNetatalk = true
        store.draft.debugLogging = true
        store.draft.mountWaitSeconds = "60"
        store.draft.ataIdleSeconds = "0"
        store.draft.ataStandby = "0"

        await store.save(profile: profile)

        try await waitUntilStoreState { store.state == .saved }
        let call = try XCTUnwrap(fixture.runner.calls.first)
        XCTAssertEqual(call.operation, "configure")
        XCTAssertEqual(call.context?.profileID, profile.id)
        guard case .string(let stagedConfigPath)? = call.params["config"] else {
            return XCTFail("Expected staged config path.")
        }
        XCTAssertNotEqual(stagedConfigPath, profile.configPath)
        XCTAssertTrue(stagedConfigPath.contains("/.Staging/"))
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configPath))
        XCTAssertEqual(call.params["host"], .string("root@10.0.0.9"))
        XCTAssertEqual(call.params["password"], .string("pw"))
        XCTAssertEqual(call.params["persist_password"], .bool(false))
        XCTAssertEqual(call.params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(call.params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(call.params["mdns_advertise_afp"], .bool(true))
        XCTAssertEqual(call.params["any_protocol"], .bool(true))
        XCTAssertEqual(call.params["force_disable_smb_signing_and_encryption"], .bool(true))
        XCTAssertEqual(call.params["fruit_metadata_netatalk"], .bool(true))
        XCTAssertEqual(call.params["debug_logging"], .bool(true))
        XCTAssertEqual(call.params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(call.params["ata_standby"], .number(0))

        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(saved.id, profile.id)
        XCTAssertEqual(saved.keychainAccount, profile.keychainAccount)
        XCTAssertEqual(saved.displayName, "Updated Capsule")
        XCTAssertEqual(saved.host, "root@10.0.0.9")
        XCTAssertEqual(saved.lastCheckup?.state, .passed)
        XCTAssertEqual(saved.lastDeployState?.status, .succeeded)
        XCTAssertEqual(saved.runtimeState?.state, .installedVerified)
        XCTAssertEqual(saved.settings, DeviceProfileSettings(
            nbnsEnabled: false,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            mdnsAdvertiseAFP: true,
            anyProtocol: true,
            forceDisableSMBSigningAndEncryption: true,
            fruitMetadataNetatalk: true,
            debugLogging: true,
            mountWaitSeconds: 60,
            ataIdleSeconds: 0,
            ataStandby: 0
        ))
    }

    func testReconfigureForwardsLocalNetworkPreflightTelemetry() async throws {
        let checker = FixedLocalNetworkPreflightChecker(status: .unknown, detail: "timeout")
        let fixture = try await makeFixture(
            responses: [
                .init(events: [
                    BackendEvent(
                        type: "result",
                        operation: "configure",
                        ok: true,
                        payload: testConfigurePayload(host: "root@10.0.0.9", syap: "119", model: "TimeCapsule8,119")
                    )
                ])
            ],
            localNetworkPreflightChecker: checker
        )
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)

        store.draft.host = "10.0.0.9"

        await store.save(profile: profile)

        try await waitUntilStoreState { fixture.runner.calls.count == 1 }
        XCTAssertEqual(checker.checkCount, 1)
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_result"], .string("unknown"))
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_error"], .string("timeout"))
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

        try await waitUntilStoreState {
            authStore.state == .authFailed &&
            auth.registry.profile(id: authProfile.id)?.passwordState == .invalid
        }
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

    func testReconfigureUsesCurrentDiscoveredRecordWhenHostChangesToNewBonjourAddress() async throws {
        let oldRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let currentRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.80"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [currentRecord]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.80"))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try DiscoveredDevice(record: oldRecord.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.deviceDiscovery.refresh()
        try await waitUntilStoreState { fixture.deviceDiscovery.state == .ready }
        let store = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)
        store.draft.host = "10.0.0.80"

        await store.save(profile: profile)

        try await waitUntilStoreState { store.state == .saved }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["discover", "configure"])
        XCTAssertEqual(fixture.runner.calls[1].params["selected_record"], currentRecord)
        XCTAssertNil(fixture.runner.calls[1].params["host"])
    }

    func testDashboardUpdateConfiguredAddressRunsConfigureWithCurrentBonjourRecord() async throws {
        let oldRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let currentRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.80"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [currentRecord]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.80"))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try DiscoveredDevice(record: oldRecord.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.deviceDiscovery.refresh()
        try await waitUntilStoreState { fixture.deviceDiscovery.state == .ready }
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)
        for tab in DeviceDashboardTab.allCases {
            session.selectedTab = tab
            XCTAssertNotNil(session.staleEndpointNotice(for: profile))
        }

        session.updateConfiguredAddressFromDiscovery(profile: profile)

        try await waitUntilStoreState { session.profileEditorStore.state == .saved }
        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(session.selectedTab, .settings)
        XCTAssertEqual(saved.host, "root@10.0.0.80")
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["discover", "configure"])
        XCTAssertEqual(fixture.runner.calls[1].params["selected_record"], currentRecord)
        XCTAssertNil(fixture.runner.calls[1].params["host"])
        for tab in DeviceDashboardTab.allCases {
            session.selectedTab = tab
            XCTAssertNil(session.staleEndpointNotice(for: profile))
            XCTAssertNil(session.staleEndpointNotice(for: saved))
        }
    }

    func testDashboardUpdateConfiguredAddressRunsConfigureWithCurrentIPv6BonjourRecord() async throws {
        let oldRecord = testDeviceRecord(
            name: "IPv6 Capsule",
            hostname: "ipv6-capsule.local.",
            ipv4: [],
            ipv6: ["fd00::2"],
            fullname: "IPv6 Capsule._airport._tcp.local."
        )
        let currentRecord = testDeviceRecord(
            name: "IPv6 Capsule",
            hostname: "ipv6-capsule.local.",
            ipv4: [],
            ipv6: ["fd00::80"],
            fullname: "IPv6 Capsule._airport._tcp.local."
        )
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [currentRecord]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@fd00::80"))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "root@fd00::2"),
            discoveredDevice: try DiscoveredDevice(record: oldRecord.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "device-one"
        )
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        fixture.deviceDiscovery.refresh()
        try await waitUntilStoreState { fixture.deviceDiscovery.state == .ready }
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

        session.updateConfiguredAddressFromDiscovery(profile: profile)

        try await waitUntilStoreState { session.profileEditorStore.state == .saved }
        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(session.selectedTab, .settings)
        XCTAssertEqual(saved.host, "root@fd00::80")
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["discover", "configure"])
        XCTAssertEqual(fixture.runner.calls[1].params["selected_record"], currentRecord)
        XCTAssertNil(fixture.runner.calls[1].params["host"])
        XCTAssertNil(fixture.deviceDiscovery.staleEndpointNotice(for: saved))
    }

    func testDashboardSessionPublishesWhenDiscoveryChanges() async throws {
        let oldRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let currentRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.80"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [currentRecord]))
            ])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try DiscoveredDevice(record: oldRecord.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "device-one"
        )
        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)
        var didPublish = false
        let cancellable = session.objectWillChange.sink {
            didPublish = true
        }

        fixture.deviceDiscovery.refresh()

        try await waitUntilStoreState { fixture.deviceDiscovery.state == .ready }
        XCTAssertTrue(didPublish)
        XCTAssertNotNil(session.staleEndpointNotice(for: profile))
        withExtendedLifetime(cancellable) {}
    }

    private func makeFixture(
        responses: [StoreTestRunner.Response],
        localNetworkPreflightChecker: LocalNetworkPreflightChecking? = nil
    ) async throws -> (
        appStore: AppStore,
        registry: DeviceRegistryStore,
        passwordStore: InMemoryPasswordStore,
        runner: StoreTestRunner,
        deviceDiscovery: DeviceDiscoveryStore
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = StoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let passwordStore = InMemoryPasswordStore()
        let deviceDiscovery = DeviceDiscoveryStore(coordinator: coordinator, registry: registry)
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: passwordStore,
            deviceDiscovery: deviceDiscovery,
            localNetworkPreflightChecker: localNetworkPreflightChecker
        )
        return (appStore, registry, passwordStore, runner, deviceDiscovery)
    }
}
