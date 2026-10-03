import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ApplianceIdentityTests: XCTestCase {
    private let mac = "02:aa:bb:cc:dd:ee"
    private let peerMAC = "02:aa:bb:cc:dd:ff"

    func testCheckupsCannotPersistDuplicateHardwareIdentities() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let first = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "192.0.2.10"),
            discoveredDevice: nil, passwordState: .available, preferredID: "ipv4")
        let second = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "fd00::10"),
            discoveredDevice: nil, passwordState: .available, preferredID: "ipv6")
        let snapshot = DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: 1000), state: .passed, passCount: 1, warnCount: 0, failCount: 0, summary: "passed")
        await registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: mac, for: first.id)
        let before = registry.profiles
        let runtime = DeviceRuntimeStateSnapshot(state: .installedVerified, source: .doctor, stage: nil,
            payloadFamily: second.payloadFamily, verified: true, summary: "verified",
            errorCode: nil, errorMessage: nil, recovery: nil)
        await registry.updateCheckup(snapshot, runtimeState: runtime, airportMAC: mac, for: second.id)
        XCTAssertEqual(registry.profiles, before)
        XCTAssertNotNil(registry.error)
        await registry.load()
        XCTAssertEqual(registry.profiles, before)
        let identity = DeviceNetworkIdentity(configuredSSHTarget: "192.0.2.10", airportMAC: mac)
        XCTAssertEqual(DeviceProfileMatch.resolve(identity, in: registry.profiles).profile?.id, first.id)
    }

    func testCheckupWithoutUsableMACPreservesConfirmedIdentityAndUpdatesSnapshot() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "192.0.2.10", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "known")
        for (index, observed) in [nil, "invalid", "00:00:00:00:00:00", "01:aa:bb:cc:dd:ee", mac].enumerated() {
            let snapshot = DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: Double(1001 + index)),
                state: .passed, passCount: 1, warnCount: 0, failCount: 0, summary: "passed")
            await registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: observed, for: profile.id)
            XCTAssertEqual(registry.profile(id: profile.id)?.network.airportMAC, mac)
            XCTAssertEqual(registry.profile(id: profile.id)?.lastCheckup, snapshot)
            XCTAssertNil(registry.error)
        }
        let before = registry.profiles
        await registry.load()
        XCTAssertEqual(registry.profiles, before)
    }

    func testConflictRecoveryUsesLocalizedActionLabelsAndPreservesUserNames() {
        let previousLanguage = L10n.currentLanguage
        defer { L10n.apply(language: previousLanguage) }
        let title = "Café Capsule %@"
        for language in AppLanguage.allCases where language != .system {
            L10n.apply(language: language)
            let guidance = L10n.format("discovery.identity_owned", title)
            XCTAssertTrue(guidance.contains(title), "\(language)")
            XCTAssertTrue(guidance.contains(L10n.string("toolbar.forget")), "\(language)")
            XCTAssertTrue(L10n.string("discovery.identity_conflict").contains(L10n.string("toolbar.forget")), "\(language)")
            let action = L10n.format("discovery.open_saved_device", title)
            XCTAssertTrue(action.contains(title), "\(language)")
            XCTAssertNotEqual(action, "discovery.open_saved_device")
        }
    }

    func testConcurrentCheckupsClaimHardwareIdentityOnce() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        for (id, host) in [("one", "192.0.2.10"), ("two", "fd00::10")] {
            _ = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: host),
                discoveredDevice: nil, passwordState: .available, preferredID: id)
        }
        let snapshot = DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: 1000), state: .passed, passCount: 1, warnCount: 0, failCount: 0, summary: "passed")
        async let one: Void = registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: mac, for: "one")
        async let two: Void = registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: mac, for: "two")
        _ = await (one, two)
        await registry.load()
        XCTAssertEqual(registry.profiles.filter { $0.network.airportMAC == mac }.count, 1)
        XCTAssertEqual(registry.profiles.filter { $0.lastCheckup != nil }.count, 1)
    }

    private func device(host: String, mac: String?) throws -> DiscoveredDevice {
        let record = testDeviceRecord(hostname: "shared.local", ipv4: [host],
            fullname: "Shared._airport._tcp.local.", airportMAC: mac)
        return DiscoveredDevice(record: try record.decode(BonjourResolvedServicePayload.self), index: 0)
    }

    func testNormalizesAppleMACAndRejectsUnusableValues() {
        XCTAssertEqual(DeviceNetworkIdentity.normalizedAirportMAC(" 02-AA-Bb-CC-DD-EE "), mac)
        for value in ["", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:aa:bb:cc:dd:ee", "02:a:bb:cc:dd:ee", "02:aa:bb:cc:dd", "+2:aa:bb:cc:dd:ee"] {
            XCTAssertNil(DeviceNetworkIdentity.normalizedAirportMAC(value), value)
        }
    }

    func testDHCPChangeKeepsProfileUUIDAndReplacesOldObservations() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let first = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
            discoveredDevice: device(host: "10.0.0.2", mac: mac), passwordState: .available, preferredID: "first")
        let current = try device(host: "10.0.0.3", mac: "02-AA-BB-CC-DD-EE")
        XCTAssertEqual(registry.profileMatch(for: current), .unique(first))
        let updated = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3", airportMAC: mac),
            discoveredDevice: current, passwordState: .available, preferredID: "unused", existingProfileID: first.id)
        XCTAssertEqual(updated.id, first.id)
        XCTAssertEqual(updated.configPath, first.configPath)
        XCTAssertEqual(updated.keychainAccount, first.keychainAccount)
        XCTAssertEqual(updated.addresses, ["10.0.0.3"])
        await registry.load()
        XCTAssertEqual(registry.profiles, [updated])
    }

    func testDifferentMACOverridesIdenticalNamesAndReusedIP() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let first = try await registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
            discoveredDevice: device(host: "10.0.0.2", mac: mac), passwordState: .available, preferredID: "first")
        let peer = try device(host: "10.0.0.2", mac: peerMAC)
        XCTAssertEqual(registry.profileMatch(for: peer), .none)
        XCTAssertNil(registry.suggestedProfile(for: peer))
        do {
            _ = try await registry.storeTestProfile(
                configuredDevice: testConfiguredDevice(host: peer.host, airportMAC: peerMAC),
                discoveredDevice: peer, passwordState: .available, preferredID: "peer")
            XCTFail("A reused endpoint must not replace the original profile")
        } catch {
            XCTAssertTrue(error is DeviceRegistryError)
        }
        XCTAssertEqual(registry.profiles, [first])
    }

    func testHardwareMatchWinsOverAnOldAddressAndMultipleHardwareMatchesConflict() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let first = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "first")
        var legacy = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil, passwordState: .available, preferredID: "legacy")
        let observed = DeviceNetworkIdentity(configuredSSHTarget: "10.0.0.3", airportMAC: mac)
        XCTAssertEqual(DeviceProfileMatch.resolve(observed, in: [first, legacy]), .unique(first))
        legacy.network.airportMAC = mac
        XCTAssertEqual(DeviceProfileMatch.resolve(observed, in: [first, legacy]), .conflict(["first", "legacy"]))
    }

    func testAdvertisedIdentityIsNotPersistedWithoutConfirmation() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: device(host: "10.0.0.2", mac: mac), passwordState: .available, preferredID: "legacy")
        XCTAssertNil(profile.network.airportMAC)
        await registry.updateCheckup(DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: 1000), state: .passed, passCount: 1, warnCount: 0, failCount: 0, summary: "passed"), runtimeState: nil, airportMAC: mac, for: profile.id)
        XCTAssertEqual(registry.profile(id: profile.id)?.network.airportMAC, mac)
        let confirmed = registry.profiles
        await registry.updateCheckup(DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: 1000), state: .passed, passCount: 1, warnCount: 0, failCount: 0, summary: "passed"), runtimeState: nil, airportMAC: peerMAC, for: profile.id)
        XCTAssertEqual(registry.profiles, confirmed)
        XCTAssertNotNil(registry.error)
        await registry.load()
        XCTAssertEqual(registry.profiles, confirmed)
    }

    func testUnconfirmedOrDifferentIdentityCannotReplaceCredentialsOrConfig() async throws {
        for confirmed in [nil, peerMAC] {
            let temp = try TemporaryDirectory()
            let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
            await registry.load()
            let profile = try await registry.storeTestProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
                discoveredDevice: nil, passwordState: .available, preferredID: "first")
            let passwords = InMemoryPasswordStore()
            try passwords.save("original", for: profile.keychainAccount)
            try Data("original config".utf8).write(to: profile.configURL)
            let persistence = DeviceProfilePersistenceService(registry: registry, passwordStore: passwords)
            let draft = try persistence.prepareConfigureTarget(targetHost: "10.0.0.3", discoveredDevice: nil,
                existingProfile: profile, preferredID: profile.id, settings: .default)
            try Data("replacement config".utf8).write(to: draft.context.configURL)
            do {
                _ = try await persistence.commitConfiguredProfile(
                    configuredDevice: testConfiguredDevice(host: "10.0.0.3", airportMAC: confirmed),
                    draft: draft, password: "replacement")
                XCTFail("Expected identity verification to reject this replacement")
            } catch {
                XCTAssertTrue(error is DeviceRegistryError)
            }
            XCTAssertEqual(try passwords.password(for: profile.keychainAccount), "original")
            XCTAssertEqual(try Data(contentsOf: profile.configURL), Data("original config".utf8))
            XCTAssertFalse(FileManager.default.fileExists(atPath: draft.context.configURL.path))
            XCTAssertEqual(registry.profiles, [profile])
        }
    }

    func testHistoricalAddressCannotAssociateAnUnrelatedDevice() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        var original = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil, passwordState: .available, preferredID: "original")
        original.network.setAddressValues(original.network.addressValues + ["10.0.0.9"])
        original = try await registry.updateProfile(original)
        let peer = try device(host: "10.0.0.9", mac: nil)
        XCTAssertEqual(registry.profileMatch(for: peer), .none)
        let saved = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: peer.host),
            discoveredDevice: peer, passwordState: .available, preferredID: "peer")
        XCTAssertNotEqual(saved.id, original.id)
        XCTAssertEqual(registry.profile(id: original.id), original)
    }

    func testLoadingProfilesNormalizesStoredHardwareEvidence() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let original = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil, passwordState: .available, preferredID: "legacy")
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        for (stored, expected) in [("02-AA-BB-CC-DD-EE", mac), ("00:00:00:00:00:00", nil)] {
            var imported = original
            imported.network.airportMAC = stored
            try encoder.encode([imported]).write(to: registry.registryURL)
            await registry.load()
            XCTAssertEqual(registry.profiles.count, 1)
            XCTAssertEqual(registry.profiles[0].id, original.id)
            XCTAssertEqual(registry.profiles[0].network.airportMAC, expected)
        }
    }

    func testConcurrentSameHardwareSavesRejectDuplicateOwnership() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        let first = try await registry.makeConfiguredDeviceProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "one")
        let second = try await registry.makeConfiguredDeviceProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.3", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "two")
        func save(_ profile: DeviceProfile) async -> Result<DeviceProfile, Error> {
            do { return .success(try await registry.saveProfile(profile)) }
            catch { return .failure(error) }
        }
        async let one = save(first)
        async let two = save(second)
        let outcomes = await [one, two]
        let saved = outcomes.compactMap { try? $0.get() }
        XCTAssertEqual(saved.count, 1)
        let winner = try XCTUnwrap(saved.first)
        for outcome in outcomes {
            if case .failure(let error) = outcome {
                guard case .duplicateProfile(let field, let value, let id) = error as? DeviceRegistryError else {
                    return XCTFail("Unexpected save error: \(error)")
                }
                XCTAssertEqual(field, "AirPort MAC")
                XCTAssertEqual(value, mac)
                XCTAssertEqual(id, winner.id)
            }
        }
        await registry.load()
        XCTAssertEqual(registry.profiles, [winner])
        XCTAssertEqual(winner.network.airportMAC, mac)
    }
}
