import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ConnectionWorkflowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(ConnectionWorkflowState.allCases, [
            .idle,
            .discovering,
            .discoveryReady,
            .discoveryEmpty,
            .discoveryFailed,
            .configuring,
            .configured,
            .configureFailed
        ])
    }

    func testInvalidDiscoverTimeoutMovesToDiscoveryFailedWithoutRunningHelper() {
        let runner = WorkflowRecordingRunner(responses: [])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.bonjourTimeout = "bad"

        store.runDiscover()

        XCTAssertEqual(store.state, .discoveryFailed)
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(runner.calls, [])
    }

    func testDiscoverSingleDeviceAutoSelectsAndRecordsStage() async throws {
        let record = deviceRecord(name: "TC", ipv4: ["10.0.0.2"], syap: "119")
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "discover", stage: "bonjour_discovery", risk: "local_read", cancellable: true),
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: [record]))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.bonjourTimeout = "0.25"

        store.runDiscover()

        XCTAssertEqual(store.state, .discovering)
        try await waitUntil { store.state == .discoveryReady }
        XCTAssertEqual(store.currentStage?.stage, "bonjour_discovery")
        XCTAssertEqual(store.devices.count, 1)
        XCTAssertEqual(store.devices[0].name, "TC")
        XCTAssertEqual(store.devices[0].syap, "119")
        XCTAssertEqual(store.selectedDeviceID, store.devices[0].id)
        XCTAssertEqual(runner.calls.first?.operation, "discover")
        XCTAssertEqual(runner.calls.first?.params["timeout"], .number(0.25))
    }

    func testDiscoverEmptyResultMovesToDiscoveryEmpty() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: []))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()

        try await waitUntil { store.state == .discoveryEmpty }
        XCTAssertEqual(store.devices, [])
        XCTAssertNil(store.selectedDeviceID)
    }

    func testDiscoverMultipleDevicesRequiresExplicitSelection() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: [
                    deviceRecord(name: "TC One", ipv4: ["10.0.0.2"], syap: "119"),
                    deviceRecord(name: "TC Two", ipv4: ["10.0.0.3"], syap: "120")
                ]))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()

        try await waitUntil { store.state == .discoveryReady }
        XCTAssertEqual(store.devices.count, 2)
        XCTAssertNil(store.selectedDeviceID)

        store.select(store.devices[1])

        XCTAssertEqual(store.selectedDeviceID, store.devices[1].id)
        XCTAssertEqual(store.selectedDevice?.name, "TC Two")
    }

    func testDiscoverBackendErrorMovesToDiscoveryFailedWithRecovery() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "discover",
                    code: "operation_failed",
                    message: "Bonjour failed.",
                    recovery: recovery(title: "Discovery failed", actions: ["Retry discovery."])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()

        try await waitUntil { store.state == .discoveryFailed }
        XCTAssertEqual(store.error?.message, "Bonjour failed.")
        XCTAssertEqual(store.error?.recovery?.title, "Discovery failed")
        XCTAssertEqual(store.error?.recovery?.actions, ["Retry discovery."])
    }

    func testMalformedDiscoverPayloadMovesToDiscoveryFailed() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "discover",
                    ok: true,
                    payload: .object(["schema_version": .string("wrong")])
                )
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()

        try await waitUntil { store.state == .discoveryFailed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testConfigureRejectsMissingPasswordWithoutRunningHelper() {
        let runner = WorkflowRecordingRunner(responses: [])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.manualHost = "root@10.0.0.2"

        store.runConfigure(password: " ")

        XCTAssertEqual(store.state, .configureFailed)
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(runner.calls, [])
    }

    func testConfigureRejectsMissingTargetWithoutRunningHelper() {
        let runner = WorkflowRecordingRunner(responses: [])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runConfigure(password: "pw")

        XCTAssertEqual(store.state, .configureFailed)
        XCTAssertEqual(store.error?.message, "Choose a discovered device or enter a host.")
        XCTAssertEqual(runner.calls, [])
    }

    func testConfigureSelectedDeviceSendsSelectedRecordAndStoresResult() async throws {
        let record = deviceRecord(name: "TC", ipv4: ["10.0.0.2"], syap: "119")
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: [record]))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "configure", stage: "ssh_probe", risk: "remote_read", cancellable: true),
                BackendEvent(type: "result", operation: "configure", ok: true, payload: configurePayload(host: "root@10.0.0.2"))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()
        try await waitUntil { store.state == .discoveryReady }
        store.runConfigure(password: "pw")

        XCTAssertEqual(store.state, .configuring)
        try await waitUntil { store.state == .configured }
        XCTAssertEqual(store.currentStage?.stage, "ssh_probe")
        XCTAssertEqual(store.configuredDevice?.host, "root@10.0.0.2")
        XCTAssertEqual(store.configuredDevice?.sshAuthenticated, true)
        XCTAssertEqual(runner.calls.count, 2)
        XCTAssertNil(runner.calls[1].params["host"])
        XCTAssertEqual(runner.calls[1].params["selected_record"], store.devices[0].rawRecord)
        XCTAssertEqual(runner.calls[1].params["password"], .string("pw"))
    }

    func testConfigureManualHostSendsHostWhenNoDeviceSelected() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: configurePayload(host: "root@10.0.0.9"))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.manualHost = " root@10.0.0.9 "
        store.debugLogging = true

        store.runConfigure(password: "pw")

        try await waitUntil { store.state == .configured }
        XCTAssertEqual(runner.calls.first?.operation, "configure")
        XCTAssertEqual(runner.calls.first?.params["host"], .string("root@10.0.0.9"))
        XCTAssertNil(runner.calls.first?.params["selected_record"])
        XCTAssertEqual(runner.calls.first?.params["debug_logging"], .bool(true))
    }

    func testConfigureAuthFailurePreservesDiscoverySelectionAndShowsRecovery() async throws {
        let record = deviceRecord(name: "TC", ipv4: ["10.0.0.2"], syap: "119")
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: [record]))
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "configure",
                    code: "auth_failed",
                    message: "The AirPort admin password did not work.",
                    recovery: recovery(title: "AirPort password rejected", actions: ["Re-enter the password."])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()
        try await waitUntil { store.state == .discoveryReady }
        let selectedID = store.selectedDeviceID
        store.runConfigure(password: "bad")

        try await waitUntil { store.state == .configureFailed }
        XCTAssertEqual(store.selectedDeviceID, selectedID)
        XCTAssertEqual(store.devices.count, 1)
        XCTAssertEqual(store.error?.code, "auth_failed")
        XCTAssertEqual(store.error?.recovery?.title, "AirPort password rejected")
    }

    func testConfigureFalseResultMovesToConfigureFailed() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "configure",
                    ok: false,
                    payload: .object(["summary": .string("configuration failed.")])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.manualHost = "root@10.0.0.2"

        store.runConfigure(password: "pw")

        try await waitUntil { store.state == .configureFailed }
        XCTAssertEqual(store.error?.message, "configuration failed.")
    }

    func testMalformedConfigurePayloadMovesToConfigureFailed() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "configure",
                    ok: true,
                    payload: .object(["schema_version": .number(1)])
                )
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))
        store.manualHost = "root@10.0.0.2"

        store.runConfigure(password: "pw")

        try await waitUntil { store.state == .configureFailed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testClearReturnsWorkflowToIdle() async throws {
        let runner = WorkflowRecordingRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: discoverPayload(records: [
                    deviceRecord(name: "TC", ipv4: ["10.0.0.2"], syap: "119")
                ]))
            ])
        ])
        let store = ConnectionWorkflowStore(backend: BackendClient(runner: runner))

        store.runDiscover()
        try await waitUntil { store.state == .discoveryReady }
        store.clear()

        XCTAssertEqual(store.state, .idle)
        XCTAssertEqual(store.devices, [])
        XCTAssertNil(store.selectedDeviceID)
        XCTAssertNil(store.configuredDevice)
        XCTAssertNil(store.error)
        XCTAssertEqual(store.events.count, 0)
    }

    private func waitUntil(
        timeoutNanoseconds: UInt64 = 2_000_000_000,
        _ condition: @escaping @MainActor () -> Bool
    ) async throws {
        let start = DispatchTime.now().uptimeNanoseconds
        while !condition() {
            if DispatchTime.now().uptimeNanoseconds - start > timeoutNanoseconds {
                XCTFail("Timed out waiting for connection workflow state change.")
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }

    private func deviceRecord(name: String, ipv4: [String], syap: String) -> JSONValue {
        .object([
            "name": .string(name),
            "hostname": .string("\(name.lowercased().replacingOccurrences(of: " ", with: "-")).local."),
            "service_type": .string("_airport._tcp.local."),
            "port": .number(5009),
            "ipv4": .array(ipv4.map(JSONValue.string)),
            "ipv6": .array([]),
            "services": .array([.string("_airport._tcp.local.")]),
            "properties": .object(["syAP": .string(syap)]),
            "fullname": .string("\(name)._airport._tcp.local.")
        ])
    }

    private func discoverPayload(records: [JSONValue]) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "instances": .array([]),
            "resolved": .array(records),
            "counts": .object([
                "instances": .number(0),
                "resolved": .number(Double(records.count))
            ]),
            "summary": .string("discovered \(records.count) resolved AirPort service(s).")
        ])
    }

    private func configurePayload(host: String) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "config_path": .string("/app/.env"),
            "host": .string(host),
            "configure_id": .string("cfg-1"),
            "ssh_authenticated": .bool(true),
            "device_syap": .string("119"),
            "device_model": .string("Time Capsule"),
            "compatibility": .object([
                "payload_family": .string("netbsd6_samba4"),
                "supported": .bool(true),
                "syap_candidates": .array([.string("119")]),
                "model_candidates": .array([.string("Time Capsule")])
            ]),
            "device": .object([
                "host": .string(host),
                "syap": .string("119"),
                "model": .string("Time Capsule")
            ]),
            "summary": .string("configuration saved and SSH authentication verified.")
        ])
    }

    private func recovery(title: String, actions: [String]) -> JSONValue {
        .object([
            "title": .string(title),
            "message": .string(title),
            "actions": .array(actions.map(JSONValue.string)),
            "retryable": .bool(true),
            "suggested_operation": .string("configure")
        ])
    }
}

private final class WorkflowRecordingRunner: HelperRunning, @unchecked Sendable {
    struct Call: Equatable, Sendable {
        let helperPath: String?
        let operation: String
        let params: [String: JSONValue]
    }

    struct Response: Sendable {
        let events: [BackendEvent]
        let result: HelperRunResult

        init(
            events: [BackendEvent],
            result: HelperRunResult = HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: "")
        ) {
            self.events = events
            self.result = result
        }
    }

    private let queue = DispatchQueue(label: "TimeCapsuleSMBAppTests.WorkflowRecordingRunner")
    private var storedResponses: [Response]
    private var storedCalls: [Call] = []

    init(responses: [Response]) {
        self.storedResponses = responses
    }

    var calls: [Call] {
        queue.sync { storedCalls }
    }

    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        let response = queue.sync {
            storedCalls.append(Call(helperPath: helperPath, operation: operation, params: params))
            if storedResponses.isEmpty {
                return Response(
                    events: [BackendEvent.error(operation: operation, code: "missing_test_response", message: "No test response queued.")],
                    result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
                )
            }
            return storedResponses.removeFirst()
        }

        for event in response.events {
            await onEvent(event)
        }
        return response.result
    }
}
