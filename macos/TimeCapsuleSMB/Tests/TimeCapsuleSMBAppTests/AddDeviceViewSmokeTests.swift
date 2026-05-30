import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AddDeviceViewSmokeTests: XCTestCase {
    func testRendersIdleManualAndLocalValidationStates() async throws {
        let fixture = try await makeFixture()

        try assertRendersNonBlank(AddDeviceView(store: fixture.store), size: CGSize(width: 900, height: 700))

        fixture.store.startManualEntry()
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
                delayNanoseconds: 250_000_000
            )
        ])
        discovering.store.runDiscover()
        XCTAssertEqual(discovering.store.state, .discovering)
        try assertRendersNonBlank(AddDeviceView(store: discovering.store), size: CGSize(width: 900, height: 700))

        let empty = try await makeFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
            ])
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

    func testRendersConfigureTerminalStates() async throws {
        try await renderConfigureState(
            responses: [
                .init(
                    events: [BackendEvent(type: "stage", operation: "configure", stage: "connect_ssh")],
                    delayNanoseconds: 250_000_000
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
        try await renderConfigureState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload())
                ])
            ],
            expectedState: .saved
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
    }

    private func makeFixture(responses: [StoreTestRunner.Response] = []) async throws -> (
        store: AddDeviceFlowStore,
        registry: DeviceRegistryStore,
        runner: StoreTestRunner
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = StoreTestRunner(responses: responses)
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
