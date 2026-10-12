import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ApplianceIdentityTests: XCTestCase {
    private let mac = "02:aa:bb:cc:dd:ee"
    private let peerMAC = "02:aa:bb:cc:dd:ff"

    private func snapshot(_ seconds: Double = 1000) -> DeviceCheckupSnapshot {
        DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: seconds), state: .passed, passCount: 1, warnCount: 0,
                              failCount: 0, summary: "passed")
    }

    func testAnOperationCannotGiveTwoProfilesOneMAC() async throws {
        // Two entries for one device: the registry's error offers to merge them.
        let registry = try await loadedRegistry()
        let first = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "192.0.2.10"),
            discoveredDevice: nil, passwordState: .available, preferredID: "ipv4")
        let second = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "fd00::10"),
            discoveredDevice: nil, passwordState: .available, preferredID: "ipv6")
        await registry.confirmAirportIdentity(try XCTUnwrap(AirportIdentityObservation(mac: mac)), for: first.id)
        let before = registry.profiles
        await registry.confirmAirportIdentity(try XCTUnwrap(AirportIdentityObservation(mac: mac)), for: second.id)
        XCTAssertEqual(registry.profiles, before)
        guard case .duplicateProfile(_, _, let owner)? = registry.error else { return XCTFail("\(String(describing: registry.error))") }
        XCTAssertEqual(owner, first.id)
        await registry.load()
        XCTAssertEqual(registry.profiles, before)
        let identity = DeviceNetworkIdentity(configuredSSHTarget: "192.0.2.10", airportMAC: mac)
        XCTAssertEqual(DeviceProfileMatch.resolve(identity, in: registry.profiles).profile?.id, first.id)
    }

    func testACheckupOfAnotherAirPortIsNotRecorded() async throws {
        let registry = try await loadedRegistry()
        let profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "192.0.2.10", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "known")
        // No MAC, or none that says otherwise: the checkup is this device's.
        for (index, observed) in [nil, "invalid", "00:00:00:00:00:00", "01:aa:bb:cc:dd:ee", mac].enumerated() {
            let checkup = snapshot(Double(1001 + index))
            await registry.updateCheckup(checkup, runtimeState: nil, observedMAC: observed, for: profile.id)
            XCTAssertEqual(registry.profile(id: profile.id)?.lastCheckup, checkup, String(describing: observed))
        }
        let recorded = registry.profiles
        await registry.updateCheckup(snapshot(2000), runtimeState: nil, observedMAC: peerMAC, for: profile.id)
        XCTAssertEqual(registry.profiles, recorded)
        // The learner leaves the saved MAC as it is, and leaves the error to
        // the operation that met the other AirPort.
        await registry.confirmAirportIdentity(try XCTUnwrap(AirportIdentityObservation(mac: peerMAC)), for: profile.id)
        XCTAssertNil(registry.error)
        XCTAssertEqual(registry.profiles, recorded)
        await registry.load()
        XCTAssertEqual(registry.profiles, recorded)
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

    func testConcurrentOperationsClaimHardwareIdentityOnce() async throws {
        let registry = try await loadedRegistry()
        for (id, host) in [("one", "192.0.2.10"), ("two", "fd00::10")] {
            _ = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: host),
                discoveredDevice: nil, passwordState: .available, preferredID: id)
        }
        let observation = try XCTUnwrap(AirportIdentityObservation(mac: mac))
        async let one: Void = registry.confirmAirportIdentity(observation, for: "one")
        async let two: Void = registry.confirmAirportIdentity(observation, for: "two")
        _ = await (one, two)
        await registry.load()
        XCTAssertEqual(registry.profiles.filter { $0.network.airportMAC == mac }.count, 1)
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

    private func storeNamedProfile(_ registry: DeviceRegistryStore, id: String = "legacy", name: String = "Shared",
                                   hostname: String = "shared.local", host: String = "10.0.0.2") async throws -> DeviceProfile {
        let record = testDeviceRecord(name: name, hostname: hostname, ipv4: [host], fullname: "\(name)._airport._tcp.local.")
        let discovered = DiscoveredDevice(record: try record.decode(BonjourResolvedServicePayload.self), index: 0)
        return try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: host),
            discoveredDevice: discovered, passwordState: .available, preferredID: id)
    }

    private func loadedRegistry() async throws -> DeviceRegistryStore {
        let registry = DeviceRegistryStore(applicationSupportURL: try TemporaryDirectory().url, now: { Date(timeIntervalSince1970: 1000) })
        await registry.load()
        return registry
    }

    private func visible(_ name: String, hostname: String, mac: String?) -> DeviceNetworkIdentity {
        DeviceNetworkIdentity(configuredSSHTarget: "10.0.0.50", hostname: hostname, bonjourName: name,
                              bonjourFullname: "\(name)._airport._tcp.local.", airportMAC: mac)
    }

    func testWhatAnOperationsNamesTeachAProfileWithoutAMAC() async throws {
        // An operation at the saved address reports what answered there. Two
        // AirPorts of one household often share the admin password, so the
        // MAC is saved only when the names say it is this profile's device.
        struct Case {
            let name: String
            var profile: String = "Shared"
            var hostname: String = "shared.local"
            var instanceName: String?
            var hostLabel: String?
            var fromDNSName = false
            var visible: [DeviceNetworkIdentity] = []
            var otherProfile: String?
            var otherProfileHostname = "shared.local"
            let learns: Bool
        }
        let cases = [
            Case(name: "same name", instanceName: "Shared", learns: true),
            // With no service name reported, the host label is all there is.
            Case(name: "same host label", hostLabel: "shared", learns: true),
            // Two known names decide: many names share a host label.
            Case(name: "another name, same host label", instanceName: "Renamed", hostLabel: "shared", learns: false),
            // A label from syDN stays when syNm is renamed, so it still names the device.
            Case(name: "renamed under its syDN label", instanceName: "Renamed", hostLabel: "shared", fromDNSName: true,
                 learns: true),
            Case(name: "syDN label of another device", instanceName: "Renamed", hostLabel: "den", fromDNSName: true,
                 learns: false),
            Case(name: "renamed under its syDN label, old name now elsewhere", instanceName: "Renamed", hostLabel: "shared",
                 fromDNSName: true, visible: [visible("Shared", hostname: "other.local", mac: peerMAC)], learns: false),
            // ACPd turns each non-ASCII byte into "-": both are Caf.
            Case(name: "an accent away", profile: "Café", hostname: "caf.local", instanceName: "Cafè", hostLabel: "caf",
                 learns: false),
            Case(name: "international name", profile: "Café", hostname: "caf.local", instanceName: "Café", hostLabel: "caf",
                 learns: true),
            // Another device whose name only shares the host label is no twin.
            Case(name: "international name beside another", profile: "Café", hostname: "caf.local", instanceName: "Café",
                 hostLabel: "caf", visible: [visible("Cafè", hostname: "caf-2.local", mac: peerMAC)], learns: true),
            // Another saved profile kept caf from when its device held it: Apple
            // never saves a rename, so both profiles can keep the same label.
            Case(name: "international name beside a stale profile", profile: "Café", hostname: "caf.local",
                 instanceName: "Café", hostLabel: "caf", otherProfile: "Cafè", otherProfileHostname: "caf.local", learns: true),
            // A device on that very label now is a conflict Apple has yet to rename.
            Case(name: "international name beside a live conflict", profile: "Café", hostname: "caf.local",
                 instanceName: "Café", hostLabel: "caf", visible: [visible("Cafè", hostname: "caf.local", mac: peerMAC)],
                 learns: false),
            Case(name: "Office 2 and Office-2", profile: "Office 2", hostname: "office-2.local", instanceName: "Office-2",
                 hostLabel: "office-2", learns: false),
            // Bonjour folds ASCII case only, and never normalizes.
            Case(name: "non-ASCII case", profile: "Été", hostname: "t.local", instanceName: "été", hostLabel: "t", learns: false),
            Case(name: "ASCII case", instanceName: "SHARED", learns: true),
            Case(name: "decomposed accent", profile: "Caf\u{E9}", hostname: "caf.local", instanceName: "Cafe\u{301}",
                 hostLabel: "caf", learns: false),
            Case(name: "another device", instanceName: "Den Capsule", hostLabel: "den-capsule", learns: false),
            Case(name: "no names reported", learns: false),
            // A twin with the same name is on the network, renamed by Apple.
            Case(name: "visible twin", instanceName: "Shared",
                 visible: [visible("Shared (2)", hostname: "shared-2.local", mac: peerMAC)], learns: false),
            // The device itself, seen by discovery, is no twin.
            Case(name: "itself visible", instanceName: "Shared",
                 visible: [visible("Shared", hostname: "shared.local", mac: mac)], learns: true),
            // Saved while another device had the name: it named neither.
            Case(name: "saved renamed", profile: "Shared (2)", hostname: "shared-2.local", instanceName: "Shared", learns: false),
            Case(name: "another profile with the name", instanceName: "Shared", otherProfile: "Shared", learns: false),
            // "Office 2" is a real name, not Apple's rename of "Office".
            Case(name: "numbered name", profile: "Office 2", hostname: "office-2.local", instanceName: "Office 2",
                 hostLabel: "office-2", visible: [visible("Office", hostname: "office.local", mac: peerMAC)], learns: true),
        ]
        for testCase in cases {
            let registry = try await loadedRegistry()
            let profile = try await storeNamedProfile(registry, name: testCase.profile, hostname: testCase.hostname)
            if let otherProfile = testCase.otherProfile {
                _ = try await storeNamedProfile(registry, id: "other", name: otherProfile,
                                                hostname: testCase.otherProfileHostname, host: "10.0.0.3")
            }
            let observation = try XCTUnwrap(AirportIdentityObservation(mac: mac, instanceName: testCase.instanceName,
                                                                      hostLabel: testCase.hostLabel,
                                                                      hostLabelFromDNSName: testCase.fromDNSName))
            await registry.confirmAirportIdentity(observation, for: profile.id, visible: testCase.visible)
            XCTAssertEqual(registry.profile(id: profile.id)?.network.airportMAC, testCase.learns ? mac : nil, testCase.name)
            XCTAssertNil(registry.error, testCase.name)
        }
    }

    func testAProfileWithoutANameLearnsTheMACOnlyWithNoOtherAirPortInSight() async throws {
        // Its address is all that says which device it is, and DHCP may have
        // given it to the household's other AirPort, whose MAC would stay.
        let cases: [(String, [DeviceNetworkIdentity], Bool)] = [
            ("nothing visible", [], true),
            ("only itself", [visible("Office", hostname: "office.local", mac: mac)], true),
            ("another AirPort", [visible("Office", hostname: "office.local", mac: mac),
                                 visible("Den", hostname: "den.local", mac: peerMAC)], false),
            ("an AirPort without a MAC", [visible("Den", hostname: "den.local", mac: nil)], false),
        ]
        for (label, visibleDevices, learns) in cases {
            let registry = try await loadedRegistry()
            let profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
                discoveredDevice: nil, passwordState: .available, preferredID: "by-address")
            await registry.confirmAirportIdentity(try XCTUnwrap(AirportIdentityObservation(mac: mac, instanceName: "Office",
                hostLabel: "office")), for: profile.id, visible: visibleDevices)
            XCTAssertEqual(registry.profile(id: profile.id)?.network.airportMAC, learns ? mac : nil, label)
            XCTAssertNil(registry.error, label)
        }
    }

    func testAProfileWithoutANameTakesOnlyTheMAC() async throws {
        // Saved by address, or with a fallback hostname that names no device:
        // its address is all there is. Learning the MAC changes nothing about
        // how the profile is named or reached.
        for hostname in [nil, "timecapsule.local", "base-station-58f15c.local"] {
            let registry = try await loadedRegistry()
            var profile = try await registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
                discoveredDevice: nil, passwordState: .available, preferredID: "by-address")
            profile.network.hostname = hostname
            profile = try await registry.saveProfile(profile)
            let smbAddress = SMBAddressPolicy.url(for: profile)
            await registry.confirmAirportIdentity(try XCTUnwrap(AirportIdentityObservation(mac: mac, instanceName: "Office",
                                                                                          hostLabel: "office")), for: profile.id)
            await registry.load()
            let saved = try XCTUnwrap(registry.profile(id: profile.id))
            XCTAssertEqual(saved.network.airportMAC, mac, String(describing: hostname))
            XCTAssertNil(saved.network.bonjourName)
            XCTAssertEqual(saved.title, profile.title)
            XCTAssertEqual(SMBAddressPolicy.url(for: saved), smbAddress)
        }
    }

    func testAnEventCarriesTheIdentityInItsPayloadOrDebugFields() throws {
        let fields: JSONValue = .object([
            "airport_mac": .string("02-AA-BB-CC-DD-EE"), "airport_instance_name": .string(" Office"),
            "airport_host_label": .string("Office"), "airport_host_label_source": .string("syDN"),
        ])
        for event in [
            BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: fields),
            BackendEvent(type: "error", operation: "deploy", debug: fields),
        ] {
            XCTAssertEqual(AirportIdentityObservation(event: event),
                           AirportIdentityObservation(mac: mac, instanceName: " Office", hostLabel: "office",
                                                      hostLabelFromDNSName: true))
        }
        // A label from syNm, or no label, gives the label no extra weight.
        for source: JSONValue? in [.string("syNm"), nil] {
            var values: [String: JSONValue] = ["airport_mac": .string(mac), "airport_host_label": .string("office")]
            values["airport_host_label_source"] = source
            let event = BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: .object(values))
            XCTAssertEqual(AirportIdentityObservation(event: event)?.hostLabelFromDNSName, false)
        }
        XCTAssertEqual(AirportIdentityObservation(mac: mac, hostLabelFromDNSName: true)?.hostLabelFromDNSName, false)
        XCTAssertNil(AirportIdentityObservation(event: BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: .object([:]))))
        XCTAssertNil(AirportIdentityObservation(mac: "not-a-mac", instanceName: "Office"))
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
