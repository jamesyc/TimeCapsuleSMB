import XCTest
@testable import TimeCapsuleSMBApp

final class DeviceProfileMigrationsTests: XCTestCase {
    private func oldProfile() -> [String: Any] {
        [
            "settings": [
                "forceDisableSMBSigningAndEncryption": true,
                "vfsAIOForkEnabled": false,
                "anyProtocol": true,
            ] as [String: Any],
            "lastDeployState": ["summary": "Deployment completed."],
            "runtimeState": ["summary": "Copied by hand", "summaryRef": ["key": "backend.summary.fsck_failed",
                                                                           "arguments": [], "text": "x"]],
        ]
    }

    func testAnOldProfileIsBroughtToTheCurrentFormat() throws {
        var profile = oldProfile()

        XCTAssertTrue(DeviceProfileMigrations.migrate(&profile))

        XCTAssertEqual(profile["formatVersion"] as? Int, DeviceProfileMigrations.currentVersion)
        let settings = try XCTUnwrap(profile["settings"] as? [String: Any])
        XCTAssertEqual(settings["disableSMBEncryption"] as? Bool, true)
        XCTAssertNil(settings["forceDisableSMBSigningAndEncryption"])
        XCTAssertNil(settings["vfsAIOForkEnabled"])
        XCTAssertEqual(settings["anyProtocol"] as? Bool, true, "a combination the device takes is kept")
        let deploy = try XCTUnwrap(profile["lastDeployState"] as? [String: Any])
        XCTAssertEqual((deploy["summaryRef"] as? [String: Any])?["key"] as? String, "backend.summary.deploy_completed")
        // A stored key wins over the text beside it.
        let runtime = try XCTUnwrap(profile["runtimeState"] as? [String: Any])
        XCTAssertEqual((runtime["summaryRef"] as? [String: Any])?["key"] as? String, "backend.summary.fsck_failed")
    }

    func testSettingsTheDeviceRefusesAreFixed() {
        var settings: [String: Any] = ["requireSMBEncryption": true, "anyProtocol": true, "disableSMBEncryption": true]
        DeviceProfileMigrations.migrateSettings(&settings)
        XCTAssertEqual(settings["anyProtocol"] as? Bool, false)
        XCTAssertEqual(settings["disableSMBEncryption"] as? Bool, false)
        XCTAssertEqual(settings["requireSMBEncryption"] as? Bool, true)
    }

    func testAnIdleTimeTheDeviceRunsAs300IsSavedAs300() {
        for (saved, migrated) in [(100_000, 300), (86_401, 300), (86_400, 86_400), (0, 0)] {
            var settings: [String: Any] = ["ataIdleSeconds": saved]
            DeviceProfileMigrations.migrateSettings(&settings)
            XCTAssertEqual(settings["ataIdleSeconds"] as? Int, migrated, "\(saved)")
        }
    }

    func testTextWithoutAKnownKeyIsLeftAsSaved() throws {
        var profile: [String: Any] = ["lastDeployState": ["summary": "PASS 3, WARN 1, FAIL 0"]]
        XCTAssertTrue(DeviceProfileMigrations.migrate(&profile))
        let deploy = try XCTUnwrap(profile["lastDeployState"] as? [String: Any])
        XCTAssertNil(deploy["summaryRef"])
        XCTAssertEqual(deploy["summary"] as? String, "PASS 3, WARN 1, FAIL 0")
    }

    func testEachStepCanRunTwiceAndACurrentProfileIsLeftAlone() throws {
        var once = oldProfile()
        _ = DeviceProfileMigrations.migrate(&once)
        var twice = once
        XCTAssertFalse(DeviceProfileMigrations.migrate(&twice), "a migrated profile is not migrated again")
        XCTAssertEqual(NSDictionary(dictionary: twice), NSDictionary(dictionary: once))

        // An older app dropped formatVersion: migrating again gives the same profile.
        var dropped = once
        dropped.removeValue(forKey: "formatVersion")
        XCTAssertTrue(DeviceProfileMigrations.migrate(&dropped))
        XCTAssertEqual(NSDictionary(dictionary: dropped), NSDictionary(dictionary: once))

        var current: [String: Any] = ["formatVersion": DeviceProfileMigrations.currentVersion,
                                      "settings": ["forceDisableSMBSigningAndEncryption": true]]
        XCTAssertFalse(DeviceProfileMigrations.migrate(&current))
    }

    func testAFileThatIsNotAListOfProfilesIsLeftForTheDecoder() {
        for text in ["{}", "not json", "[1, 2]"] {
            let data = Data(text.utf8)
            let result = DeviceProfileMigrations.migrate(registryData: data)
            XCTAssertEqual(result.data, data, text)
            XCTAssertFalse(result.migrated, text)
        }
    }
}

@MainActor
final class DeviceProfileFormatLaunchTests: XCTestCase {
    func testAFormatNewerThanThisAppIsSavedAsThisAppsOwn() async throws {
        // A newer app's profile: this app drops what it does not know when it
        // saves, so the newer app must migrate it again.
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        var profile = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        profile.formatVersion = DeviceProfileMigrations.currentVersion + 4
        profile.settings.mdnsAdvertiseAFP = true

        let data = try JSONEncoder().encode(profile)
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(raw["formatVersion"] as? Int, DeviceProfileMigrations.currentVersion)
        let decoded = try JSONDecoder().decode(DeviceProfile.self, from: data)
        XCTAssertEqual(decoded.settings, profile.settings)
        XCTAssertEqual(decoded.displayName, profile.displayName)
    }

    func testLaunchShowsAnOldProfileMigratedWithoutRunningTheHelper() async throws {
        let temp = try TemporaryDirectory()
        let writer = DeviceRegistryStore(applicationSupportURL: temp.url)
        await writer.load()
        let saved = try await writer.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        // As an app before format versions saved it.
        var raw = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: writer.registryURL)) as? [[String: Any]])
        raw[0].removeValue(forKey: "formatVersion")
        try JSONSerialization.data(withJSONObject: raw).write(to: writer.registryURL)

        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        let runner = StoreTestRunner(responses: [])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            appSettingsStore: AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json")),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore()
        )

        await appStore.start()

        XCTAssertEqual(registry.profile(id: saved.id)?.formatVersion, DeviceProfileMigrations.currentVersion)
        XCTAssertFalse(runner.calls.contains { $0.operation == "update-config-settings" })
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: temp.url.appendingPathComponent(DeviceProfileMigrations.backupFileName).path))
    }
}
