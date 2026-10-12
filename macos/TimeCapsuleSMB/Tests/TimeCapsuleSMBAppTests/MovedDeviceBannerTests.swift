import XCTest
@testable import TimeCapsuleSMBApp

/// Finding a saved device after DHCP moved it: by its saved name for a profile
/// without a MAC, turning SSH on while saving the new address, and Checkup
/// results that show the banners.
@MainActor
final class MovedDeviceBannerTests: XCTestCase {
    private let mac = "02:aa:bb:cc:dd:ee"

    @MainActor
    private struct Fixture {
        let appStore: AppStore
        let registry: DeviceRegistryStore
        let passwordStore: InMemoryPasswordStore
        let runner: StoreTestRunner
        var discovery: DeviceDiscoveryStore { appStore.deviceDiscovery }
        var coordinator: OperationCoordinator { appStore.operationCoordinator }
    }

    private func makeFixture(_ responses: [StoreTestRunner.Response]) async throws -> Fixture {
        let registry = DeviceRegistryStore(applicationSupportURL: try TemporaryDirectory().url)
        await registry.load()
        let runner = StoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let passwordStore = InMemoryPasswordStore()
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: passwordStore,
            deviceDiscovery: DeviceDiscoveryStore(coordinator: coordinator, registry: registry)
        )
        return Fixture(appStore: appStore, registry: registry, passwordStore: passwordStore, runner: runner)
    }

    private func record(name: String = "Office Capsule", hostname: String? = nil, host: String, mac: String? = nil) -> JSONValue {
        testDeviceRecord(name: name, hostname: hostname ?? "\(name.lowercased().replacingOccurrences(of: " ", with: "-")).local.",
            ipv4: [host], fullname: "\(name)._airport._tcp.local.", airportMAC: mac)
    }

    private func discoverResponse(_ records: [JSONValue]) -> StoreTestRunner.Response {
        .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: records))])
    }

    /// A profile saved before the MAC was, from a discovery of the device at .2.
    private func saveOldProfile(_ fixture: Fixture, id: String = "old", name: String = "Office Capsule", hostname: String? = nil,
                                host: String = "10.0.0.2", mac savedMAC: String? = nil) async throws -> DeviceProfile {
        let discovered = DiscoveredDevice(record: try record(name: name, hostname: hostname, host: host)
            .decode(BonjourResolvedServicePayload.self), index: 0)
        let profile = try await fixture.registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: host, airportMAC: savedMAC),
            discoveredDevice: discovered, passwordState: .available, preferredID: id)
        try fixture.passwordStore.save("pw", for: profile.keychainAccount)
        return profile
    }

    private func discover(_ fixture: Fixture) async throws {
        fixture.discovery.refresh()
        try await waitUntilStoreState { fixture.discovery.state == .ready }
    }

    func testAProfileWithoutAMACFindsItsMovedDeviceByName() async throws {
        let current = record(host: "10.0.0.80", mac: mac)
        let fixture = try await makeFixture([
            discoverResponse([current]),
            .init(events: [BackendEvent(type: "result", operation: "configure", ok: true,
                payload: testConfigurePayload(host: "root@10.0.0.80", airportMAC: mac))]),
        ])
        let profile = try await saveOldProfile(fixture)
        try await discover(fixture)

        let notice = try XCTUnwrap(fixture.discovery.staleEndpointNotice(for: profile))
        XCTAssertTrue(notice.matchedByName)
        XCTAssertEqual(notice.configuredHost, "10.0.0.2")
        XCTAssertEqual(notice.currentHost, "10.0.0.80")

        let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)
        session.updateConfiguredAddressFromDiscovery(profile: profile)
        try await waitUntilStoreState { session.profileEditorStore.state == .saved }

        // Configure gets the record it was found by, so the MAC it reads is checked against it.
        let configure = try XCTUnwrap(fixture.runner.calls.first { $0.operation == "configure" })
        XCTAssertEqual(configure.params["selected_record"], current)
        XCTAssertEqual(configure.params["address_source"], .string("name"))
        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(saved.host, "root@10.0.0.80")
        XCTAssertEqual(saved.network.airportMAC, mac)
    }

    func testANameDoesNotFindTheDeviceWhenItCouldBeAnother() async throws {
        let twin = "02:aa:bb:cc:dd:ff"
        let cases: [(String, [JSONValue], String?, String)] = [
            ("two devices with the name", [record(host: "10.0.0.80", mac: mac), record(host: "10.0.0.81", mac: twin)], nil, "Office Capsule"),
            // Apple renamed the twin; the two can swap names after a restart.
            ("a renamed twin", [record(host: "10.0.0.80", mac: mac), record(name: "Office Capsule (2)", host: "10.0.0.81", mac: twin)],
             nil, "Office Capsule"),
            // A renamed device shows another one has, or had, its name.
            ("a renamed device", [record(name: "Office Capsule (2)", host: "10.0.0.80", mac: mac)], nil, "Office Capsule"),
            ("saved while renamed", [record(name: "Office Capsule (2)", host: "10.0.0.80", mac: mac)], nil, "Office Capsule (2)"),
            ("a profile with a MAC", [record(host: "10.0.0.80", mac: twin)], mac, "Office Capsule"),
            ("another name", [record(name: "Den Capsule", host: "10.0.0.80", mac: mac)], nil, "Office Capsule"),
        ]
        for (label, records, savedMAC, savedName) in cases {
            let fixture = try await makeFixture([discoverResponse(records)])
            let profile = try await saveOldProfile(fixture, name: savedName, mac: savedMAC)
            try await discover(fixture)
            XCTAssertNil(fixture.discovery.staleEndpointNotice(for: profile), label)
        }
    }

    func testANameOneAccentAwayDoesNotFindTheDevice() async throws {
        // "Café" and "Cafè" share the host label Caf (ACPd turns each
        // non-ASCII byte into "-"), but they are two devices' names.
        let fixture = try await makeFixture([discoverResponse([record(name: "Cafè", hostname: "caf.local.", host: "10.0.0.80", mac: mac)])])
        let profile = try await saveOldProfile(fixture, name: "Café", hostname: "caf.local.")
        try await discover(fixture)
        XCTAssertNil(fixture.discovery.staleEndpointNotice(for: profile))
        // The same international name does.
        let same = try await makeFixture([discoverResponse([record(name: "Café", hostname: "caf.local.", host: "10.0.0.80", mac: mac)])])
        let moved = try await saveOldProfile(same, name: "Café", hostname: "caf.local.")
        try await discover(same)
        XCTAssertEqual(same.discovery.staleEndpointNotice(for: moved)?.currentHost, "10.0.0.80")
    }

    func testAChangedAddressSaysWhatFoundIt() async throws {
        // Telemetry: the saved MAC at the new address, the saved name (the
        // banner of a profile without a MAC), or neither.
        let cases: [(String, String?, [JSONValue], String, String)] = [
            ("banner by MAC", mac, [record(host: "10.0.0.80", mac: mac)], "10.0.0.80", "mac"),
            ("banner by name", nil, [record(host: "10.0.0.80", mac: mac)], "10.0.0.80", "name"),
            ("typed, nothing there", mac, [record(host: "10.0.0.80", mac: mac)], "10.0.0.90", "manual"),
            // Matched only by its saved address: nothing says the new one is the same device.
            ("typed, matched by address", nil, [testDeviceRecord(name: "Den", hostname: "den.local.", ipv4: ["10.0.0.77", "10.0.0.2"],
                                                                  fullname: "Den._airport._tcp.local.")], "10.0.0.77", "manual"),
        ]
        for (label, savedMAC, records, newHost, expected) in cases {
            let fixture = try await makeFixture([
                discoverResponse(records),
                .init(events: [BackendEvent(type: "result", operation: "configure", ok: true,
                    payload: testConfigurePayload(host: "root@\(newHost)", airportMAC: mac))]),
            ])
            let profile = try await saveOldProfile(fixture, mac: savedMAC)
            try await discover(fixture)
            let editor = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)
            editor.draft.host = newHost
            await editor.save(profile: profile)
            try await waitUntilStoreState { fixture.runner.calls.contains { $0.operation == "configure" } }
            let configure = try XCTUnwrap(fixture.runner.calls.first { $0.operation == "configure" })
            XCTAssertEqual(configure.params["address_source"], .string(expected), label)
        }
    }

    func testADeviceAnotherProfileOwnsIsNotFoundByName() async throws {
        let fixture = try await makeFixture([discoverResponse([record(host: "10.0.0.80", mac: mac)])])
        let profile = try await saveOldProfile(fixture)
        _ = try await saveOldProfile(fixture, id: "owner", host: "10.0.0.9", mac: mac)
        try await discover(fixture)
        XCTAssertNil(fixture.discovery.staleEndpointNotice(for: profile))
    }

    private func confirmationRequired() -> StoreTestRunner.Response {
        .init(events: [BackendEvent(
            type: "error", operation: "configure", code: "confirmation_required", message: "SSH is closed.",
            details: .object([
                "confirmation_id": .string("confirm-ssh"),
                "presentation_id": .string("configure.enable_ssh_reboot"),
                "presentation_values": .object(["device_name": .string("Office Capsule")]),
            ])
        )], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
    }

    private func startHostChange(_ fixture: Fixture, profile: DeviceProfile) async throws -> DeviceProfileEditorStore {
        let editor = DeviceProfileEditorStore(profile: profile, appStore: fixture.appStore)
        editor.draft.host = "10.0.0.80"
        editor.draft.displayName = "Garage"
        await editor.save(profile: profile)
        let lane = fixture.coordinator.lane(for: .deviceWorkflow(profile.id, .configure))
        try await waitUntilStoreState { fixture.coordinator.pendingConfirmation != nil && !lane.backend.isRunning }
        return editor
    }

    func testANewAddressWithSSHOffIsSavedOnceTheUserTurnsSSHOn() async throws {
        let fixture = try await makeFixture([
            confirmationRequired(),
            .init(events: [BackendEvent(type: "result", operation: "configure", ok: true,
                payload: testConfigurePayload(host: "root@10.0.0.80", airportMAC: mac))]),
        ])
        let profile = try await saveOldProfile(fixture)
        let editor = try await startHostChange(fixture, profile: profile)
        // Waiting for the answer is not a failure.
        XCTAssertEqual(editor.state, .reconfiguring)
        XCTAssertNil(editor.error)

        fixture.coordinator.confirmPending()

        try await waitUntilStoreState { editor.state == .saved }
        XCTAssertEqual(fixture.runner.calls[1].params["confirmation_id"], .string("confirm-ssh"))
        let saved = try XCTUnwrap(fixture.registry.profile(id: profile.id))
        XCTAssertEqual(saved.host, "root@10.0.0.80")
        // The editor's own changes are saved with the address.
        XCTAssertEqual(saved.displayName, "Garage")
    }

    func testDecliningToTurnSSHOnSavesNothing() async throws {
        let fixture = try await makeFixture([confirmationRequired()])
        let profile = try await saveOldProfile(fixture)
        let editor = try await startHostChange(fixture, profile: profile)

        fixture.coordinator.cancelPendingConfirmation()

        try await waitUntilStoreState { editor.state == .dirty }
        XCTAssertNil(editor.error)
        XCTAssertEqual(fixture.registry.profile(id: profile.id)?.host, profile.host)
        XCTAssertEqual(editor.draft.host, "10.0.0.80")
    }

    func testCheckupResultsShowTheAddressAndSSHBanners() async throws {
        for (code, follow) in [("device_unreachable", "discover"), ("ssh_disabled", "set-ssh")] {
            let check: JSONValue = .object([
                "status": .string("FAIL"), "message": .string("failed"),
                "details": .object(["code": .string(code), "domain": .string("computer")]),
            ])
            let fixture = try await makeFixture([
                .init(events: [BackendEvent(type: "result", operation: "doctor", ok: false,
                    payload: testDoctorPayload(fatal: true, checks: [check]))]),
                .init(events: [BackendEvent(type: "result", operation: follow, ok: true,
                    payload: follow == "discover" ? testDiscoverPayload(records: []) : testSSHAccessPayload())]),
            ])
            let profile = try await saveOldProfile(fixture)
            let session = DeviceDashboardSession(profile: profile, appStore: fixture.appStore)

            _ = fixture.coordinator.run(operation: "doctor", context: profile.runtimeContext, activeDeviceID: profile.id,
                laneKey: .deviceWorkflow(profile.id, .doctor))

            try await waitUntilStoreState { fixture.runner.calls.map(\.operation) == ["doctor", follow] }
            _ = session
        }
    }
}
