import XCTest
@testable import TimeCapsuleSMBApp

/// Saving the AirPort MAC an operation confirmed, so a profile follows its
/// device after DHCP moves it, old profiles from before the MAC was saved included.
@MainActor
final class AirportIdentityLearningTests: XCTestCase {
    private let mac = "02:aa:bb:cc:dd:ee"
    private let twinMAC = "02:aa:bb:cc:dd:ff"

    private func identityFields(_ name: String = "Office Capsule") -> [String: JSONValue] {
        ["airport_mac": .string(mac), "airport_instance_name": .string(name), "airport_host_label": .string("office-capsule")]
    }

    private func statusPayload(withIdentity: Bool) -> JSONValue {
        guard withIdentity, case .object(var values) = testSSHAccessPayload(sshPortReachable: true) else {
            return testSSHAccessPayload(sshPortReachable: true)
        }
        values.merge(identityFields()) { $1 }
        return .object(values)
    }

    private func statusResponse(withIdentity: Bool = true) -> StoreTestRunner.Response {
        .init(events: [BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: statusPayload(withIdentity: withIdentity))])
    }

    private func discoverResponse(_ records: [JSONValue]) -> StoreTestRunner.Response {
        .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: records))])
    }

    /// A profile saved from Bonjour, before MACs were saved: it has the device's names.
    private func namedProfile(_ fixture: AppViewFixture) async throws -> DeviceProfile {
        let record = try testDeviceRecord(ipv4: ["10.0.0.2"]).decode(BonjourResolvedServicePayload.self)
        let profile = try await fixture.registry.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: DiscoveredDevice(record: record, index: 0), passwordState: .available, preferredID: "named")
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        XCTAssertNil(profile.network.airportMAC)
        return profile
    }

    func testEveryFinishedOperationTeachesAProfileItsMAC() async throws {
        // Results carry the identity in their payloads, failures in their debug fields.
        let cases: [(String, BackendEvent)] = [
            ("set-ssh", BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: statusPayload(withIdentity: true))),
            ("doctor", BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(identityFields()))),
            ("deploy", BackendEvent(type: "error", operation: "deploy", code: "acpd_broken", debug: .object(identityFields()))),
        ]
        for (operation, event) in cases {
            let fixture = try await AppViewFixture(responses: [.init(events: [event])], discoveryWaitsForReadiness: false)
            let profile = try await namedProfile(fixture)

            // No dashboard is open: the app saves it from any finished lane.
            _ = fixture.appStore.operationCoordinator.run(operation: operation, profile: profile, password: "pw")
            try await waitUntilStoreState { fixture.registry.profile(id: profile.id)?.network.airportMAC == self.mac }
            XCTAssertNil(fixture.registry.error, operation)
        }
    }

    func testATwinOnTheNetworkKeepsTheMACUnsaved() async throws {
        // Another AirPort with the same name: Apple renamed it, and the two can
        // swap names, so the name says which device this is no longer.
        let twin = testDeviceRecord(name: "Office Capsule (2)", hostname: "office-capsule-2.local.", ipv4: ["10.0.0.9"],
                                    fullname: "Office Capsule (2)._airport._tcp.local.", airportMAC: twinMAC)
        let fixture = try await AppViewFixture(responses: [discoverResponse([twin]), statusResponse()],
                                               discoveryWaitsForReadiness: false)
        let profile = try await namedProfile(fixture)
        fixture.appStore.deviceDiscovery.refresh()
        try await waitUntilStoreState { fixture.appStore.deviceDiscovery.devices.count == 1 }

        fixture.appStore.sshAccessStore.refresh(profile: profile)
        try await waitUntilStoreState { fixture.appStore.sshAccessStore.snapshot(for: profile) != nil }
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertNil(fixture.registry.profile(id: profile.id)?.network.airportMAC)
        XCTAssertNil(fixture.registry.error)
    }

    func testAResultWithoutAnIdentityChangesNothing() async throws {
        let fixture = try await AppViewFixture(responses: [statusResponse(withIdentity: false)], discoveryWaitsForReadiness: false)
        let profile = try await fixture.saveProfile(id: "old")
        let before = fixture.registry.profiles

        fixture.appStore.sshAccessStore.refresh(profile: profile)
        try await waitUntilStoreState { fixture.appStore.sshAccessStore.snapshot(for: profile) != nil }
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(fixture.registry.profiles, before)
    }

    func testOnlyAProfileWithoutAMACSendsThePasswordToTheBackgroundCheck() async throws {
        for saved in [nil, mac] {
            let fixture = try await AppViewFixture(responses: [
                discoverResponse([testDeviceRecord(ipv4: ["10.0.0.2"])]),
                statusResponse(withIdentity: false),
            ], discoveryWaitsForReadiness: false)
            let profile = try await fixture.registry.storeTestProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: saved),
                discoveredDevice: nil, passwordState: .available, preferredID: "device")
            try fixture.passwordStore.save("pw", for: profile.keychainAccount)
            let runner = try XCTUnwrap(fixture.runner as? StoreTestRunner)

            fixture.appStore.deviceDiscovery.refresh()
            // The first discovery's devices count: the check reads them after they change.
            try await waitUntilStoreState { runner.calls.contains { $0.operation == "set-ssh" } }

            let check = try XCTUnwrap(runner.calls.first { $0.operation == "set-ssh" })
            XCTAssertEqual(check.params["credentials"] != nil, saved == nil, "\(saved ?? "no MAC")")
        }
    }

    func testTheBackgroundCheckReadsTheKeychainOnlyWhenItRuns() async throws {
        // Discovery changes often; a check that just ran is not run again, so
        // its password is not read either.
        let fixture = try await AppViewFixture(responses: [statusResponse(withIdentity: false)], discoveryWaitsForReadiness: false)
        let profile = try await fixture.saveProfile(id: "old")
        let password: () -> String? = { fixture.appStore.password(for: profile) }

        fixture.appStore.sshAccessStore.refreshIfNeeded(profile: profile, password: password())
        try await waitUntilStoreState { fixture.appStore.sshAccessStore.snapshot(for: profile) != nil }
        let reads = fixture.passwordStore.passwordReadCount
        XCTAssertGreaterThan(reads, 0)

        fixture.appStore.sshAccessStore.refreshIfNeeded(profile: profile, password: password())
        XCTAssertEqual(fixture.passwordStore.passwordReadCount, reads)
    }
}
