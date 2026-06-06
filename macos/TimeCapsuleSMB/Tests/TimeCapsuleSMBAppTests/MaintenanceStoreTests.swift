import Combine
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
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))
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

    func testPublishesWhenBackendFinishesAfterActivationPlanResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        let finishPublished = expectation(description: "MaintenanceStore publishes after backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        store.objectWillChange
            .sink { [weak store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.activateState == .planReady,
                          store?.isBusy == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        store.planActivation(password: "pw")

        try await waitUntilStoreState { store.activateState == .planReady }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isBusy)
        _ = cancellables
    }

    func testSameDeviceRejectedActivationPlanDoesNotEnterPlanning() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
        let store = MaintenanceStore(
            coordinator: coordinator,
            laneKey: .deviceWorkflow(profile.id, .maintenance)
        )

        _ = coordinator.run(operation: "doctor", profile: profile)
        try await waitUntilStoreState { runner.calls.count == 1 && coordinator.isDeviceBusy(profile) }
        let result = store.planActivation(password: "pw", profile: profile)

        XCTAssertEqual(result.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(store.activateState, .failed)
        XCTAssertEqual(store.error(for: .activate)?.code, "operation_already_running")
        XCTAssertEqual(runner.calls.count, 1)
        runner.finishAll()
        try await waitUntilStoreState { !store.isRunning }
    }

    func testActivationRunsDirectlyAndHandlesConfirmationReplay() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                confirmationRequired(operation: "activate", id: "activate-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "stage", operation: "activate", stage: "run_activation", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: false))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .awaitingConfirmation && store.pendingConfirmation(for: .activate) != nil && !store.isRunning }

        store.confirmPending(for: .activate)

        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "run_activation")
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(false))
        XCTAssertEqual(runner.calls[1].params["confirmation_id"], .string("activate-confirm"))
    }

    func testConfirmationCancellationRestoresMaintenanceWorkflowState() async throws {
        do {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
                ]),
                .init(events: [
                    confirmationRequired(operation: "activate", id: "activate-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ])
            let store = MaintenanceStore(backend: BackendClient(runner: runner))

            store.planActivation(password: "pw")
            try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
            store.runActivation(password: "pw")
            try await waitUntilStoreState { store.activateState == .awaitingConfirmation && store.pendingConfirmation(for: .activate) != nil && !store.isRunning }
            store.cancelPendingConfirmation(for: .activate)

            try await waitUntilStoreState { store.activateState == .planReady && store.pendingConfirmation(for: .activate) == nil }
            XCTAssertNil(store.error)
        }

        do {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    confirmationRequired(operation: "uninstall", id: "uninstall-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ])
            let store = MaintenanceStore(backend: BackendClient(runner: runner))

            store.runUninstall(password: "pw")
            try await waitUntilStoreState { store.uninstallState == .awaitingConfirmation && store.pendingConfirmation(for: .uninstall) != nil && !store.isRunning }
            store.noWait = true
            store.cancelPendingConfirmation(for: .uninstall)

            try await waitUntilStoreState { store.uninstallState == .idle && store.pendingConfirmation(for: .uninstall) == nil }
            XCTAssertNil(store.error)
        }

        do {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))
                ]),
                .init(events: [
                    BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
                ]),
                .init(events: [
                    confirmationRequired(operation: "fsck", id: "fsck-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ])
            let store = MaintenanceStore(backend: BackendClient(runner: runner))

            store.refreshFsckTargets(password: "pw")
            try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
            store.planFsck(password: "pw")
            try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
            store.runFsck(password: "pw")
            try await waitUntilStoreState { store.fsckState == .awaitingConfirmation && store.pendingConfirmation(for: .fsck) != nil && !store.isRunning }
            store.noWait = true
            store.cancelPendingConfirmation(for: .fsck)

            try await waitUntilStoreState { store.fsckState == .planStale && store.pendingConfirmation(for: .fsck) == nil }
            XCTAssertNil(store.error)
        }

        do {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
                ]),
                .init(events: [
                    confirmationRequired(operation: "repair-xattrs", id: "repair-confirm")
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ])
            let store = MaintenanceStore(backend: BackendClient(runner: runner))
            store.repairPath = "/Volumes/Data"

            store.scanRepairXattrs()
            try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
            store.runRepairXattrs()
            try await waitUntilStoreState { store.repairState == .awaitingConfirmation && store.pendingConfirmation(for: .repairXattrs) != nil && !store.isRunning }
            store.repairPath = "/Volumes/Other"
            store.cancelPendingConfirmation(for: .repairXattrs)

            try await waitUntilStoreState { store.repairState == .scanStale && store.pendingConfirmation(for: .repairXattrs) == nil }
            XCTAssertNil(store.error)
        }
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

    func testUninstallPlanDirectRunAndBackendError() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallResultPayload(waited: false, verified: false))
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
        try await waitUntilStoreState { store.uninstallState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.uninstallResult?.waited, false)
        XCTAssertEqual(store.uninstallResult?.verified, false)
        XCTAssertNil(store.uninstallPlan)
        XCTAssertEqual(runner.calls[1].params["dry_run"], .bool(false))
        XCTAssertEqual(runner.calls[1].params["no_wait"], .bool(true))

        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .failed }
        XCTAssertEqual(store.error?.code, "remote_error")
        XCTAssertEqual(store.error?.recovery?.title, "Uninstall failed")
        XCTAssertEqual(runner.calls[2].params["dry_run"], .bool(false))
    }

    func testUninstallInvalidMountWaitAndMalformedPlanFail() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.mountWait = "bad"

        store.runUninstall(password: "")

        XCTAssertEqual(store.uninstallState, .failed)
        XCTAssertEqual(store.error?.code, "mount_wait_invalid")
        XCTAssertEqual(runner.calls, [])

        store.mountWait = "30"
        store.planUninstall(password: "")

        try await waitUntilStoreState { store.uninstallState == .failed && store.error?.code == "contract_decode_failed" && !store.isRunning }
    }

    func testUninstallConfirmationReplayCompletes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                confirmationRequired(operation: "uninstall", id: "uninstall-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "stage", operation: "uninstall", stage: "remove_payload", risk: "remote_write", cancellable: false),
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallResultPayload(waited: true, verified: true))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .awaitingConfirmation && store.pendingConfirmation(for: .uninstall) != nil && !store.isRunning }

        store.confirmPending(for: .uninstall)

        try await waitUntilStoreState { store.uninstallState == .succeeded && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "remove_payload")
        XCTAssertEqual(store.uninstallResult?.verified, true)
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(false))
        XCTAssertEqual(runner.calls[1].params["confirmation_id"], .string("uninstall-confirm"))
    }

    func testFsckListPlanStaleAndRunConfirmation() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
            ]),
            .init(events: [
                confirmationRequired(operation: "fsck", id: "fsck-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckResultPayload(returncode: 0))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

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
        try await waitUntilStoreState { store.fsckState == .awaitingConfirmation && store.pendingConfirmation(for: .fsck) != nil && !store.isRunning }

        store.confirmPending(for: .fsck)

        try await waitUntilStoreState { store.fsckState == .succeeded }
        XCTAssertEqual(store.fsckResult?.returncode, 0)
        XCTAssertEqual(runner.calls[4].params["confirmation_id"], .string("fsck-confirm"))
    }

    func testFsckEmptyListPlanValidationAndFalseResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: []))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: false, payload: testFsckResultPayload(returncode: 1))
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        XCTAssertEqual(store.fsckTargets, [])

        store.planFsck(password: "")
        XCTAssertEqual(store.fsckState, .failed)
        XCTAssertEqual(store.error?.code, "fsck_target_required")

        store.refreshFsckTargets(password: "")
        try await waitUntilStoreState { store.fsckState == .listReady && store.fsckTargets.count == 1 && !store.isRunning }
        store.planFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        store.runFsck(password: "")
        try await waitUntilStoreState { store.fsckState == .failed }
        XCTAssertEqual(store.error?.code, "operation_failed")
    }

    func testFsckFallbackVolumeParamTargetChangeBackendErrorAndMalformedPayloads() async throws {
        let targetWithoutName = testFsckTargetPayload(name: nil, device: "/dev/dk3", mountpoint: "/Volumes/External")
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [
                    targetWithoutName,
                    testFsckTargetPayload(name: "Data")
                ]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload(target: targetWithoutName, device: "/dev/dk3", mountpoint: "/Volumes/External"))
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
        XCTAssertEqual(store.error?.code, "fsck_plan_stale")

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
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ]),
            .init(events: [
                confirmationRequired(operation: "repair-xattrs", id: "repair-confirm")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 0))
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
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.repairPath = "/Volumes/Data"
        store.repairRecursive = false
        store.repairMaxDepth = "2"
        store.repairIncludeHidden = true
        store.repairIncludeTimeMachine = true
        store.repairFixPermissions = true
        store.repairVerbose = true

        store.scanRepairXattrs()

        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        XCTAssertEqual(store.currentStage?.stage, "scan_findings")
        XCTAssertTrue(store.canRepairXattrs)
        XCTAssertEqual(runner.calls[0].params["dry_run"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["recursive"], .bool(false))
        XCTAssertEqual(runner.calls[0].params["max_depth"], .number(2))
        XCTAssertEqual(runner.calls[0].params["include_hidden"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["include_time_machine"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["fix_permissions"], .bool(true))
        XCTAssertEqual(runner.calls[0].params["verbose"], .bool(true))

        store.repairPath = "/Volumes/Other"
        XCTAssertEqual(store.repairState, .scanStale)
        store.repairPath = "/Volumes/Data"
        store.runRepairXattrs()
        XCTAssertEqual(store.repairState, .scanStale)
        XCTAssertEqual(store.error?.code, "repair_xattrs_scan_stale")
        XCTAssertEqual(runner.calls.count, 1)

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        store.runRepairXattrs()
        try await waitUntilStoreState { store.repairState == .awaitingConfirmation && store.pendingConfirmation(for: .repairXattrs) != nil && !store.isRunning }
        store.confirmPending(for: .repairXattrs)
        try await waitUntilStoreState { store.repairState == .repaired }
        XCTAssertEqual(store.repairResult?.repairableCount, 0)
        XCTAssertEqual(runner.calls[3].params["confirmation_id"], .string("repair-confirm"))
        XCTAssertEqual(runner.calls[3].params["recursive"], .bool(false))
        XCTAssertEqual(runner.calls[3].params["max_depth"], .number(2))

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .failed }
        XCTAssertEqual(store.error?.code, "validation_failed")
        XCTAssertEqual(store.error?.recovery?.title, "repair-xattrs cannot run")
    }

    func testRepairXattrsOptionChangesInvalidateScan() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        store.repairPath = "/Volumes/Data"

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        XCTAssertTrue(store.canRepairXattrs)
        XCTAssertEqual(runner.calls[0].params["recursive"], .bool(true))
        XCTAssertNil(runner.calls[0].params["max_depth"])

        store.repairMaxDepth = "3"
        XCTAssertEqual(store.repairState, .scanStale)
        XCTAssertFalse(store.canRepairXattrs)
        store.runRepairXattrs()
        XCTAssertEqual(store.error?.code, "repair_xattrs_scan_stale")
        XCTAssertEqual(runner.calls.count, 1)

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        XCTAssertEqual(runner.calls[1].params["max_depth"], .number(3))
    }

    func testRepairXattrsMissingPathZeroRepairableAndMalformedPayload() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 0, repairable: 0))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.scanRepairXattrs()
        XCTAssertEqual(store.repairState, .failed)
        XCTAssertEqual(store.error?.code, "repair_xattrs_path_required")
        XCTAssertFalse(store.canScanRepairXattrs)

        store.repairPath = "/Volumes/Data"
        store.repairMaxDepth = "-1"
        store.scanRepairXattrs()
        XCTAssertEqual(store.repairState, .failed)
        XCTAssertEqual(store.error?.code, "repair_xattrs_depth_invalid")
        XCTAssertEqual(runner.calls, [])

        store.repairMaxDepth = ""
        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady }
        XCTAssertFalse(store.canRepairXattrs)

        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .failed && store.error?.code == "contract_decode_failed" }
    }

    func testCoordinatorMaintenanceWorkflowsUseSeparateLanes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallPlanPayload())
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = MaintenanceStore(
            coordinator: coordinator,
            laneKey: .deviceWorkflow("device-one", .maintenance)
        )

        store.planActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }

        let activateLane = OperationLaneKey.deviceWorkflow("device-one", .activate)
        let uninstallLane = OperationLaneKey.deviceWorkflow("device-one", .uninstall)
        let legacyMaintenanceLane = OperationLaneKey.deviceWorkflow("device-one", .maintenance)
        XCTAssertEqual(coordinator.lane(for: activateLane).backend.events.last?.operation, "activate")
        XCTAssertEqual(coordinator.lane(for: uninstallLane).backend.events.last?.operation, "uninstall")
        XCTAssertTrue(coordinator.lane(for: legacyMaintenanceLane).backend.events.isEmpty)
        XCTAssertEqual(store.timelineEvents(for: .activate).last?.operation, "activate")
        XCTAssertEqual(store.timelineEvents(for: .uninstall).last?.operation, "uninstall")
    }

    func testMaintenanceWorkflowErrorsDoNotBleedBetweenPages() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "activate",
                    code: "remote_error",
                    message: "activation failed"
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallPlanPayload())
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))

        store.planActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .failed && !store.isRunning }
        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }

        XCTAssertEqual(store.error(for: .activate)?.code, "remote_error")
        XCTAssertNil(store.error(for: .uninstall))
    }

    func testClearResetsMaintenanceState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
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

}
