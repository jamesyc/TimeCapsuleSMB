import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceDiscoveryStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(
            DeviceDiscoveryState.allCases,
            [.idle, .waitingForReadiness, .discovering, .empty, .ready, .paused, .readinessBlocked, .failed]
        )
    }

    func testWaitsForReadinessThenDiscoversWithoutSavingProfiles() async throws {
        let fixture = try await makeFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())]),
            .init(events: [BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload())]),
            .init(events: [
                BackendEvent(type: "stage", operation: "discover", stage: "bonjour_discovery"),
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [
                    testDeviceRecord()
                ]))
            ])
        ])

        fixture.monitor.startMonitoring()
        XCTAssertEqual(fixture.monitor.state, .waitingForReadiness)
        fixture.readiness.start()

        try await waitUntilStoreState { fixture.monitor.state == .ready }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["capabilities", "validate-install", "discover"])
        XCTAssertEqual(fixture.monitor.devices.map(\.host), ["10.0.0.2"])
        XCTAssertEqual(fixture.monitor.unsavedDevices.count, 1)
        XCTAssertTrue(fixture.registry.profiles.isEmpty)
        XCTAssertEqual(fixture.monitor.currentStage?.stage, "bonjour_discovery")
    }

    func testDiscoveryEmptyFailedAndMalformedPayloadStatesAreExplicit() async throws {
        let empty = try await makeReadyFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))])
        ])
        empty.monitor.startMonitoring()
        try await waitUntilStoreState { empty.monitor.state == .empty }
        XCTAssertEqual(empty.monitor.devices, [])

        let failed = try await makeReadyFixture(responses: [
            .init(events: [BackendEvent.error(operation: "discover", code: "bonjour_failed", message: "Bonjour failed.")])
        ])
        failed.monitor.startMonitoring()
        try await waitUntilStoreState { failed.monitor.state == .failed }
        XCTAssertEqual(failed.monitor.error?.code, "bonjour_failed")

        let malformed = try await makeReadyFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: .object(["schema_version": .string("wrong")]))])
        ])
        malformed.monitor.startMonitoring()
        try await waitUntilStoreState { malformed.monitor.state == .failed }
        XCTAssertEqual(malformed.monitor.error?.code, "contract_decode_failed")
    }

    func testSavedProfilesAreFilteredAndReportedAsSeenNow() async throws {
        let fixture = try await makeReadyFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [
                testDeviceRecord()
            ]))])
        ])
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )

        fixture.monitor.startMonitoring()

        try await waitUntilStoreState { fixture.monitor.state == .ready }
        XCTAssertEqual(fixture.monitor.unsavedDevices, [])
        XCTAssertEqual(fixture.monitor.savedDevices.map(\.host), ["10.0.0.2"])
        XCTAssertEqual(fixture.monitor.lastSeenText(for: profile), "Seen now")
    }

    func testRefreshPausesBehindActiveOperationAndResumesWhenRunnerIsFree() async throws {
        let fixture = try await makeReadyFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "ok", domain: "Runtime")
                ]))
            ], pauseAfterEvents: true),
            .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [
                testDeviceRecord(hostname: "paused.local.")
            ]))])
        ])

        fixture.coordinator.run(operation: "doctor", params: [:], profile: nil)
        fixture.monitor.startMonitoring()

        XCTAssertEqual(fixture.monitor.state, .paused)
        fixture.runner.finishAll()
        try await waitUntilStoreState { fixture.monitor.state == .ready }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["capabilities", "validate-install", "doctor", "discover"])
    }

    func testDeviceOperationDoesNotPauseAppDiscoveryRefresh() async throws {
        let fixture = try await makeReadyFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                    testDoctorCheck(status: "PASS", message: "ok", domain: "Runtime")
                ]))
            ], pauseAfterEvents: true),
            .init(events: [BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [
                testDeviceRecord(hostname: "parallel.local.")
            ]))])
        ])
        let context = DeviceRuntimeContext(
            profileID: "device-one",
            configURL: URL(fileURLWithPath: "/tmp/device-one/.env")
        )

        fixture.coordinator.run(
            operation: "doctor",
            context: context,
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )
        try await waitUntilStoreState {
            fixture.coordinator.lane(for: .device("device-one")).backend.isRunning &&
            fixture.runner.calls.map(\.operation) == ["capabilities", "validate-install", "doctor"]
        }
        fixture.monitor.startMonitoring()

        XCTAssertNotEqual(fixture.monitor.state, .paused)
        try await waitUntilStoreState { fixture.runner.calls.count == 4 }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["capabilities", "validate-install", "doctor", "discover"])
        try await waitUntilStoreState { fixture.monitor.state == .ready }
        fixture.runner.finishAll()
    }

    func testReadinessBlockedPreventsDiscovery() async throws {
        let temp = try TemporaryDirectory()
        let runner = StoreTestRunner(responses: [])
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let readiness = AppReadinessStore(
            backend: backend,
            runtimeResolver: DiscoveryMonitorTestRuntimeResolver(issues: [
                BundleRuntimeIssue(
                    code: .distributionRootMissing,
                    severity: .error,
                    message: "missing distribution",
                    recovery: "reinstall"
                )
            ]),
            helperPathProvider: { "" }
        )
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let monitor = DeviceDiscoveryStore(coordinator: coordinator, readinessStore: readiness, registry: registry)

        monitor.startMonitoring()
        readiness.start()

        try await waitUntilStoreState { monitor.state == .readinessBlocked }
        XCTAssertEqual(monitor.state, .readinessBlocked)
        XCTAssertEqual(runner.calls, [])
    }

    private struct Fixture {
        let runner: PausingStoreTestRunner
        let coordinator: OperationCoordinator
        let readiness: AppReadinessStore
        let registry: DeviceRegistryStore
        let monitor: DeviceDiscoveryStore
    }

    private func makeFixture(responses: [StoreTestRunner.Response]) async throws -> Fixture {
        let temp = try TemporaryDirectory()
        let runner = PausingStoreTestRunner(responses: responses)
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let readiness = AppReadinessStore(
            backend: backend,
            runtimeResolver: DiscoveryMonitorTestRuntimeResolver(),
            helperPathProvider: { "" }
        )
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let monitor = DeviceDiscoveryStore(coordinator: coordinator, readinessStore: readiness, registry: registry)
        return Fixture(
            runner: runner,
            coordinator: coordinator,
            readiness: readiness,
            registry: registry,
            monitor: monitor
        )
    }

    private func makeReadyFixture(responses: [StoreTestRunner.Response]) async throws -> Fixture {
        let fixture = try await makeFixture(responses: [
            .init(events: [BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())]),
            .init(events: [BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload())])
        ] + responses)
        fixture.readiness.start()
        try await waitUntilStoreState { fixture.readiness.state.kind == .ready }
        return fixture
    }

    private func capabilitiesPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "api_schema_version": .number(1),
            "helper_version": .string("1.2.3"),
            "helper_version_code": .number(123),
            "operations": .array([.string("discover"), .string("validate-install")]),
            "distribution_root": .string("/bundle/Distribution"),
            "artifact_manifest_sha256": .string("abc"),
            "confirmation_schema_version": .number(1),
            "summary": .string("Helper capabilities resolved.")
        ])
    }

    private func validationPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "ok": .bool(true),
            "checks": .array([
                .object([
                    "id": .string("distribution_root"),
                    "ok": .bool(true),
                    "message": .string("distribution root is valid")
                ])
            ]),
            "counts": .object([
                "checks": .number(1),
                "pass": .number(1),
                "fail": .number(0)
            ]),
            "summary": .string("Install validation passed.")
        ])
    }
}

private struct DiscoveryMonitorTestRuntimeResolver: AppRuntimeResolving {
    var issues: [BundleRuntimeIssue] = []

    func resolve(helperPath: String?) throws -> HelperResolution {
        HelperResolution(
            executableURL: URL(fileURLWithPath: "/bundle/Contents/Helpers/tcapsule"),
            distributionRootURL: URL(fileURLWithPath: "/bundle/Contents/Resources/Distribution", isDirectory: true),
            toolsBinURL: URL(fileURLWithPath: "/bundle/Contents/Resources/Tools/bin", isDirectory: true),
            mode: .productionBundle,
            attemptedPaths: ["/bundle/Contents/Helpers/tcapsule"]
        )
    }

    func runtimeIssues(for resolution: HelperResolution) -> [BundleRuntimeIssue] {
        issues
    }
}
