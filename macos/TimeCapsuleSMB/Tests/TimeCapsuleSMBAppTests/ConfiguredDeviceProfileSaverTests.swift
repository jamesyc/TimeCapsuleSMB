import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ConfiguredDeviceProfileSaverTests: XCTestCase {
    func testKeychainFailureDoesNotPersistProfile() throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        registry.load()
        let passwordStore = InMemoryPasswordStore()
        passwordStore.saveFailure = .save
        let saver = ConfiguredDeviceProfileSaver(registry: registry, passwordStore: passwordStore)

        XCTAssertThrowsError(try saver.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            password: "secret",
            preferredID: "device-one"
        ))

        XCTAssertEqual(registry.profiles, [])
        XCTAssertEqual(passwordStore.state(for: "device-one"), .missing)
    }

    func testRegistryFailureRollsBackNewKeychainPassword() throws {
        let temp = try TemporaryDirectory()
        let blockedApplicationSupport = temp.url.appendingPathComponent("not-a-directory")
        try "file".write(to: blockedApplicationSupport, atomically: true, encoding: .utf8)
        let registry = DeviceRegistryStore(applicationSupportURL: blockedApplicationSupport)
        let passwordStore = InMemoryPasswordStore()
        let saver = ConfiguredDeviceProfileSaver(registry: registry, passwordStore: passwordStore)

        XCTAssertThrowsError(try saver.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            password: "secret",
            preferredID: "device-one"
        ))

        XCTAssertEqual(registry.profiles, [])
        XCTAssertEqual(passwordStore.state(for: "device-one"), .missing)
    }

    func testRegistryFailureRestoresExistingKeychainPassword() throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        registry.load()
        let existing = try registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let passwordStore = InMemoryPasswordStore(passwords: [existing.keychainAccount: "old-secret"])
        let saver = ConfiguredDeviceProfileSaver(registry: registry, passwordStore: passwordStore)
        let blockedRegistryPath = registry.registryURL
        try FileManager.default.removeItem(at: blockedRegistryPath)
        try FileManager.default.createDirectory(at: blockedRegistryPath, withIntermediateDirectories: false)

        XCTAssertThrowsError(try saver.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "Updated Capsule"),
            discoveredDevice: nil,
            password: "new-secret",
            preferredID: "device-one"
        ))

        XCTAssertEqual(try passwordStore.password(for: existing.keychainAccount), "old-secret")
        XCTAssertEqual(registry.profile(id: existing.id)?.model, existing.model)
    }
}
