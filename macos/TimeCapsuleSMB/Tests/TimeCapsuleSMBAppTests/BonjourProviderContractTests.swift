import XCTest
@testable import TimeCapsuleSMBApp

final class BonjourProviderContractTests: XCTestCase {
    func testDecodesActualPairedProviderPayloadsAndKeepsSelectionEvidence() throws {
        let scenarios = try fixtureScenarios()
        XCTAssertEqual(scenarios.count, 16)
        for scenario in scenarios {
            let name = try XCTUnwrap(scenario["case"] as? String)
            let data = try JSONSerialization.data(withJSONObject: XCTUnwrap(scenario["payload"]))
            let payload = try JSONDecoder().decode(DiscoverPayload.self, from: data)
            let devices = payload.devices.enumerated().map { DiscoveredDevice(payload: $0.element, index: $0.offset) }
            XCTAssertEqual(devices.count, scenario["expected_device_count"] as? Int, name)
            XCTAssertEqual(devices.map(\.connectionTarget), scenario["expected_hosts"] as? [String], name)
            XCTAssertEqual(Set(devices.map(\.id)).count, devices.count, name)
            for (index, device) in devices.enumerated() {
                let original = payload.devices[index]
                XCTAssertEqual(device.name, original.name, name)
                XCTAssertEqual(device.fullname, original.fullname, name)
                XCTAssertEqual(device.observedIdentity.bonjourName, original.name, name)
                XCTAssertEqual(device.observedIdentity.bonjourFullname, original.fullname, name)
            }
            if name == "equal_fullnames" || name == "equal_hostnames" {
                XCTAssertTrue(Set(devices.compactMap(\.airportMAC)).count == 2)
                XCTAssertEqual(devices[0].fullname, devices[1].fullname)
            }
            if name == "distinct_fullnames_shared_hostname" {
                XCTAssertTrue(Set(devices.compactMap(\.airportMAC)).count == 2)
                XCTAssertEqual(devices[0].hostname, devices[1].hostname)
                XCTAssertNotEqual(devices[0].fullname, devices[1].fullname)
            }
            if name == "dual_stack" {
                XCTAssertEqual(devices[0].addresses, ["192.0.2.10", "fd00::10"])
                XCTAssertEqual(devices[0].model, "TimeCapsule6,116")
                if case .object(let raw) = devices[0].rawRecord {
                    XCTAssertEqual(raw["interface_index"], .number(14))
                } else { XCTFail("Selected record evidence was lost") }
            }
            if name == "unsupported" { XCTAssertTrue(devices[0].isUnsupportedModel) }
            if name == "unknown_model" { XCTAssertFalse(devices[0].isUnsupportedModel) }
        }
    }

    @MainActor
    func testSameHostnameProviderPeersSaveIndependentlyAndSurviveReloadAndEdit() async throws {
        let scenarios = try fixtureScenarios().filter {
            ["equal_hostnames", "distinct_fullnames_shared_hostname"].contains(($0["case"] as? String) ?? "")
        }
        XCTAssertEqual(scenarios.count, 2)
        for scenario in scenarios {
            let name = try XCTUnwrap(scenario["case"] as? String)
            do {
                let data = try JSONSerialization.data(withJSONObject: XCTUnwrap(scenario["payload"]))
                let payload = try JSONDecoder().decode(DiscoverPayload.self, from: data)
                let devices = payload.devices.enumerated().map { DiscoveredDevice(payload: $0.element, index: $0.offset) }
                XCTAssertEqual(devices.count, 2)
                XCTAssertEqual(devices[0].hostname, devices[1].hostname)
                XCTAssertTrue(Set(devices.compactMap(\.airportMAC)).count == 2)
                let temp = try TemporaryDirectory()
                let registry = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
                let passwords = InMemoryPasswordStore()
                await registry.load()
                var saved: [DeviceProfile] = []
                for (index, device) in devices.enumerated() {
                    var profile = try await registry.storeTestProfile(
                        configuredDevice: testConfiguredDevice(host: device.connectionTarget, airportMAC: device.airportMAC), discoveredDevice: device,
                        passwordState: .available, preferredID: "peer-\(index)")
                    profile.displayName = "Peer \(index)"
                    profile.settings.debugLogging = index == 0
                    saved.append(try await registry.updateProfile(profile))
                    try passwords.save("password-\(index)", for: profile.keychainAccount)
                    try Data("configuration-\(index)".utf8).write(to: profile.configURL)
                }
                XCTAssertEqual(Set(saved.map(\.id)).count, 2)
                XCTAssertEqual(Set(saved.map(\.configPath)).count, 2)
                XCTAssertEqual(Set(saved.map(\.keychainAccount)).count, 2)
                let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url)
                await reloaded.load()
                XCTAssertEqual(reloaded.profiles.count, 2)
                for (index, original) in saved.enumerated() {
                    var profile = try XCTUnwrap(reloaded.profile(id: original.id))
                    XCTAssertEqual(profile, original)
                    XCTAssertEqual(profile.network.airportMAC, devices[index].airportMAC)
                    XCTAssertEqual(reloaded.matchingProfile(for: devices[index])?.id, original.id)
                    XCTAssertEqual(try passwords.password(for: profile.keychainAccount), "password-\(index)")
                    XCTAssertEqual(try Data(contentsOf: profile.configURL), Data("configuration-\(index)".utf8))
                    profile.displayName += " edited"
                    _ = try await reloaded.updateProfile(profile)
                    XCTAssertEqual(reloaded.profile(id: saved[1 - index].id)?.host, saved[1 - index].host)
                }
                await reloaded.load()
                XCTAssertEqual(Set(reloaded.profiles.map(\.id)), Set(saved.map(\.id)))
                XCTAssertEqual(Set(reloaded.profiles.compactMap(\.network.airportMAC)).count, 2)
            } catch {
                XCTFail("\(name) same-hostname peers failed to save: \(error)")
            }
        }
    }

    private func fixtureScenarios() throws -> [[String: Any]] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "bonjour_payloads", withExtension: "json", subdirectory: "Fixtures"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
    }
}
