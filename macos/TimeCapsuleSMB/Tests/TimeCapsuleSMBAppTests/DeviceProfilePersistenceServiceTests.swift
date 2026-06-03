import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceProfilePersistenceServiceTests: XCTestCase {
    func testKeychainFailureDoesNotPersistProfile() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let passwordStore = InMemoryPasswordStore()
        passwordStore.saveFailure = .save
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let draft = try service.prepareConfigureTarget(
            targetHost: "10.0.0.2",
            discoveredDevice: nil,
            existingProfile: nil,
            preferredID: "device-one",
            settings: .default
        )
        try writeTestConfig(to: draft.context.configURL)

        do {
            _ = try await service.commitConfiguredProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
                draft: draft,
                password: "secret",
                overrides: .empty
            )
            XCTFail("Expected keychain save failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertEqual(registry.profiles, [])
        XCTAssertEqual(passwordStore.state(for: "device-one"), .missing)
    }

    func testRegistryFailureRollsBackNewKeychainPassword() async throws {
        let temp = try TemporaryDirectory()
        let blockedApplicationSupport = temp.url.appendingPathComponent("not-a-directory")
        try "file".write(to: blockedApplicationSupport, atomically: true, encoding: .utf8)
        let registry = DeviceRegistryStore(applicationSupportURL: blockedApplicationSupport)
        let passwordStore = InMemoryPasswordStore()
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let draft = ConfigureProfileDraft(
            profileID: "device-one",
            existingProfileID: nil,
            discoveredDevice: nil,
            targetHost: "10.0.0.2",
            settings: .default,
            context: DeviceRuntimeContext(
                profileID: "device-one",
                configURL: temp.url.appendingPathComponent("staged.env")
            )
        )
        try writeTestConfig(to: draft.context.configURL)

        do {
            _ = try await service.commitConfiguredProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
                draft: draft,
                password: "secret",
                overrides: .empty
            )
            XCTFail("Expected registry save failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertEqual(registry.profiles, [])
        XCTAssertEqual(passwordStore.state(for: "device-one"), .missing)
    }

    func testRegistryFailureRestoresExistingKeychainPassword() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let existing = try await registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let passwordStore = InMemoryPasswordStore(passwords: [existing.keychainAccount: "old-secret"])
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let draft = try service.prepareConfigureTarget(
            targetHost: "10.0.0.2",
            discoveredDevice: nil,
            existingProfile: existing,
            preferredID: existing.id,
            settings: existing.settings
        )
        try writeTestConfig(to: draft.context.configURL, host: "root@10.0.0.2")
        let blockedRegistryPath = registry.registryURL
        try FileManager.default.removeItem(at: blockedRegistryPath)
        try FileManager.default.createDirectory(at: blockedRegistryPath, withIntermediateDirectories: false)

        do {
            _ = try await service.commitConfiguredProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "Updated Capsule"),
                draft: draft,
                password: "new-secret",
                overrides: .empty
            )
            XCTFail("Expected registry save failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertEqual(try passwordStore.password(for: existing.keychainAccount), "old-secret")
        XCTAssertEqual(registry.profile(id: existing.id)?.model, existing.model)
    }

    func testConfiguredCommitMovesStagedConfigToFinalPath() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let passwordStore = InMemoryPasswordStore()
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let draft = try service.prepareConfigureTarget(
            targetHost: "10.0.0.2",
            discoveredDevice: nil,
            existingProfile: nil,
            preferredID: "device-one",
            settings: .default
        )
        try writeTestConfig(to: draft.context.configURL)

        let profile = try await service.commitConfiguredProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            draft: draft,
            password: "secret"
        )

        XCTAssertFalse(FileManager.default.fileExists(atPath: draft.context.configURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configPath))
        XCTAssertEqual(try passwordStore.password(for: profile.keychainAccount), "secret")
    }

    func testConfiguredCommitReplacingConfigDoesNotLeaveRollbackArtifact() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let existing = try await registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try writeTestConfig(to: existing.configURL, host: "root@10.0.0.2")
        let passwordStore = InMemoryPasswordStore(passwords: [existing.keychainAccount: "old-secret"])
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let draft = try service.prepareConfigureTarget(
            targetHost: "10.0.0.3",
            discoveredDevice: nil,
            existingProfile: existing,
            preferredID: existing.id,
            settings: existing.settings
        )
        try writeTestConfig(to: draft.context.configURL, host: "root@10.0.0.3")

        let profile = try await service.commitConfiguredProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3", model: "Updated Capsule"),
            draft: draft,
            password: "new-secret"
        )

        let stagingURL = temp.url
            .appendingPathComponent("Devices", isDirectory: true)
            .appendingPathComponent(".Staging", isDirectory: true)
        let stagedArtifacts = (try? FileManager.default.contentsOfDirectory(atPath: stagingURL.path)) ?? []
        XCTAssertEqual(stagedArtifacts, [])
        XCTAssertEqual(try String(contentsOf: profile.configURL, encoding: .utf8), "TC_HOST=root@10.0.0.3\n")
        XCTAssertEqual(try passwordStore.password(for: profile.keychainAccount), "new-secret")
    }

    func testForgetRestoresPasswordWhenRegistryDeleteFails() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let profile = try await registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let passwordStore = InMemoryPasswordStore(passwords: [profile.keychainAccount: "old-secret"])
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        try FileManager.default.removeItem(at: registry.registryURL)
        try FileManager.default.createDirectory(at: registry.registryURL, withIntermediateDirectories: false)

        do {
            try await service.forget(profile)
            XCTFail("Expected registry delete failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertNotNil(registry.profile(id: profile.id))
        XCTAssertEqual(try passwordStore.password(for: profile.keychainAccount), "old-secret")
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configURL.deletingLastPathComponent().path))
    }

    private func writeTestConfig(to url: URL, host: String = "root@10.0.0.2") throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "TC_HOST=\(host)\n".write(to: url, atomically: true, encoding: .utf8)
    }
}
