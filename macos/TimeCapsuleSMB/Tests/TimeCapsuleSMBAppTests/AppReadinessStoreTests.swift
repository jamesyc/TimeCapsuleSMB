import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppReadinessStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(
            AppReadinessStateKind.allCases,
            [.idle, .resolvingBundle, .checkingVersion, .checkingCapabilities, .validatingInstall, .ready, .degraded, .blocked]
        )
    }

    func testStateTitlesAreLocalized() {
        XCTAssertEqual(AppReadinessStateKind.allCases.map(\.title), [
            "Idle",
            "Preparing app runtime",
            "Checking version",
            "Checking helper",
            "Validating bundled files",
            "Ready",
            "Degraded",
            "Blocked"
        ])
    }

    func testSuccessfulReadinessRunsCapabilitiesThenValidation() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "capabilities", stage: "summarize_capabilities"),
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "validate-install", stage: "validate_install"),
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(runner: runner)

        store.start()

        XCTAssertEqual(store.state.kind, .checkingCapabilities)
        try await waitUntilStoreState { store.state.kind == .ready }
        XCTAssertEqual(runner.calls.map(\.operation), ["capabilities", "validate-install"])
        XCTAssertEqual(store.currentStage?.stage, "validate_install")
        guard case .ready(let summary) = store.state else {
            return XCTFail("Expected ready state.")
        }
        XCTAssertEqual(summary.runtimeMode, .productionBundle)
        XCTAssertEqual(summary.helperVersion, "1.2.3")
        XCTAssertEqual(summary.distributionRoot, "/bundle/Distribution")
        XCTAssertEqual(summary.validationCounts["pass"], 1)
    }

    func testReadinessVersionCheckRunsBeforeCapabilitiesWhenConfigured() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: false))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(
            runner: runner,
            versionCheck: AppReadinessVersionCheck(url: "https://example.invalid/version.json")
        )

        store.start()

        try await waitUntilStoreState { store.state.kind == .ready }
        XCTAssertEqual(runner.calls.map(\.operation), ["version-check", "capabilities", "validate-install"])
        XCTAssertEqual(runner.calls.first?.params["url"], .string("https://example.invalid/version.json"))
        XCTAssertNil(runner.calls.first?.params["local_version_code"])
    }

    func testBlockingVersionCheckStopsReadinessBeforeCapabilities() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: true))
            ])
        ])
        let store = makeStore(
            runner: runner,
            versionCheck: AppReadinessVersionCheck(url: "https://example.invalid/version.json")
        )

        store.start()

        try await waitUntilStoreState { store.state.kind == .blocked }
        XCTAssertEqual(runner.calls.map(\.operation), ["version-check"])
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .unsupportedVersion)
        XCTAssertEqual(issue.message, "Please update.")
        XCTAssertEqual(issue.recovery, "Download the latest version from https://example.invalid/download.")
    }

    func testUnavailableVersionMetadataDegradesButFailsOpenToReadinessChecks() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: false, source: "unavailable"))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(
            runner: runner,
            versionCheck: AppReadinessVersionCheck(url: "")
        )

        store.start()

        try await waitUntilStoreState { store.state.kind == .degraded }
        XCTAssertEqual(runner.calls.map(\.operation), ["version-check", "capabilities", "validate-install"])
        XCTAssertEqual(runner.calls.first?.params, [:])
        XCTAssertEqual(store.versionCheckPayload?.source, "unavailable")
        XCTAssertTrue(store.issues.contains(where: { $0.code == .versionMetadataUnavailable && $0.severity == .warning }))
    }

    func testVersionCheckErrorDegradesButContinuesReadiness() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "version-check", code: "network_failed", message: "offline")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(
            runner: runner,
            versionCheck: AppReadinessVersionCheck(url: "https://example.invalid/version.json")
        )

        store.start()

        try await waitUntilStoreState { store.state.kind == .degraded }
        XCTAssertEqual(runner.calls.map(\.operation), ["version-check", "capabilities", "validate-install"])
        XCTAssertTrue(store.issues.contains(where: { $0.code == .versionMetadataUnavailable && $0.message == "offline" }))
    }

    func testValidationFailureBlocksApp() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: false, payload: validationPayload(ok: false))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = makeStore(runner: runner)

        store.start()

        try await waitUntilStoreState { store.state.kind == .blocked }
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .installValidationFailed)
        XCTAssertEqual(store.validation?.ok, false)
    }

    func testRuntimeWarningProducesDegradedStateAfterValidationSuccess() async throws {
        let warning = BundleRuntimeIssue(
            code: .toolsDirectoryMissing,
            severity: .warning,
            message: "missing tools",
            recovery: "repair app"
        )
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(runner: runner, issues: [warning])

        store.start()

        try await waitUntilStoreState { store.state.kind == .degraded }
        guard case .degraded(let summary, let issues) = store.state else {
            return XCTFail("Expected degraded state.")
        }
        XCTAssertEqual(summary.helperVersion, "1.2.3")
        XCTAssertEqual(issues, [warning])
    }

    func testRuntimeErrorBlocksBeforeRunningHelper() {
        let issue = BundleRuntimeIssue(
            code: .distributionRootMissing,
            severity: .error,
            message: "missing distribution",
            recovery: "reinstall"
        )
        let runner = StoreTestRunner(responses: [])
        let store = makeStore(runner: runner, issues: [issue])

        store.start()

        XCTAssertEqual(store.state.kind, .blocked)
        XCTAssertEqual(runner.calls, [])
        guard case .blocked(let blockedIssue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(blockedIssue.code, .distributionRootMissing)
    }

    func testResolveFailureBlocksBeforeRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = makeStore(runner: runner, resolveError: NSError(domain: "test", code: 1))

        store.start()

        XCTAssertEqual(store.state.kind, .blocked)
        XCTAssertEqual(runner.calls, [])
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .helperMissing)
    }

    func testMalformedCapabilitiesPayloadBlocksWithContractIssue() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = makeStore(runner: runner)

        store.start()

        try await waitUntilStoreState { store.state.kind == .blocked }
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .contractDecodeFailed)
        XCTAssertEqual(runner.calls.map(\.operation), ["capabilities"])
    }

    func testMalformedValidationPayloadBlocksWithContractIssue() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = makeStore(runner: runner)

        store.start()

        try await waitUntilStoreState { store.state.kind == .blocked }
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .contractDecodeFailed)
        XCTAssertEqual(runner.calls.map(\.operation), ["capabilities", "validate-install"])
    }

    func testHelperLaunchErrorBlocksWithLaunchIssue() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "capabilities", code: "helper_launch_failed", message: "launch failed")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = makeStore(runner: runner)

        store.start()

        try await waitUntilStoreState { store.state.kind == .blocked }
        guard case .blocked(let issue) = store.state else {
            return XCTFail("Expected blocked state.")
        }
        XCTAssertEqual(issue.code, .helperLaunchFailed)
        XCTAssertEqual(issue.message, "launch failed")
    }

    func testUnrelatedEventsDoNotAdvanceReadiness() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ])
        ])
        let store = makeStore(runner: runner)

        store.start()

        try await waitUntilStoreState { !store.backend.isRunning }
        XCTAssertEqual(store.state.kind, .checkingCapabilities)
        XCTAssertNil(store.capabilities)
        XCTAssertNil(store.validation)
    }

    func testClearResetsStateAndPayloads() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "capabilities", ok: true, payload: capabilitiesPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "validate-install", ok: true, payload: validationPayload(ok: true))
            ])
        ])
        let store = makeStore(runner: runner)

        store.start()
        try await waitUntilStoreState { store.state.kind == .ready }
        store.clear()

        XCTAssertEqual(store.state.kind, .idle)
        XCTAssertNil(store.capabilities)
        XCTAssertNil(store.validation)
        XCTAssertNil(store.versionCheckPayload)
        XCTAssertEqual(store.issues, [])
        XCTAssertNil(store.currentStage)
    }

    private func makeStore(
        runner: StoreTestRunner,
        issues: [BundleRuntimeIssue] = [],
        resolveError: Error? = nil,
        versionCheck: AppReadinessVersionCheck? = nil
    ) -> AppReadinessStore {
        let backend = BackendClient(runner: runner)
        let resolver = TestRuntimeResolver(issues: issues, resolveError: resolveError)
        let store = AppReadinessStore(
            backend: backend,
            runtimeResolver: resolver,
            helperPathProvider: { "" }
        )
        store.applyVersionCheck(versionCheck)
        return store
    }

    private func capabilitiesPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "api_schema_version": .number(1),
            "helper_version": .string("1.2.3"),
            "helper_version_code": .number(123),
            "operations": .array([.string("discover"), .string("configure"), .string("validate-install")]),
            "distribution_root": .string("/bundle/Distribution"),
            "artifact_manifest_sha256": .string("abc"),
            "confirmation_schema_version": .number(1),
            "summary": .string("Helper capabilities resolved.")
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
            "summary": .string(ok ? "Install validation passed." : "Install validation failed.")
        ])
    }

    private func versionCheckPayload(shouldBlock: Bool, source: String = "network") -> JSONValue {
        .object([
            "schema_version": .number(1),
            "should_block": .bool(shouldBlock),
            "checked_url": .string("https://example.invalid/version.json"),
            "message": .string("Please update."),
            "download_url": .string("https://example.invalid/download"),
            "local_version_code": .number(20000),
            "current_version": .number(20125),
            "min_supported_version": .number(20125),
            "latest_tag": .string("v2.1.4"),
            "source": .string(source),
            "summary": .string(source == "unavailable" ? "Version metadata is unavailable." : (shouldBlock ? "Update required." : "TimeCapsuleSMB is up to date."))
        ])
    }
}

private struct TestRuntimeResolver: AppRuntimeResolving {
    let issues: [BundleRuntimeIssue]
    let resolveError: Error?

    func resolve(helperPath: String?) throws -> HelperResolution {
        if let resolveError {
            throw resolveError
        }
        return HelperResolution(
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
