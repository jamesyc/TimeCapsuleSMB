import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceRegistryStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeviceRegistryState.allCases, [.idle, .loading, .empty, .loaded, .saving, .failed])
    }

    func testMissingRegistryStartsEmpty() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        store.load()

        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.devicesDirectoryURL.path))
    }

    func testCorruptRegistryEntersFailedStateWithoutDeletingFile() throws {
        let temp = try TemporaryDirectory()
        let registryURL = temp.url.appendingPathComponent("devices.json")
        try "{ not json".write(to: registryURL, atomically: true, encoding: .utf8)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        store.load()

        XCTAssertEqual(store.state, .failed)
        XCTAssertNotNil(store.error)
        XCTAssertTrue(FileManager.default.fileExists(atPath: registryURL.path))
        XCTAssertEqual(try String(contentsOf: registryURL), "{ not json")
    }

    func testCreateUpdateAndDeleteProfile() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        store.load()

        var profile = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        XCTAssertEqual(store.state, .loaded)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(profile.configPath, temp.url.appendingPathComponent("Devices/device-one/.env").path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: URL(fileURLWithPath: profile.configPath).deletingLastPathComponent().path))

        profile.displayName = "Renamed Capsule"
        profile.settings.debugLogging = true
        let updated = try store.updateProfile(profile)
        XCTAssertEqual(updated.displayName, "Renamed Capsule")
        XCTAssertEqual(store.profiles.first?.settings.debugLogging, true)

        try store.delete(updated)
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertFalse(FileManager.default.fileExists(atPath: URL(fileURLWithPath: updated.configPath).deletingLastPathComponent().path))
    }

    func testDuplicateSaveUpdatesByHostAndBonjourFullnameButNotWeakMetadata() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        store.load()

        let first = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "tcapsule.local.", model: "Time Capsule"),
            discoveredDevice: try discovered(record: testDeviceRecord(fullname: "Office._airport._tcp.local.")),
            passwordState: .available,
            preferredID: "device-one"
        )
        let hostDuplicate = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: " TCAPSULE.LOCAL. ", model: "Updated Model"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-two"
        )
        XCTAssertEqual(hostDuplicate.id, first.id)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(store.profiles.first?.model, "Updated Model")

        let fullnameDuplicate = try store.saveConfiguredDevice(
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

        _ = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.10", syap: "119", model: "Updated Model"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-four"
        )
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testUpdateProfileDoesNotMergeDuplicateHostIntoAnotherProfile() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        store.load()
        let first = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        var conflictingUpdate = second
        conflictingUpdate.host = " root@10.0.0.2. "

        XCTAssertThrowsError(try store.updateProfile(conflictingUpdate)) { error in
            XCTAssertEqual(
                error as? DeviceRegistryError,
                .duplicateProfile(field: "host", value: "10.0.0.2", conflictingProfileID: first.id)
            )
        }
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: first.id)?.host, "10.0.0.2")
        XCTAssertEqual(store.profile(id: second.id)?.host, "10.0.0.3")
    }

    func testUpdateProfileRejectsDuplicateBonjourFullname() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        store.load()
        let first = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(fullname: "Office._airport._tcp.local.")),
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try store.saveConfiguredDevice(
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

        XCTAssertThrowsError(try store.updateProfile(second)) { error in
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

    func testUpdateProfileMissingIDFailsWithoutCreatingProfile() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        store.load()
        var profile = DeviceProfile.make(
            id: "missing",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: temp.url,
            date: Date(timeIntervalSince1970: 10)
        )
        profile.displayName = "Unsaved"

        XCTAssertThrowsError(try store.updateProfile(profile)) { error in
            XCTAssertEqual(error as? DeviceRegistryError, .profileNotFound("missing"))
        }
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
    }

    func testUpdateProfilePreservesOtherProfilesForLocalEdits() throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: {
            Date(timeIntervalSince1970: 100)
        })
        store.load()
        var first = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try store.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        first.displayName = "Office"
        first.settings.mountWaitSeconds = 45
        let updated = try store.updateProfile(first)

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
