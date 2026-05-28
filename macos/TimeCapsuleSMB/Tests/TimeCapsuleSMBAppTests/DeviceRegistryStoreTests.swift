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

    private func discovered(record: JSONValue) throws -> DiscoveredDevice {
        let resolved = try record.decode(BonjourResolvedServicePayload.self)
        return DiscoveredDevice(record: resolved, index: 0)
    }
}
