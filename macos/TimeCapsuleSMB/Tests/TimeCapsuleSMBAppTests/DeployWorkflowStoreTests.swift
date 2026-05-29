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
        XCTAssertEqual(store.error?.code, "mount_wait_invalid")
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
        store.noWait = true
        store.nbnsEnabled = false
        store.internalShareUseDiskRoot = true
        store.anyProtocol = true
        store.debugLogging = true
        store.ataIdleSeconds = "0"
        store.ataStandby = "0"

        store.runPlan(password: "pw")

        XCTAssertEqual(store.state, .planning)
        try await waitUntilStoreState { store.state == .planReady }
        XCTAssertEqual(store.currentStage?.stage, "build_deployment_plan")
        XCTAssertEqual(store.plan?.payloadDir, "/Volumes/dk2/.samba4")
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertEqual(runner.calls[0].operation, "deploy")
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["no_reboot"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["nbns_enabled"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["any_protocol"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["debug_logging"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(runner.calls[0].params["ata_standby"], .number(0))
        XCTAssertEqual(runner.calls[0].params["mount_wait"], .number(45))
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
    }

    func testInvalidAtaOptionsMoveToPlanFailedWithoutRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.ataIdleSeconds = "bad"
        store.runPlan(password: "pw")

        XCTAssertEqual(store.state, .planFailed)
        XCTAssertEqual(store.error?.code, "ata_idle_seconds_invalid")
        XCTAssertEqual(store.error?.message, "ATA idle seconds must be a non-negative integer.")
        XCTAssertEqual(runner.calls, [])

        store.ataIdleSeconds = "300"
        store.ataStandby = "bad"
        store.runPlan(password: "pw")

        XCTAssertEqual(store.state, .planFailed)
        XCTAssertEqual(store.error?.code, "ata_standby_invalid")
        XCTAssertEqual(store.error?.message, "ATA standby seconds must be blank or a non-negative integer.")
        XCTAssertEqual(runner.calls, [])
    }

    func testNoRebootAndNoWaitAreMutuallyExclusive() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.noWait = true

        XCTAssertTrue(store.noWait)
        XCTAssertFalse(store.noReboot)
        XCTAssertFalse(DeployExecutionOptionPolicy.allowsNoReboot(noWait: store.noWait))
        XCTAssertTrue(DeployExecutionOptionPolicy.allowsNoWait(noReboot: store.noReboot))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }

        XCTAssertEqual(runner.calls[0].params["no_reboot"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(true))

        store.noReboot = true

        XCTAssertTrue(store.noReboot)
        XCTAssertFalse(store.noWait)
        XCTAssertTrue(DeployExecutionOptionPolicy.allowsNoReboot(noWait: store.noWait))
        XCTAssertFalse(DeployExecutionOptionPolicy.allowsNoWait(noReboot: store.noReboot))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { runner.calls.count == 2 && store.state == .planReady }

        XCTAssertEqual(runner.calls[1].params["no_reboot"], .bool(true))
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(false))
    }

    func testRejectedPlanDoesNotEnterPlanning() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ], delayNanoseconds: 100_000_000)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeployWorkflowStore(coordinator: coordinator)

        _ = coordinator.run(operation: "doctor", profile: nil)
        try await waitUntilStoreState { runner.calls.count == 1 }
        let result = store.runPlan(password: "pw")

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.state, .planFailed)
        XCTAssertEqual(store.error?.code, "operation_already_running")
        XCTAssertEqual(runner.calls.count, 1)
        try await waitUntilStoreState { !store.isRunning }
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
        XCTAssertEqual(store.error?.code, "deploy_plan_stale")
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

        store.internalShareUseDiskRoot = true

        XCTAssertEqual(store.state, .planStale)
        XCTAssertFalse(store.canDeploy)

        store.internalShareUseDiskRoot = false

        XCTAssertEqual(store.state, .planReady)
        XCTAssertTrue(store.canDeploy)
    }

    func testOptionChangeWhilePlanningMakesReturnedPlanStaleAndAllowsRegeneration() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ], delayNanoseconds: 50_000_000),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { runner.calls.count == 1 }
        XCTAssertEqual(store.state, .planning)

        store.noWait = true

        try await waitUntilStoreState { store.state == .planStale }
        XCTAssertFalse(store.canDeploy)
        XCTAssertNotNil(store.plan)
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(false))

        store.runPlan(password: "pw")

        try await waitUntilStoreState { store.state == .planReady && runner.calls.count == 2 }
        XCTAssertTrue(store.canDeploy)
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(true))
    }

    func testDefaultRuntimeOverridesAreSentExplicitlyInPlanParams() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }

        XCTAssertEqual(runner.calls[0].params["internal_share_use_disk_root"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["any_protocol"], .bool(false))
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
        store.internalShareUseDiskRoot = true
        store.anyProtocol = true

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
        XCTAssertEqual(runner.calls[1].params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(runner.calls[1].params["any_protocol"], .bool(true))
        XCTAssertEqual(runner.calls[1].params["credentials"], .object(["password": .string("pw2")]))
    }

    func testDeployCannotRunAgainDirectlyFromDeployedState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed }

        let result = store.runDeploy(password: "pw2")

        XCTAssertEqual(result.rejectionMessage, "Deploy plan is not ready.")
        XCTAssertEqual(store.state, .deployed)
        XCTAssertEqual(runner.calls.count, 2)
    }

    func testReinstallCreatesFreshPlanFromDeployedState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployPlanPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed }

        store.noWait = true
        store.runPlan(password: "pw2")

        try await waitUntilStoreState { store.state == .planReady && runner.calls.count == 3 }
        XCTAssertNil(store.result)
        XCTAssertEqual(runner.calls[2].operation, "deploy")
        XCTAssertEqual(runner.calls[2].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[2].params["no_wait"], .bool(true))
        XCTAssertEqual(runner.calls[2].params["credentials"], .object(["password": .string("pw2")]))
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

    func testCancellingDeployConfirmationRestoresReadyPlan() async throws {
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
                    details: .object(["confirmation_id": .string("confirm-1")])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let backend = BackendClient(runner: runner)
        let store = DeployWorkflowStore(backend: backend)

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .awaitingConfirmation && backend.pendingConfirmation != nil }

        backend.cancelPendingConfirmation()

        try await waitUntilStoreState { store.state == .planReady && backend.pendingConfirmation == nil }
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
        XCTAssertTrue(store.canDeploy)
        XCTAssertNotNil(store.plan)
        XCTAssertEqual(runner.calls.count, 2)
    }

    func testCancellingDeployConfirmationRestoresStalePlanWhenOptionsChanged() async throws {
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
                    details: .object(["confirmation_id": .string("confirm-1")])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let backend = BackendClient(runner: runner)
        let store = DeployWorkflowStore(backend: backend)

        store.runPlan(password: "pw")
        try await waitUntilStoreState { store.state == .planReady }
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .awaitingConfirmation && backend.pendingConfirmation != nil }

        store.noWait = true
        backend.cancelPendingConfirmation()

        try await waitUntilStoreState { store.state == .planStale && backend.pendingConfirmation == nil }
        XCTAssertNil(store.error)
        XCTAssertFalse(store.canDeploy)
        XCTAssertNotNil(store.plan)
        XCTAssertEqual(runner.calls.count, 2)
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
            "startup_mode": .string("reboot_then_verify"),
            "uploads": .array([.object(["description": .string("smbd")])]),
            "pre_upload_actions": .array([.object(["type": .string("stop_process")])]),
            "post_upload_actions": .array([]),
            "activation_actions": .array([]),
            "post_deploy_checks": .array([
                .object(["id": .string("ssh_returns_after_reboot"), "description": .string("SSH returns after reboot")])
            ]),
            "summary": .string("Deployment dry-run plan generated.")
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
            "summary": .string("Deployment completed.")
        ])
    }
}
