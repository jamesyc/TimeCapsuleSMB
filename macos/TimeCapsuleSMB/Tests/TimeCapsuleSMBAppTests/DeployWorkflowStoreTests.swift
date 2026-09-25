import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeployWorkflowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeployWorkflowState.allCases, [
            .idle,
            .deploying,
            .awaitingConfirmation,
            .deployed,
            .deployFailed
        ])
    }

    func testInvalidMountWaitMovesToDeployFailedWithoutRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "1.5"

        store.runDeploy(password: "pw")

        XCTAssertEqual(store.state, .deployFailed)
        XCTAssertEqual(store.error?.code, "mount_wait_invalid")
        XCTAssertEqual(runner.calls, [])
    }

    func testDeploySendsCurrentOptionsWithoutDryRunAndRecordsThem() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "45"
        store.noWait = true
        store.rsyncEnabled = true
        store.internalShareUseDiskRoot = true
        store.smbBrowseCompatibility = true
        store.mdnsAdvertiseAFP = true
        store.anyProtocol = true
        store.requireSMBEncryption = true
        store.forceDisableSMBSigningAndEncryption = true
        store.fruitMetadataNetatalk = true
        store.vfsAIOForkEnabled = true
        store.debugLogging = true
        store.ataIdleSeconds = "0"
        store.ataStandby = "0"

        store.runDeploy(password: "pw")

        XCTAssertEqual(store.state, .deploying)
        // The profile's rsync setting is persisted from the options the run used.
        XCTAssertEqual(store.runOptions?.rsyncEnabled, true)
        XCTAssertEqual(store.runOptions?.noWait, true)
        try await waitUntilStoreState { store.state == .deployed }
        XCTAssertEqual(store.currentStage?.stage, "upload_payload")
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertEqual(runner.calls[0].operation, "deploy")
        XCTAssertNil(runner.calls[0].params["dry_run"])
        XCTAssertNil(runner.calls[0].params["nbns_enabled"])
        XCTAssertNil(runner.calls[0].params["no_reboot"])
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["rsync_enabled"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["mdns_advertise_afp"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["any_protocol"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["require_smb_encryption"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["force_disable_smb_signing_and_encryption"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["fruit_metadata_netatalk"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["vfs_aio_fork_enabled"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["debug_logging"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(runner.calls[0].params["ata_standby"], .number(0))
        XCTAssertEqual(runner.calls[0].params["mount_wait"], .number(45))
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
    }

    func testPublishesWhenBackendFinishesAfterDeployResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        let finishPublished = expectation(description: "DeployWorkflowStore publishes after backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        store.objectWillChange
            .sink { [weak store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.state == .deployed,
                          store?.isBusy == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployed }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isBusy)
        _ = cancellables
    }

    func testInvalidAtaOptionsMoveToDeployFailedWithoutRunningHelper() {
        let runner = StoreTestRunner(responses: [])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.ataIdleSeconds = "bad"
        store.runDeploy(password: "pw")

        XCTAssertEqual(store.state, .deployFailed)
        XCTAssertEqual(store.error?.code, "ata_idle_seconds_invalid")
        XCTAssertEqual(store.error?.message, "ATA idle time must be a non-negative number of seconds.")
        XCTAssertEqual(runner.calls, [])

        store.ataIdleSeconds = "300"
        store.ataStandby = "bad"
        store.runDeploy(password: "pw")

        XCTAssertEqual(store.state, .deployFailed)
        XCTAssertEqual(store.error?.code, "ata_standby_invalid")
        XCTAssertEqual(store.error?.message, "ATA standby time must be blank or a non-negative number of seconds.")
        XCTAssertEqual(runner.calls, [])
    }

    func testNoWaitChangesDeployParamsAndNeverSendsNoReboot() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.noWait = true

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed && !store.isBusy }

        XCTAssertNil(runner.calls[0].params["no_reboot"])
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(true))

        store.noWait = false
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { runner.calls.count == 2 && store.state == .deployed }

        XCTAssertNil(runner.calls[1].params["no_reboot"])
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(false))
    }

    func testRejectedDeployDoesNotEnterDeploying() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeployWorkflowStore(coordinator: coordinator)

        _ = coordinator.run(operation: "doctor", profile: nil)
        try await waitUntilStoreState { runner.calls.count == 1 && coordinator.backend.isRunning }
        let result = store.runDeploy(password: "pw")

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.state, .deployFailed)
        XCTAssertEqual(store.error?.code, "operation_already_running")
        XCTAssertNil(store.runOptions)
        XCTAssertEqual(runner.calls.count, 1)
        runner.finishAll()
        try await waitUntilStoreState { !store.isRunning }
    }

    func testMalformedDeployPayloadMovesToDeployFailed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runDeploy(password: "")

        try await waitUntilStoreState { store.state == .deployFailed }
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
    }

    func testOptionChangeWhileDeployingDoesNotAffectTheRunningDeploy() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ], pauseBeforeEvents: true),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { runner.calls.count == 1 }
        XCTAssertEqual(store.state, .deploying)

        store.noWait = true

        XCTAssertEqual(store.state, .deploying)
        XCTAssertFalse(store.canDeploy)
        XCTAssertEqual(store.runOptions?.noWait, false)
        runner.finishAll()

        try await waitUntilStoreState { store.state == .deployed && !store.isBusy }
        XCTAssertEqual(runner.calls[0].params["no_wait"], .bool(false))
        XCTAssertEqual(store.runOptions?.noWait, false)
        XCTAssertTrue(store.canDeploy)

        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployed && runner.calls.count == 2 }
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(true))
        XCTAssertEqual(store.runOptions?.noWait, true)
    }

    func testDefaultRuntimeOverridesAreSentExplicitly() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        XCTAssertTrue(store.canDeploy)
        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed }

        XCTAssertEqual(runner.calls[0].params["internal_share_use_disk_root"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["smb_browse_compatibility"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["any_protocol"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["rsync_enabled"], .bool(false))
    }

    func testDeploySendsRunParamsAndStoresResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))
        store.mountWait = "30"
        store.internalShareUseDiskRoot = true
        store.smbBrowseCompatibility = true
        store.mdnsAdvertiseAFP = true
        store.anyProtocol = true
        store.fruitMetadataNetatalk = true

        store.runDeploy(password: "pw2")

        XCTAssertEqual(store.state, .deploying)
        try await waitUntilStoreState { store.state == .deployed }
        XCTAssertEqual(store.currentStage?.stage, "upload_payload")
        XCTAssertEqual(store.result?.verified, true)
        XCTAssertEqual(runner.calls.count, 1)
        XCTAssertEqual(runner.calls[0].params["mount_wait"], .number(30))
        XCTAssertEqual(runner.calls[0].params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["any_protocol"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["fruit_metadata_netatalk"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw2")]))
    }

    func testDeployCanRunAgainFromDeployedStateWithNewOptions() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ], pauseBeforeEvents: true)
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed && !store.isBusy }
        XCTAssertNotNil(store.result)

        store.noWait = true
        let result = store.runDeploy(password: "pw2")

        XCTAssertNil(result.rejectionMessage)
        // A new run discards the previous result until the new one arrives.
        XCTAssertEqual(store.state, .deploying)
        XCTAssertNil(store.result)
        runner.finishAll()
        try await waitUntilStoreState { store.state == .deployed && runner.calls.count == 2 }
        XCTAssertNotNil(store.result)
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(true))
        XCTAssertEqual(runner.calls[1].params["credentials"], .object(["password": .string("pw2")]))
    }

    func testConfirmationRequiredMovesToAwaitingConfirmationThenConfirmedDeployCompletes() async throws {
        let runner = StoreTestRunner(responses: [
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

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .awaitingConfirmation && backend.pendingConfirmation != nil && !backend.isRunning }

        backend.confirmPending()

        try await waitUntilStoreState { store.state == .deployed }
        XCTAssertEqual(store.currentStage?.stage, "pre_upload_actions")
        XCTAssertEqual(runner.calls.count, 2)
        XCTAssertEqual(runner.calls[1].params["confirmation_id"], .string("confirm-1"))
    }

    func testCancellingDirectDeployConfirmationReturnsToIdle() async throws {
        let runner = StoreTestRunner(responses: [
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

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .awaitingConfirmation && backend.pendingConfirmation != nil && !backend.isRunning }

        store.noWait = true
        backend.cancelPendingConfirmation()

        try await waitUntilStoreState { store.state == .idle && backend.pendingConfirmation == nil }
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
        XCTAssertNil(store.result)
        XCTAssertTrue(store.canDeploy)
        XCTAssertEqual(runner.calls.count, 1)
    }

    func testDeployBackendErrorMovesToDeployFailedWithRecovery() async throws {
        let runner = StoreTestRunner(responses: [
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

        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployFailed }
        XCTAssertEqual(store.error?.code, "remote_error")
        XCTAssertEqual(store.error?.recovery?.title, "No HFS volumes found")
    }

    func testFalseDeployResultMovesToDeployFailed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: false, payload: .object(["summary": .string("deployment failed.")]))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runDeploy(password: "pw")

        try await waitUntilStoreState { store.state == .deployFailed }
        XCTAssertEqual(store.error?.message, "deployment failed.")
    }

    func testClearResetsDeployState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: deployResultPayload())
            ])
        ])
        let store = DeployWorkflowStore(backend: BackendClient(runner: runner))

        store.runDeploy(password: "pw")
        try await waitUntilStoreState { store.state == .deployed }
        store.clear()

        XCTAssertEqual(store.state, .idle)
        XCTAssertNil(store.result)
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
        XCTAssertNil(store.runOptions)
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
