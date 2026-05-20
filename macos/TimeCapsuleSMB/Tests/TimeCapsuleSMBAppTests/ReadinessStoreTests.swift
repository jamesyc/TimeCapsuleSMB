import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ReadinessStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(ReadinessOperationState.allCases, [.idle, .running, .succeeded, .failed])
    }

    func testCapabilitiesSuccessStoresHelperMetadataAndStage() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "capabilities", stage: "summarize_capabilities", risk: "local_read", cancellable: true),
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ])
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runCapabilities()

        XCTAssertEqual(store.capabilitiesState, .running)
        try await waitUntilStoreState { store.capabilitiesState == .succeeded }
        XCTAssertEqual(store.currentStage?.stage, "summarize_capabilities")
        XCTAssertEqual(store.capabilities?.helperVersion, "1.2.3")
        XCTAssertEqual(runner.calls.first?.operation, "capabilities")
    }

    func testPathsSuccessStoresArtifactRows() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "paths", ok: true, payload: pathsPayload())
            ])
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runPaths()

        try await waitUntilStoreState { store.pathsState == .succeeded }
        XCTAssertEqual(store.paths?.artifacts.count, 1)
        XCTAssertEqual(store.paths?.artifacts[0].name, "smbd")
        XCTAssertEqual(store.paths?.counts["artifacts"], 1)
    }

    func testValidationSuccessStoresPassCounts() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runValidateInstall()

        try await waitUntilStoreState { store.validationState == .succeeded }
        XCTAssertEqual(store.validation?.counts["pass"], 1)
        XCTAssertNil(store.error)
    }

    func testValidationFailureStoresPayloadWithoutTransportError() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: false, payload: validationPayload(ok: false))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runValidateInstall()

        try await waitUntilStoreState { store.validationState == .failed }
        XCTAssertEqual(store.validation?.ok, false)
        XCTAssertEqual(store.validation?.counts["fail"], 1)
        XCTAssertNil(store.error)
    }

    func testBackendErrorFailsOnlyMatchingOperationWithRecovery() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "paths",
                    code: "validation_failed",
                    message: "missing distribution root",
                    recovery: recoveryValue(title: "Deployment validation failed", actions: ["Open Readiness."], suggestedOperation: "validate-install")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runPaths()

        try await waitUntilStoreState { store.pathsState == .failed }
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(store.error?.recovery?.title, "Deployment validation failed")
        XCTAssertEqual(store.capabilitiesState, .idle)
        XCTAssertEqual(store.validationState, .idle)
    }

    func testMalformedPayloadFailsContract() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runCapabilities()

        try await waitUntilStoreState { store.capabilitiesState == .failed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testClearResetsReadinessState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ])
        ])
        let store = ReadinessStore(backend: BackendClient(runner: runner))

        store.runCapabilities()
        try await waitUntilStoreState { store.capabilitiesState == .succeeded }
        store.clear()

        XCTAssertEqual(store.capabilitiesState, .idle)
        XCTAssertEqual(store.pathsState, .idle)
        XCTAssertEqual(store.validationState, .idle)
        XCTAssertNil(store.capabilities)
        XCTAssertNil(store.paths)
        XCTAssertNil(store.validation)
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
    }

    private func capabilitiesPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "api_schema_version": .number(1),
            "helper_version": .string("1.2.3"),
            "helper_version_code": .number(123),
            "operations": .array([.string("discover"), .string("configure")]),
            "distribution_root": .string("/repo"),
            "artifact_manifest_sha256": .string("abc"),
            "confirmation_schema_version": .number(1),
            "summary": .string("helper capabilities resolved.")
        ])
    }

    private func pathsPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "distribution_root": .string("/repo"),
            "config_path": .string("/app/.env"),
            "state_dir": .string("/app"),
            "package_root": .string("/repo/src/timecapsulesmb"),
            "artifact_manifest": .string("/repo/src/timecapsulesmb/assets/artifact-manifest.json"),
            "artifacts": .array([
                .object([
                    "name": .string("smbd"),
                    "repo_relative_path": .string("bin/samba4/smbd"),
                    "absolute_path": .string("/repo/bin/samba4/smbd"),
                    "sha256": .string("hash"),
                    "ok": .bool(true),
                    "message": .string("ok")
                ])
            ]),
            "counts": .object(["artifacts": .number(1)]),
            "summary": .string("resolved app paths with 1 artifact path(s).")
        ])
    }

    private func validationPayload(ok: Bool) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "ok": .bool(ok),
            "checks": .array([
                .object([
                    "id": .string(ok ? "python_modules" : "artifact_hashes"),
                    "ok": .bool(ok),
                    "message": .string(ok ? "required Python modules import" : "artifact validation failed")
                ])
            ]),
            "counts": .object([
                "checks": .number(1),
                "pass": .number(ok ? 1 : 0),
                "fail": .number(ok ? 0 : 1)
            ]),
            "summary": .string(ok ? "install validation passed." : "install validation failed.")
        ])
    }
}
