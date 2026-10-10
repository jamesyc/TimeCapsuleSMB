import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DoctorStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DoctorWorkflowState.allCases, [
            .idle,
            .running,
            .passed,
            .warning,
            .failed,
            .runFailed
        ])
    }

    func testRunSendsDoctorParamsAndPassedResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "doctor", stage: "run_checks", risk: "remote_read", cancellable: true),
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(
                    fatal: false,
                    checks: [
                        check(status: "PASS", message: "smbd is running", domain: "device"),
                        check(status: "INFO", message: "bonjour visible", domain: "Bonjour")
                    ]
                ))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))
        store.skipSSH = true
        store.skipBonjour = true
        store.skipSMB = true

        store.runDoctor(password: "pw")

        XCTAssertEqual(store.state, .running)
        try await waitUntilStoreState { store.state == .passed }
        XCTAssertEqual(store.currentStage?.stage, "run_checks")
        XCTAssertEqual(store.summary?.passCount, 1)
        XCTAssertEqual(store.summary?.infoCount, 1)
        XCTAssertEqual(runner.calls.first?.operation, "doctor")
        XCTAssertEqual(runner.calls.first?.params["skip_ssh"], .bool(true))
        XCTAssertEqual(runner.calls.first?.params["skip_bonjour"], .bool(true))
        XCTAssertEqual(runner.calls.first?.params["skip_smb"], .bool(true))
        XCTAssertEqual(runner.calls.first?.params["credentials"], .object(["password": .string("pw")]))
    }

    func testPublishesWhenBackendFinishesAfterTerminalResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(
                    fatal: false,
                    checks: [check(status: "PASS", message: "runtime ok", domain: "device")]
                ))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))
        let finishPublished = expectation(description: "DoctorStore publishes after backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        store.objectWillChange
            .sink { [weak store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.state == .passed,
                          store?.isRunning == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .passed }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isRunning)
        _ = cancellables
    }

    func testRejectedRunDoesNotEnterRunning() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: .object(["ok": .bool(true)]))
            ], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DoctorStore(coordinator: coordinator)

        _ = coordinator.run(operation: "deploy", profile: nil)
        try await waitUntilStoreState { runner.calls.count == 1 && coordinator.backend.isRunning }
        let result = store.runDoctor(password: "pw")

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.state, .runFailed)
        XCTAssertEqual(store.error?.code, "operation_already_running")
        XCTAssertEqual(runner.calls.count, 1)
        runner.finishAll()
        try await waitUntilStoreState { !store.isRunning }
    }

    func testWarningResultMovesToWarning() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(
                    fatal: false,
                    checks: [check(status: "WARN", message: "NBNS skipped", domain: "Discovery")]
                ))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .warning }
        XCTAssertEqual(store.summary?.warnCount, 1)
    }

    func testFatalPayloadMovesToFailedAndGroupsFatalFirst() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: false, payload: testDoctorPayload(
                    fatal: true,
                    checks: [
                        check(status: "PASS", message: "local tools exist", domain: "Local"),
                        check(status: "FAIL", message: "smbd is not running", domain: "device"),
                        check(status: "WARN", message: "bonjour missing", domain: "Bonjour")
                    ]
                ))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .failed }
        XCTAssertEqual(store.summary?.failCount, 1)
        // Every domain but "computer" is the device's.
        XCTAssertEqual(store.summary.map { DoctorCheckDomainPolicy.signals(from: $0.checks) }?.map(\.domain), [.device])
    }

    func testMissingDomainCountsAsTheDevice() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(
                    fatal: false,
                    checks: [.object([
                        "status": .string("PASS"),
                        "message": .string("config exists"),
                        "details": .object([:])
                    ])]
                ))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .passed }
        XCTAssertEqual(store.summary.map { DoctorCheckDomainPolicy.signals(from: $0.checks) }?.map(\.domain), [.device])
    }

    func testBackendErrorMovesToRunFailedWithRecovery() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "doctor",
                    code: "config_error",
                    message: "missing .env",
                    recovery: recoveryValue(title: "Configuration error", actions: ["Open Connect."], suggestedOperation: "configure")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .runFailed }
        XCTAssertEqual(store.error?.code, "config_error")
        XCTAssertEqual(store.error?.recovery?.suggestedOperation, "configure")
    }

    func testMalformedPayloadMovesToRunFailed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .runFailed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testClearResetsDoctorState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(
                    fatal: false,
                    checks: [check(status: "PASS", message: "ok", domain: "computer")]
                ))
            ])
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")
        try await waitUntilStoreState { store.state == .passed }
        store.clear()

        XCTAssertEqual(store.state, .idle)
        XCTAssertNil(store.payload)
        XCTAssertNil(store.summary)
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
    }

    private func check(status: String, message: String, domain: String) -> JSONValue {
        .object([
            "status": .string(status),
            "message": .string(message),
            "details": .object(["domain": .string(domain)])
        ])
    }
}
