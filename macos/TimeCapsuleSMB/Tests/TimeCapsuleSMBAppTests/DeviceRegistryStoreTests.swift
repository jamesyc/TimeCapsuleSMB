import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceRegistryStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeviceRegistryState.allCases, [.idle, .loading, .empty, .loaded, .saving, .failed])
    }

    func testMissingRegistryStartsEmpty() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.devicesDirectoryURL.path))
    }

    func testCorruptRegistryEntersFailedStateWithoutDeletingFile() async throws {
        let temp = try TemporaryDirectory()
        let registryURL = temp.url.appendingPathComponent("devices.json")
        try "{ not json".write(to: registryURL, atomically: true, encoding: .utf8)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        XCTAssertEqual(store.state, .failed)
        XCTAssertNotNil(store.error)
        XCTAssertTrue(FileManager.default.fileExists(atPath: registryURL.path))
        XCTAssertEqual(try String(contentsOf: registryURL), "{ not json")
    }

    func testLegacyStoredPathAndKeychainAccountAreDerivedAfterLoad() async throws {
        let temp = try TemporaryDirectory()
        let registryURL = temp.url.appendingPathComponent("devices.json")
        try """
        [
          {
            "id": "device-one",
            "displayName": "Office",
            "network": {
              "configuredSSHTarget": "10.0.0.2",
              "addresses": []
            },
            "configPath": "/legacy/path/.env",
            "keychainAccount": "legacy-account",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T00:00:00Z",
            "settings": {},
            "passwordState": "available"
          }
        ]
        """.write(to: registryURL, atomically: true, encoding: .utf8)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        let profile = try XCTUnwrap(store.profiles.first)
        XCTAssertEqual(profile.configPath, temp.url.appendingPathComponent("Devices/device-one/.env").path)
        XCTAssertEqual(profile.keychainAccount, "device-one")
        _ = try await store.updateProfile(profile)
        let persistedJSON = try String(contentsOf: registryURL)
        XCTAssertFalse(persistedJSON.contains("\"configPath\""))
        XCTAssertFalse(persistedJSON.contains("\"keychainAccount\""))
    }

    func testCreateUpdateAndDeleteProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()

        var profile = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        XCTAssertEqual(store.state, .loaded)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(profile.configPath, temp.url.appendingPathComponent("Devices/device-one/.env").path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: URL(fileURLWithPath: profile.configPath).deletingLastPathComponent().path))
        let persistedJSON = try String(contentsOf: store.registryURL)
        XCTAssertFalse(persistedJSON.contains("\"configPath\""))
        XCTAssertFalse(persistedJSON.contains("\"keychainAccount\""))

        profile.displayName = "Renamed Capsule"
        profile.settings.debugLogging = true
        let updated = try await store.updateProfile(profile)
        XCTAssertEqual(updated.displayName, "Renamed Capsule")
        XCTAssertEqual(store.profiles.first?.settings.debugLogging, true)

        try await store.delete(updated)
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertFalse(FileManager.default.fileExists(atPath: URL(fileURLWithPath: updated.configPath).deletingLastPathComponent().path))
        let stagingURL = temp.url.appendingPathComponent("Devices/.Staging", isDirectory: true)
        let stagedArtifacts = (try? FileManager.default.contentsOfDirectory(atPath: stagingURL.path)) ?? []
        XCTAssertEqual(stagedArtifacts, [])
    }

    func testDuplicateSaveUpdatesByHostAndBonjourFullnameButNotWeakMetadata() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()

        let first = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "tcapsule.local.", model: "Time Capsule"),
            discoveredDevice: try discovered(record: testDeviceRecord(fullname: "Office._airport._tcp.local.")),
            passwordState: .available,
            preferredID: "device-one"
        )
        let hostDuplicate = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: " TCAPSULE.LOCAL. ", model: "Updated Model"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-two"
        )
        XCTAssertEqual(hostDuplicate.id, first.id)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(store.profiles.first?.model, "Updated Model")

        let fullnameDuplicate = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.9"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "other.local.",
                ipv4: ["10.0.0.9"],
                fullname: " office._AIRPORT._tcp.local. "
            )),
            passwordState: .available,
            preferredID: "device-three"
        )
        XCTAssertEqual(fullnameDuplicate.id, first.id)
        XCTAssertEqual(store.profiles.count, 1)

        let addressDuplicate = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "other.local."),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "other.local.",
                ipv4: ["10.0.0.2"],
                fullname: "Other._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-address"
        )
        XCTAssertEqual(addressDuplicate.id, first.id)
        XCTAssertEqual(store.profiles.count, 1)

        _ = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.10", syap: "119", model: "Updated Model"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-four"
        )
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testConcurrentDuplicateSavesAreSerializedThroughRepository() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()

        async let first = store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "Original Capsule"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        async let second = store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: " 10.0.0.2 ", model: "Updated Capsule"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-two"
        )

        let saved = try await [first, second]

        XCTAssertEqual(Set(saved.map(\.id)).count, 1)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(store.profiles.first?.id, saved[0].id)

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let persisted = try decoder.decode([DeviceProfile].self, from: Data(contentsOf: store.registryURL))
        XCTAssertEqual(persisted.count, 1)
        XCTAssertEqual(persisted.first?.id, saved[0].id)
    }

    func testUpdateProfileDoesNotMergeDuplicateHostIntoAnotherProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        var conflictingUpdate = second
        conflictingUpdate.host = " root@10.0.0.2. "

        do {
            _ = try await store.updateProfile(conflictingUpdate)
            XCTFail("Expected duplicate host update to fail.")
        } catch {
            XCTAssertEqual(
                error as? DeviceRegistryError,
                .duplicateProfile(field: "host", value: "10.0.0.2", conflictingProfileID: first.id)
            )
        }
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: first.id)?.host, "10.0.0.2")
        XCTAssertEqual(store.profile(id: second.id)?.host, "10.0.0.3")
    }

    func testUpdateProfileRejectsDuplicateBonjourFullname() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(fullname: "Office._airport._tcp.local.")),
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "den.local.",
                ipv4: ["10.0.0.3"],
                fullname: "Den._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-two"
        )

        second.bonjourFullname = " office._AIRPORT._tcp.local. "

        do {
            _ = try await store.updateProfile(second)
            XCTFail("Expected duplicate Bonjour fullname update to fail.")
        } catch {
            XCTAssertEqual(
                error as? DeviceRegistryError,
                .duplicateProfile(
                    field: "Bonjour fullname",
                    value: "office._airport._tcp.local.",
                    conflictingProfileID: first.id
                )
            )
        }
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: second.id)?.bonjourFullname, "Den._airport._tcp.local.")
    }

    func testUpdateProfileIgnoresLinkLocalAddressConflicts() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        _ = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "office.local.",
                ipv4: ["10.0.0.2", "169.254.44.9"],
                fullname: "Office._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "den.local.",
                ipv4: ["10.0.0.3"],
                fullname: "Den._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-two"
        )

        second.addresses = ["169.254.44.9"]
        let updated = try await store.updateProfile(second)

        XCTAssertEqual(updated.addresses, ["169.254.44.9", "10.0.0.3"])
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testUpdateProfileRejectsRegularAddressConflicts() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        second.addresses = ["10.0.0.2"]

        do {
            _ = try await store.updateProfile(second)
            XCTFail("Expected duplicate regular address update to fail.")
        } catch {
            XCTAssertEqual(
                error as? DeviceRegistryError,
                .duplicateProfile(field: "address", value: "10.0.0.2", conflictingProfileID: first.id)
            )
        }
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testDeleteRestoresConfigDirectoryWhenRegistryPersistFails() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let profile = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try "TC_HOST=root@10.0.0.2\n".write(to: profile.configURL, atomically: true, encoding: .utf8)
        try FileManager.default.removeItem(at: store.registryURL)
        try FileManager.default.createDirectory(at: store.registryURL, withIntermediateDirectories: false)

        do {
            try await store.delete(profile)
            XCTFail("Expected registry delete failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertNotNil(store.profile(id: profile.id))
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configURL.deletingLastPathComponent().path))
        XCTAssertEqual(try String(contentsOf: profile.configURL, encoding: .utf8), "TC_HOST=root@10.0.0.2\n")
    }

    func testUpdateProfileMissingIDFailsWithoutCreatingProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        var profile = DeviceProfile.make(
            id: "missing",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: temp.url,
            date: Date(timeIntervalSince1970: 10)
        )
        profile.displayName = "Unsaved"

        do {
            _ = try await store.updateProfile(profile)
            XCTFail("Expected missing profile update to fail.")
        } catch {
            XCTAssertEqual(error as? DeviceRegistryError, .profileNotFound("missing"))
        }
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
    }

    func testUpdateProfilePreservesOtherProfilesForLocalEdits() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: {
            Date(timeIntervalSince1970: 100)
        })
        await store.load()
        var first = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        first.displayName = "Office"
        first.settings.mountWaitSeconds = 45
        let updated = try await store.updateProfile(first)

        XCTAssertEqual(updated.displayName, "Office")
        XCTAssertEqual(updated.settings.mountWaitSeconds, 45)
        XCTAssertEqual(store.profile(id: second.id), second)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testLoadMarksInProgressDeployStateInterrupted() async throws {
        let temp = try TemporaryDirectory()
        let start = Date(timeIntervalSince1970: 200)
        let interruptedAt = Date(timeIntervalSince1970: 300)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: { start })
        await store.load()
        let profile = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await store.updateDeployState(testDeployState(
            status: .deploying,
            startedAt: start,
            updatedAt: start,
            finishedAt: nil,
            stage: "read_mast",
            verified: nil,
            summary: ""
        ), for: profile.id)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url, now: { interruptedAt })
        await reloaded.load()

        let deployState = try XCTUnwrap(reloaded.profile(id: profile.id)?.lastDeployState)
        XCTAssertEqual(deployState.status, .interrupted)
        XCTAssertEqual(deployState.startedAt, start)
        XCTAssertEqual(deployState.updatedAt, interruptedAt)
        XCTAssertEqual(deployState.finishedAt, interruptedAt)
        XCTAssertEqual(deployState.stage, "read_mast")
        XCTAssertEqual(deployState.errorCode, "operation_interrupted")
        XCTAssertEqual(deployState.localizedSummary, "Deploy was interrupted before it completed.")
        let runtimeState = try XCTUnwrap(reloaded.profile(id: profile.id)?.runtimeState)
        XCTAssertEqual(runtimeState.state, .installInterrupted)
        XCTAssertEqual(runtimeState.stage, "read_mast")
        XCTAssertEqual(runtimeState.errorCode, "operation_interrupted")
        XCTAssertEqual(runtimeState.localizedSummary, "Deploy was interrupted before it completed.")
    }

    func testInterruptedRuntimeStateOverridesSuccessfulCheckupAfterReload() async throws {
        let temp = try TemporaryDirectory()
        let start = Date(timeIntervalSince1970: 200)
        let interruptedAt = Date(timeIntervalSince1970: 300)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: { start })
        await store.load()
        let profile = try await store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await store.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 3,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        ), for: profile.id)
        await store.updateDeployState(testDeployState(
            status: .deploying,
            startedAt: start,
            updatedAt: start,
            finishedAt: nil,
            stage: "read_mast",
            verified: nil,
            summary: ""
        ), for: profile.id)
        await store.updateRuntimeState(testRuntimeState(
            state: .installing,
            stage: "read_mast",
            verified: nil,
            summary: ""
        ), for: profile.id)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url, now: { interruptedAt })
        await reloaded.load()

        let reloadedProfile = try XCTUnwrap(reloaded.profile(id: profile.id))
        XCTAssertEqual(reloadedProfile.lastCheckup?.state, .passed)
        XCTAssertEqual(reloadedProfile.lastDeployState?.status, .interrupted)
        XCTAssertEqual(reloadedProfile.runtimeState?.state, .installInterrupted)
        XCTAssertEqual(DeviceStatusPolicy.status(
            for: reloadedProfile,
            passwordState: .available,
            activeOperation: nil
        ), .failed)
    }

    private func discovered(record: JSONValue) throws -> DiscoveredDevice {
        let resolved = try record.decode(BonjourResolvedServicePayload.self)
        return DiscoveredDevice(record: resolved, index: 0)
    }
}
