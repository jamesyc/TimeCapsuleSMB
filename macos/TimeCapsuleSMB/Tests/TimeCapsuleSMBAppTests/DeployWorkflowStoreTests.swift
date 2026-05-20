import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeployWorkflowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeployWorkflowState.allCases, [
            .idle,
            .planning,
            .planReady,
            .planStale,
            .planFailed,
            .deploying,
            .awaitingConfirmation,
            .deployed,
            .deployFailed
        ])
    }

    func testInvalidMountWaitMovesToPlanFailedWithoutRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "1.5"

        store.runPlan(password: "pw")

        XCTAssertEqual(store.state, .planFailed)
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(runner.calls, [])
    }

    func testPlanSendsDryRunParamsAndMovesToPlanReady() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "build_deployment_plan", risk: "local_read", cancellable: true),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "45"
        store.noReboot = true
        store.noWait = true
        store.nbnsEnabled = false
        store.debugLogging = true

        store.runPlan(password: "pw")

        XCTAssertEqual(store.state, .planning)
        try await waitUntilStoreState { store.state == .planReady }
        XCTAssertEqual(store.currentStage?.stage, "build_deployment_plan")
        XCTAssertEqual(store.plan?.payloadDir, "/Volumes/dk2/.samba4")
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertEqual(runner.calls[0].operation, "deploy")
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["no_reboot"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["nbns_enabled"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["debug_logging"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["mount_wait"], .number(45))
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
    }

    func testMalformedPlanPayloadMovesToPlanFailed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "")

        try await waitUntilStoreState { store.state == .planFailed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testDeployBeforePlanMarksPlanStaleWithoutRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        XCTAssertFalse(store.canDeploy)
        store.runDeploy(password: "pw")

        XCTAssertEqual(store.state, .planStale)
        XCTAssertEqual(store.error?.code, "plan_stale")
        XCTAssertEqual(runner.calls, [])
    }

    func testOptionChangeAfterPlanMarksPlanStale() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }

        store.noWait = true

        XCTAssertEqual(store.state, .planStale)
        XCTAssertFalse(store.canDeploy)
    }

    func testDeploySendsRunParamsFromPlanOptionsAndStoresResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "30"

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw2")

        XCTAssertEqual(store.state, .deploying)
        try await waitUntilStoreState { store.state == .deployed }
        XCTAssertEqual(store.currentStage?.stage, "upload_payload")
        XCTAssertEqual(store.result?.verified, true)
        XCTAssertEqual(runner.calls.count, 2)
        XCTAssertEqual(runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(runner.calls[1].params["mount_wait"], .number(30))
        XCTAssertEqual(runner.calls[1].params["credentials"], .object(["password": .string("pw2")]))
    }

    func testConfirmationRequiredMovesToAwaitingConfirmationThenConfirmedDeployCompletes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "confirmation_required",
                    message: "Confirm deployment.",
                    details: .object([
                        "title": .string("Confirm deployment"),
                        "message": .string("Deploy and reboot."),
                        "action_title": .string("Deploy"),
                        "confirmation_id": .string("confirm-1")
                    ])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "pre_upload_actions", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let backend = BackendClient(runner: runner)
        let store = DeployWorkflowStore(backend: backend)

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .awaitingConfirmation && backend.pendingConfirmation != nil }

        backend.confirmPending()

        try await waitUntilStoreState { store.state == .deployed }
        XCTAssertEqual(store.currentStage?.stage, "pre_upload_actions")
        XCTAssertEqual(runner.calls.count, 3)
        XCTAssertEqual(runner.calls[2].params["confirmation_id"], .string("confirm-1"))
    }

    func testDeployBackendErrorMovesToDeployFailedWithRecovery() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "remote_error",
                    message: "No HFS volumes found.",
                    recovery: recoveryValue(title: "No HFS volumes found", actions: ["Wake the disk."], suggestedOperation: "deploy")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployFailed }
        XCTAssertEqual(store.error?.code, "remote_error")
        XCTAssertEqual(store.error?.recovery?.title, "No HFS volumes found")
    }

    func testFalseDeployResultMovesToDeployFailed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: false, payload: .object(["summary": .string("deployment failed.")]))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployFailed }
        XCTAssertEqual(store.error?.message, "deployment failed.")
    }

    func testClearResetsDeployState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.clear()

        XCTAssertEqual(store.state, .idle)
        XCTAssertNil(store.plan)
        XCTAssertNil(store.result)
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
        XCTAssertNil(store.plannedOptions)
    }

    private func deployPlanPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "host": .string("root@10.0.0.2"),
            "volume_root": .string("/Volumes/dk2"),
            "payload_dir": .string("/Volumes/dk2/.samba4"),
            "payload_family": .string("netbsd6_samba4"),
            "netbsd4": .bool(false),
            "requires_reboot": .bool(true),
            "reboot_required": .bool(true),
            "uploads": .array([.object(["description": .string("smbd")])]),
            "pre_upload_actions": .array([.object(["type": .string("stop_process")])]),
            "post_upload_actions": .array([]),
            "activation_actions": .array([]),
            "post_deploy_checks": .array([
                .object(["id": .string("ssh_returns_after_reboot"), "description": .string("SSH returns after reboot")])
            ]),
            "summary": .string("deployment dry-run plan generated.")
        ])
    }

    private func deployResultPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "payload_dir": .string("/Volumes/dk2/.samba4"),
            "netbsd4": .bool(false),
            "payload_family": .string("netbsd6_samba4"),
            "requires_reboot": .bool(true),
            "rebooted": .bool(true),
            "reboot_requested": .bool(true),
            "waited": .bool(true),
            "verified": .bool(true),
            "summary": .string("deployment completed.")
        ])
    }
}
