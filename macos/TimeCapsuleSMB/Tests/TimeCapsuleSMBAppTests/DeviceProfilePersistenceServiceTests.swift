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
        let existing = try await registry.storeTestProfile(
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

    func testConcurrentHardwareConflictPreservesWinningCredentialsAndConfig() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let passwords = InMemoryPasswordStore()
        let service = DeviceProfilePersistenceService(registry: registry, passwordStore: passwords)
        let first = try service.prepareConfigureTarget(targetHost: "10.0.0.2", discoveredDevice: nil,
            existingProfile: nil, preferredID: "one", settings: .default)
        let second = try service.prepareConfigureTarget(targetHost: "10.0.0.3", discoveredDevice: nil,
            existingProfile: nil, preferredID: "two", settings: .default)
        try writeTestConfig(to: first.context.configURL, host: "root@10.0.0.2")
        try writeTestConfig(to: second.context.configURL, host: "root@10.0.0.3")
        func commit(_ draft: ConfigureProfileDraft) async -> Result<DeviceProfile, Error> {
            do {
                return .success(try await service.commitConfiguredProfile(
                    configuredDevice: testConfiguredDevice(host: draft.targetHost, airportMAC: "02:aa:bb:cc:dd:ee"),
                    draft: draft, password: "password-\(draft.profileID)"))
            } catch { return .failure(error) }
        }
        async let one = commit(first)
        async let two = commit(second)
        let outcomes = await [one, two]
        let saved = outcomes.compactMap { try? $0.get() }
        XCTAssertEqual(saved.count, 1)
        let winner = try XCTUnwrap(saved.first)
        for outcome in outcomes {
            if case .failure(let error) = outcome {
                guard case .duplicateProfile(let field, _, let id) = error as? DeviceRegistryError else {
                    return XCTFail("Unexpected commit error: \(error)")
                }
                XCTAssertEqual(field, "AirPort MAC")
                XCTAssertEqual(id, winner.id)
            }
        }
        let loser = winner.id == first.profileID ? second : first
        XCTAssertEqual(try passwords.password(for: winner.keychainAccount), "password-\(winner.id)")
        XCTAssertEqual(try String(contentsOf: winner.configURL, encoding: .utf8), "TC_HOST=root@\(winner.host)\n")
        XCTAssertEqual(passwords.state(for: loser.profileID), .missing)
        XCTAssertFalse(FileManager.default.fileExists(atPath: DeviceProfile.configURL(for: loser.profileID,
            applicationSupportURL: temp.url).path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: first.context.configURL.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: second.context.configURL.path))
        await registry.load()
        XCTAssertEqual(registry.profiles, [winner])
    }

    func testConfiguredCommitReplacingConfigDoesNotLeaveRollbackArtifact() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let existing = try await registry.storeTestProfile(
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

    func testProfileEditCommitsStagedConfigWithSettings() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let profile = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try writeTestConfig(to: profile.configURL)
        let service = DeviceProfilePersistenceService(
            registry: registry,
            passwordStore: InMemoryPasswordStore()
        )
        let stagedConfigURL = try service.stageProfileConfig(profile)
        try "TC_HOST=root@10.0.0.2\nTC_ANY_PROTOCOL=true\n".write(
            to: stagedConfigURL,
            atomically: true,
            encoding: .utf8
        )
        var settings = profile.settings
        settings.anyProtocol = true

        let saved = try await service.saveProfileEdits(
            profile: profile,
            fields: DeviceProfileEditableFields(displayName: "Updated", settings: settings),
            stagedConfigURL: stagedConfigURL
        )

        XCTAssertEqual(saved.displayName, "Updated")
        XCTAssertTrue(saved.settings.anyProtocol)
        XCTAssertEqual(
            try String(contentsOf: saved.configURL, encoding: .utf8),
            "TC_HOST=root@10.0.0.2\nTC_ANY_PROTOCOL=true\n"
        )
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedConfigURL.path))
    }

    func testProfileEditRegistryFailureRestoresPreviousConfig() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let profile = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try writeTestConfig(to: profile.configURL)
        let service = DeviceProfilePersistenceService(
            registry: registry,
            passwordStore: InMemoryPasswordStore()
        )
        let stagedConfigURL = try service.stageProfileConfig(profile)
        try "TC_HOST=root@10.0.0.2\nTC_ANY_PROTOCOL=true\n".write(
            to: stagedConfigURL,
            atomically: true,
            encoding: .utf8
        )
        try FileManager.default.removeItem(at: registry.registryURL)
        try FileManager.default.createDirectory(at: registry.registryURL, withIntermediateDirectories: false)

        do {
            _ = try await service.saveProfileEdits(
                profile: profile,
                fields: DeviceProfileEditableFields(displayName: "Updated", settings: profile.settings),
                stagedConfigURL: stagedConfigURL
            )
            XCTFail("Expected registry update failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertEqual(
            try String(contentsOf: profile.configURL, encoding: .utf8),
            "TC_HOST=root@10.0.0.2\n"
        )
    }

    func testForgetRestoresPasswordWhenRegistryDeleteFails() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let profile = try await registry.storeTestProfile(
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
