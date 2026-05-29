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
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload(
                    fatal: false,
                    checks: [
                        check(status: "PASS", message: "smbd is running", domain: "Runtime"),
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

    func testRejectedRunDoesNotEnterRunning() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: .object(["ok": .bool(true)]))
            ], delayNanoseconds: 100_000_000)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DoctorStore(coordinator: coordinator)

        _ = coordinator.run(operation: "deploy", profile: nil)
        try await waitUntilStoreState { runner.calls.count == 1 }
        let result = store.runDoctor(password: "pw")

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.state, .runFailed)
        XCTAssertEqual(store.error?.code, "operation_already_running")
        XCTAssertEqual(runner.calls.count, 1)
        try await waitUntilStoreState { !store.isRunning }
    }

    func testWarningResultMovesToWarning() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload(
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
                BackendEvent(type: "result", operation: "doctor", ok: false, payload: doctorPayload(
                    fatal: true,
                    checks: [
                        check(status: "PASS", message: "local tools exist", domain: "Local"),
                        check(status: "FAIL", message: "smbd is not running", domain: "Runtime"),
                        check(status: "WARN", message: "bonjour missing", domain: "Bonjour")
                    ]
                ))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DoctorStore(backend: BackendClient(runner: runner))

        store.runDoctor(password: "")

        try await waitUntilStoreState { store.state == .failed }
        XCTAssertEqual(store.summary?.failCount, 1)
        XCTAssertEqual(store.summary?.groups.first?.domain, "Runtime")
    }

    func testMissingDomainGroupsAsGeneral() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload(
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
        XCTAssertEqual(store.summary?.groups.first?.domain, "General")
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
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload(
                    fatal: false,
                    checks: [check(status: "PASS", message: "ok", domain: "General")]
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

    private func doctorPayload(fatal: Bool, checks: [JSONValue]) -> JSONValue {
        let pass = checks.filter { $0.stringValue(for: "status") == "PASS" }.count
        let warn = checks.filter { $0.stringValue(for: "status") == "WARN" }.count
        let fail = checks.filter { $0.stringValue(for: "status") == "FAIL" }.count
        let info = checks.filter { $0.stringValue(for: "status") == "INFO" }.count
        return .object([
            "schema_version": .number(1),
            "fatal": .bool(fatal),
            "results": .array(checks),
            "counts": .object([
                "PASS": .number(Double(pass)),
                "WARN": .number(Double(warn)),
                "FAIL": .number(Double(fail)),
                "INFO": .number(Double(info))
            ]),
            "error": fatal ? .string("doctor failed") : .null,
            "summary": .string(fatal ? "Doctor found one or more fatal problems." : "Doctor checks passed.")
        ])
    }

    private func check(status: String, message: String, domain: String) -> JSONValue {
        .object([
            "status": .string(status),
            "message": .string(message),
            "details": .object(["domain": .string(domain)])
        ])
    }
}
