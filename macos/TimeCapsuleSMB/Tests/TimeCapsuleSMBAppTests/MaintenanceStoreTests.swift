import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class MaintenanceStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(MaintenanceOperationState.allCases, [
            .idle,
            .loading,
            .listReady,
            .planning,
            .planReady,
            .planStale,
            .scanning,
            .scanReady,
            .scanStale,
            .awaitingConfirmation,
            .running,
            .repairing,
            .succeeded,
            .repaired,
            .failed
        ])
        XCTAssertEqual(MaintenanceWorkflow.allCases, [.activate, .uninstall, .fsck, .repairXattrs])
    }

    func testActivationPlanAndAlreadyActiveResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "activate", stage: "build_activation_plan", risk: "local_read", cancellable: true),
                BackendEvent(type: "result", operation: "activate", ok: true, payload: activationPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: activationResultPayload(alreadyActive: true))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.planActivation(password: "pw")

        try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "build_activation_plan")
        XCTAssertEqual(store.activationPlan?.actions.count, 1)
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))

        store.runActivation(password: "pw2")

        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.activationResult?.alreadyActive, true)
        XCTAssertEqual(runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(runner.calls[1].params["credentials"], .object(["password": .string("pw2")]))
    }

    func testRejectedActivationPlanDoesNotEnterPlanning() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ], delayNanoseconds: 100_000_000)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = MaintenanceStore(coordinator: coordinator)

        _ = coordinator.run(operation: "doctor", profile: nil)
        try await waitUntilStoreState { runner.calls.count == 1 }
        let result = store.planActivation(password: "pw")

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.activateState, .failed)
        XCTAssertEqual(store.error?.code, "operation_rejected")
        XCTAssertEqual(runner.calls.count, 1)
        try await waitUntilStoreState { !store.isRunning }
    }

    func testActivationRequiresPlanAndHandlesConfirmationReplay() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: activationPlanPayload())
            ]),
            .init(events: [
                confirmationRequired(operation: "activate", id: "activate-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "stage", operation: "activate", stage: "run_activation", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "activate", ok: true, payload: activationResultPayload(alreadyActive: false))
            ])
        ])
        let backend = BackendClient(runner: runner)
        let store = MaintenanceStore(backend: backend)

        store.runActivation(password: "pw")
        XCTAssertEqual(store.activateState, .failed)
        XCTAssertEqual(store.error?.code, "validation_failed")

        store.planActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .awaitingConfirmation && backend.pendingConfirmation != nil }

        backend.confirmPending()

        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "run_activation")
        XCTAssertEqual(runner.calls[2].params["confirmation_id"], .string("activate-confirm"))
    }

    func testActivationBackendErrorAndMalformedPayloadFail() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "activate",
                    code: "unsupported_device",
                    message: "NetBSD4 activation is not available.",
                    recovery: recoveryValue(title: "Activation unavailable", actions: ["Use deploy instead."], suggestedOperation: "deploy")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.planActivation(password: "")
        try await waitUntilStoreState { store.activateState == .failed && !store.isRunning }
        XCTAssertEqual(store.error?.code, "unsupported_device")
        XCTAssertEqual(store.error?.recovery?.title, "Activation unavailable")

        store.planActivation(password: "")
        try await waitUntilStoreState { store.activateState == .failed && store.error?.code == "contract_decode_failed" && !store.isRunning }
    }

    func testUninstallPlanStaleRunAndBackendError() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallResultPayload(waited: false, verified: false))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallPlanPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "uninstall",
                    code: "remote_error",
                    message: "uninstall failed",
                    recovery: recoveryValue(title: "Uninstall failed", actions: ["Retry."], suggestedOperation: "uninstall")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.mountWait = "15"
        store.noReboot = true

        store.planUninstall(password: "pw")

        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        XCTAssertEqual(store.uninstallPlan?.payloadDirs, ["/Volumes/dk2/.samba4"])
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["mount_wait"], .number(15))

        store.noWait = true
        XCTAssertEqual(store.uninstallState, .planStale)
        store.runUninstall(password: "pw")
        XCTAssertEqual(store.error?.code, "plan_stale")

        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.uninstallResult?.waited, false)
        XCTAssertEqual(store.uninstallResult?.verified, false)
        XCTAssertEqual(runner.calls[2].params["dry_run"], .bool(false))

        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .failed }
        XCTAssertEqual(store.error?.code, "remote_error")
        XCTAssertEqual(store.error?.recovery?.title, "Uninstall failed")
    }

    func testUninstallInvalidMountWaitAndMalformedPlanFail() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.mountWait = "bad"

        store.planUninstall(password: "")

        XCTAssertEqual(store.uninstallState, .failed)
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(runner.calls, [])

        store.mountWait = "30"
        store.planUninstall(password: "")

        try await waitUntilStoreState { store.uninstallState == .failed && store.error?.code == "contract_decode_failed" && !store.isRunning }
    }

    func testUninstallConfirmationReplayCompletes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallPlanPayload())
            ]),
            .init(events: [
                confirmationRequired(operation: "uninstall", id: "uninstall-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "stage", operation: "uninstall", stage: "remove_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: uninstallResultPayload(waited: true, verified: true))
            ])
        ])
        let backend = BackendClient(runner: runner)
        let store = MaintenanceStore(backend: backend)

        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .awaitingConfirmation && backend.pendingConfirmation != nil }

        backend.confirmPending()

        try await waitUntilStoreState { store.uninstallState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "remove_payload")
        XCTAssertEqual(store.uninstallResult?.verified, true)
        XCTAssertEqual(runner.calls[2].params["confirmation_id"], .string("uninstall-confirm"))
    }

    func testFsckListPlanStaleAndRunConfirmation() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckListPayload(targets: [fsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckPlanPayload())
            ]),
            .init(events: [
                confirmationRequired(operation: "fsck", id: "fsck-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckResultPayload(returncode: 0))
            ])
        ])
        let backend = BackendClient(runner: runner)
        let store = MaintenanceStore(backend: backend)

        store.refreshFsckTargets(password: "pw")

        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        XCTAssertEqual(store.fsckTargets.count, 1)
        XCTAssertEqual(store.selectedFsckTarget?.name, "Data")
        XCTAssertEqual(runner.calls[0].params["list_volumes"], .bool(true))

        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        XCTAssertEqual(store.fsckPlan?.device, "/dev/dk2")
        XCTAssertEqual(runner.calls[1].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[1].params["volume"], .string("/dev/dk2"))

        store.noWait = true
        XCTAssertEqual(store.fsckState, .planStale)
        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        store.runFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .awaitingConfirmation && backend.pendingConfirmation != nil }

        backend.confirmPending()

        try await waitUntilStoreState { store.fsckState == .succeeded }
        XCTAssertEqual(store.fsckResult?.returncode, 0)
        XCTAssertEqual(runner.calls[4].params["confirmation_id"], .string("fsck-confirm"))
    }

    func testFsckEmptyListPlanValidationAndFalseResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckListPayload(targets: []))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckListPayload(targets: [fsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: false, payload: fsckResultPayload(returncode: 1))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        XCTAssertEqual(store.fsckTargets, [])

        store.planFsck(password: "")
        XCTAssertEqual(store.fsckState, .failed)
        XCTAssertEqual(store.error?.code, "validation_failed")

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .listReady && store.fsckTargets.count == 1 && !store.isRunning }
        store.planFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        store.runFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .failed }
        XCTAssertEqual(store.error?.code, "operation_failed")
    }

    func testFsckFallbackVolumeParamTargetChangeBackendErrorAndMalformedPayloads() async throws {
        let targetWithoutName = fsckTargetPayload(name: nil, device: "/dev/dk3", mountpoint: "/Volumes/External")
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckListPayload(targets: [
                    targetWithoutName,
                    fsckTargetPayload(name: "Data")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: fsckPlanPayload(target: targetWithoutName, device: "/dev/dk3", mountpoint: "/Volumes/External"))
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "fsck",
                    code: "validation_failed",
                    message: "No HFS volume selected.",
                    recovery: recoveryValue(title: "Select a volume", actions: ["List volumes again."], suggestedOperation: "fsck")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        XCTAssertNil(store.selectedFsckTargetID)
        store.selectedFsckTargetID = store.fsckTargets[0].id

        store.planFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        XCTAssertEqual(runner.calls[1].params["volume"], .string("/dev/dk3"))

        store.selectedFsckTargetID = store.fsckTargets[1].id
        XCTAssertEqual(store.fsckState, .planStale)
        store.runFsck(password: "")
        XCTAssertEqual(store.error?.code, "plan_stale")

        store.planFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .failed }
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(store.error?.recovery?.title, "Select a volume")

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .failed && store.error?.code == "contract_decode_failed" }
    }

    func testRepairXattrsScanRepairStaleConfirmationAndBackendError() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "repair-xattrs", stage: "scan_findings", risk: "local_read", cancellable: true),
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: repairPayload(findings: 2, repairable: 1))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: repairPayload(findings: 2, repairable: 1))
            ]),
            .init(events: [
                confirmationRequired(operation: "repair-xattrs", id: "repair-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: repairPayload(findings: 2, repairable: 0))
            ]),
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "repair-xattrs",
                    code: "validation_failed",
                    message: "repair-xattrs must run on macOS",
                    recovery: recoveryValue(title: "repair-xattrs cannot run", actions: ["Run this from macOS."], suggestedOperation: "repair-xattrs")
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let backend = BackendClient(runner: runner)
        let store = MaintenanceStore(backend: backend)
        store.repairPath = "/Volumes/Data"

        store.scanRepairXattrs()

        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "scan_findings")
        XCTAssertTrue(store.canRepairXattrs)
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))

        store.repairPath = "/Volumes/Other"
        XCTAssertEqual(store.repairState, .scanStale)
        store.repairPath = "/Volumes/Data"
        store.runRepairXattrs()
        XCTAssertEqual(store.repairState, .scanStale)
        XCTAssertEqual(store.error?.code, "scan_stale")
        XCTAssertEqual(runner.calls.count, 1)

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        store.runRepairXattrs()
        try await waitUntilStoreState { store.repairState == .awaitingConfirmation && backend.pendingConfirmation != nil }
        backend.confirmPending()
        try await waitUntilStoreState { store.repairState == .repaired }
        XCTAssertEqual(store.repairResult?.repairableCount, 0)
        XCTAssertEqual(runner.calls[3].params["confirmation_id"], .string("repair-confirm"))

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .failed }
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(store.error?.recovery?.title, "repair-xattrs cannot run")
    }

    func testRepairXattrsMissingPathZeroRepairableAndMalformedPayload() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: repairPayload(findings: 0, repairable: 0))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.scanRepairXattrs()
        XCTAssertEqual(store.repairState, .failed)
        XCTAssertEqual(store.error?.code, "validation_failed")

        store.repairPath = "/Volumes/Data"
        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady }
        XCTAssertFalse(store.canRepairXattrs)

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .failed && store.error?.code == "contract_decode_failed" }
    }

    func testClearResetsMaintenanceState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: activationPlanPayload())
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.planActivation(password: "")
        try await waitUntilStoreState { store.activateState == .planReady }
        store.clear()

        XCTAssertEqual(store.activateState, .idle)
        XCTAssertEqual(store.uninstallState, .idle)
        XCTAssertEqual(store.fsckState, .idle)
        XCTAssertEqual(store.repairState, .idle)
        XCTAssertNil(store.activationPlan)
        XCTAssertNil(store.uninstallPlan)
        XCTAssertNil(store.fsckPlan)
        XCTAssertNil(store.repairScan)
        XCTAssertNil(store.error)
        XCTAssertNil(store.currentStage)
    }

    private func confirmationRequired(operation: String, id: String) -> BackendEvent {
        BackendEvent(
            type: "error",
            operation: operation,
            code: "confirmation_required",
            message: "Confirm \(operation).",
            details: .object([
                "title": .string("Confirm \(operation)"),
                "message": .string("Confirm \(operation)."),
                "action_title": .string("Confirm"),
                "confirmation_id": .string(id)
            ])
        )
    }

    private func activationPlanPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "actions": .array([.object(["type": .string("run_script")])]),
            "post_activation_checks": .array([
                .object(["id": .string("runtime_ready"), "description": .string("runtime ready")])
            ]),
            "counts": .object(["actions": .number(1)]),
            "summary": .string("NetBSD4 activation dry-run plan generated.")
        ])
    }

    private func activationResultPayload(alreadyActive: Bool) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "already_active": .bool(alreadyActive),
            "summary": .string(alreadyActive ? "NetBSD4 payload was already active." : "NetBSD4 activation completed.")
        ])
    }

    private func uninstallPlanPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "host": .string("root@10.0.0.2"),
            "volume_roots": .array([.string("/Volumes/dk2")]),
            "payload_dirs": .array([.string("/Volumes/dk2/.samba4")]),
            "remote_actions": .array([.object(["type": .string("remove_path")])]),
            "requires_reboot": .bool(true),
            "reboot_required": .bool(true),
            "post_uninstall_checks": .array([
                .object(["id": .string("managed_files_absent"), "description": .string("managed files absent")])
            ]),
            "counts": .object(["payload_dirs": .number(1)]),
            "summary": .string("uninstall dry-run plan generated.")
        ])
    }

    private func uninstallResultPayload(waited: Bool, verified: Bool) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "summary": .string(verified ? "uninstall completed." : "uninstall completed without post-reboot verification."),
            "requires_reboot": .bool(true),
            "rebooted": .bool(false),
            "reboot_requested": .bool(true),
            "waited": .bool(waited),
            "verified": .bool(verified)
        ])
    }

    private func fsckListPayload(targets: [JSONValue]) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "targets": .array(targets),
            "counts": .object(["targets": .number(Double(targets.count))]),
            "summary": .string("found \(targets.count) mounted HFS volume(s).")
        ])
    }

    private func fsckTargetPayload(
        name: String?,
        device: String = "/dev/dk2",
        mountpoint: String = "/Volumes/dk2"
    ) -> JSONValue {
        var payload: [String: JSONValue] = [
            "device": .string(device),
            "mountpoint": .string(mountpoint),
            "builtin": .bool(true)
        ]
        if let name {
            payload["name"] = .string(name)
        }
        return .object(payload)
    }

    private func fsckPlanPayload(
        target: JSONValue? = nil,
        device: String = "/dev/dk2",
        mountpoint: String = "/Volumes/dk2"
    ) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "target": target ?? fsckTargetPayload(name: "Data"),
            "device": .string(device),
            "mountpoint": .string(mountpoint),
            "reboot_required": .bool(true),
            "wait_after_reboot": .bool(false),
            "summary": .string("fsck dry-run plan generated.")
        ])
    }

    private func fsckResultPayload(returncode: Int) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "device": .string("/dev/dk2"),
            "mountpoint": .string("/Volumes/dk2"),
            "returncode": .number(Double(returncode)),
            "reboot_requested": .bool(false),
            "waited": .bool(false),
            "verified": .bool(false),
            "summary": .string("fsck completed.")
        ])
    }

    private func repairPayload(findings: Int, repairable: Int) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "returncode": .number(0),
            "root": .string("/Volumes/Data"),
            "finding_count": .number(Double(findings)),
            "repairable_count": .number(Double(repairable)),
            "counts": .object([
                "findings": .number(Double(findings)),
                "repairable": .number(Double(repairable))
            ]),
            "stats": .object([:]),
            "report": .string("report"),
            "summary": .string("repair-xattrs found \(findings) issue(s), \(repairable) repairable."),
            "summary_text": .string("repair-xattrs found \(findings) issue(s), \(repairable) repairable.")
        ])
    }
}
