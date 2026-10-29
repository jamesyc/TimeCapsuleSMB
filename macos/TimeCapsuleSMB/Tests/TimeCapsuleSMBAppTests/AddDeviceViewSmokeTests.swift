import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AddDeviceViewSmokeTests: XCTestCase {
    func testRendersIdleManualAndLocalValidationStates() async throws {
        let fixture = try await makeFixture()

        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))

        fixture.store.startManualEntry()
        fixture.store.password = "secret"
        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))

        fixture.store.bonjourTimeout = "-1"
        fixture.store.runDiscover()
        XCTAssertEqual(fixture.store.state, .failed)
        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))
    }

    func testRendersDiscoveringEmptyReadyAndPasswordEntryStates() async throws {
        let discovering = try await makeFixture(responses: [
            .init(
                events: [
                    BackendEvent(type: "stage", operation: "discover", stage: "browse_bonjour")
                ],
                pauseAfterEvents: true
            )
        ])
        discovering.store.runDiscover()
        XCTAssertEqual(discovering.store.state, .discovering)
        try assertRendersNonBlank(AddDeviceView(store: discovering.store), size: CGSize(width: 900, height: 700))
        discovering.runner.finishAll()

        let empty = try await makeFixture(responses: [
            testDiscoverResponse([])
        ])
        empty.store.runDiscover()
        try await waitUntilStoreState { empty.store.state == .discoveryEmpty }
        try assertRendersNonBlank(AddDeviceView(store: empty.store), size: CGSize(width: 900, height: 700))

        let ready = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [], devices: [
                    testDiscoveredDevice(name: "Office Capsule", host: "10.0.0.2")
                ]))
            ])
        ])
        ready.store.runDiscover()
        try await waitUntilStoreState { ready.store.state == .discoveryReady && ready.store.selectedDevice != nil }
        try assertRendersNonBlank(AddDeviceView(store: ready.store), size: CGSize(width: 900, height: 700))

        let selected = try XCTUnwrap(ready.store.selectedDevice)
        ready.store.select(selected)
        XCTAssertEqual(ready.store.state, .passwordEntry)
        try assertRendersNonBlank(AddDeviceView(store: ready.store), size: CGSize(width: 900, height: 700))
    }

    func testRendersUnsupportedModelRowAndExplanation() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [], devices: [
                    testDiscoveredDevice(name: "Office Capsule", host: "10.0.0.2", supportedModel: true),
                    testDiscoveredDevice(
                        id: "bonjour:express",
                        name: "Living Room Express",
                        host: "10.0.0.40",
                        hostname: "express.local.",
                        syap: "115",
                        supportedModel: false,
                        fullname: "Living Room Express._airport._tcp.local."
                    )
                ]))
            ])
        ])
        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))

        fixture.store.select(try XCTUnwrap(fixture.store.devices.first { $0.isUnsupportedModel }))
        XCTAssertEqual(fixture.store.state, .unsupported)
        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))
    }

    func testDeviceRowsStayClickableWhenNoAddIsInProgress() async throws {
        let office = testDeviceRecord(name: "Office Capsule", ipv4: ["10.0.0.2"], fullname: "Office Capsule._airport._tcp.local.")
        let den = testDeviceRecord(name: "Den Capsule", ipv4: ["10.0.0.3"], fullname: "Den Capsule._airport._tcp.local.")
        let discover = StoreTestRunner.Response(events: [
            BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [office, den]))
        ])
        let fixture = try await makeFixture(responses: [
            discover,
            // The failed add's helper stays alive after it, as while it sends telemetry.
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "auth_failed", message: "bad password")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""), pauseAfterEvents: true),
            .init(events: discover.events, pauseBeforeEvents: true)
        ])
        defer { fixture.runner.finishAll() }
        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady && !fixture.store.isRunning }
        func rowsEnabled() -> [Bool] {
            accessibleElements(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))
                .filter { element in
                    element.role == .button && ["Office Capsule,", "Den Capsule,"].contains { element.text.hasPrefix($0) }
                }
                .map(\.isEnabled)
        }
        XCTAssertEqual(rowsEnabled(), [true, true])
        fixture.store.select(try XCTUnwrap(fixture.store.devices.last))
        fixture.store.password = "bad"
        fixture.store.runConfigure()
        try await waitUntilStoreState { fixture.store.state == .authFailed }
        // Discovery is idle, so this is the failed add's helper.
        XCTAssertTrue(fixture.store.isRunning)
        XCTAssertFalse(fixture.store.isAdding)
        XCTAssertEqual(rowsEnabled(), [true, true])

        // The app refreshes discovery, as All Devices or a device page does.
        fixture.store.discovery.refresh()
        try await waitUntilStoreState { fixture.store.discovery.state == .discovering }

        // The failed add stays on screen, and another AirPort can still be chosen.
        XCTAssertEqual(fixture.store.state, .authFailed)
        XCTAssertTrue(fixture.store.isRunning)
        XCTAssertEqual(rowsEnabled(), [true, true])
    }

    func testRendersConfigureTerminalStates() async throws {
        try await renderConfigureState(
            responses: [
                .init(
                    events: [BackendEvent(type: "stage", operation: "configure", stage: "connect_ssh")],
                    pauseAfterEvents: true
                )
            ],
            expectedState: .configuring
        )
        try await renderConfigureState(
            responses: [
                .init(events: [
                    BackendEvent(
                        type: "error",
                        operation: "configure",
                        code: "confirmation_required",
                        message: "Enable SSH?"
                    )
                ])
            ],
            expectedState: .awaitingConfirmation
        )
        try await renderConfigureState(
            responses: [
                .init(events: [
                    BackendEvent(type: "error", operation: "configure", code: "auth_failed", message: "Password rejected.")
                ])
            ],
            expectedState: .authFailed
        )
        try await renderConfigureState(
            responses: [
                .init(events: [
                    BackendEvent(type: "error", operation: "configure", code: "unsupported_device", message: "Unsupported device.")
                ])
            ],
            expectedState: .unsupported
        )
    }

    private func renderConfigureState(
        responses: [StoreTestRunner.Response],
        expectedState: AddDeviceFlowState
    ) async throws {
        let fixture = try await makeFixture(responses: responses)
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "pw"

        fixture.store.runConfigure()
        if expectedState != .configuring {
            try await waitUntilStoreState { fixture.store.state == expectedState }
        }

        XCTAssertEqual(fixture.store.state, expectedState)
        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))
        fixture.runner.finishAll()
    }

    private func makeFixture(responses: [StoreTestRunner.Response] = []) async throws -> (
        store: AddDeviceFlowStore,
        registry: DeviceRegistryStore,
        runner: PausingStoreTestRunner
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = PausingStoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let passwordStore = InMemoryPasswordStore()
        let persistence = DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
        let discovery = DeviceDiscoveryStore(coordinator: coordinator, registry: registry)
        let store = AddDeviceFlowStore(
            coordinator: coordinator,
            registry: registry,
            passwordStore: passwordStore,
            profilePersistence: persistence,
            discovery: discovery
        )
        return (store, registry, runner)
    }
}
