import AppKit
import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class BootHookStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(BootHookState.allCases, [
            .readingBanks,
            .savingBackup,
            .analyzingBanks,
            .planAvailable,
            .appleCheckComplete,
            .appleFirmwareMismatch,
            .appleFirmwareReady,
            .ready,
            .awaitingStrongConfirmation,
            .writing,
            .readbackValidating,
            .writeNotNeeded,
            .writeValidatedSnapshotStale,
            .restoreRebooting,
            .failed
        ])
    }

    func testOnlyBackUpIsOfferedUntilABackupExists() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses())
        func enabled() -> Set<MaintenanceUserAction> {
            MaintenanceWorkflowDetailPresentation(store: fixture.maintenanceStore, workflow: .bootHook).enabledActions
        }
        XCTAssertEqual(fixture.store.state, .ready)
        XCTAssertEqual(enabled(), [.backUpAndInspectFirmware])
        XCTAssertEqual(
            fixture.maintenanceStore.planFirmware(mode: .checkApple, profile: fixture.profile).rejectionMessage,
            WorkflowLocalError.flashBackupRequired.message
        )
        XCTAssertTrue(fixture.runner.calls.isEmpty)

        try await fixture.run(.backup)

        // Writes come only after a plan.
        XCTAssertEqual(enabled(), [
            .backUpAndInspectFirmware, .planPatch, .planRestore, .checkAppleFirmware, .validateAppleFirmware
        ])
    }

    func testOnlyBackupsAndWritesCountAsRunsOnTheDevice() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.patchNeedsPowerCycle)
        ))
        let store = fixture.store
        XCTAssertFalse(store.lastRunConnectsToDevice)

        try await fixture.run(.backup)
        XCTAssertTrue(store.lastRunConnectsToDevice)
        try await fixture.run(.plan(.patch))
        XCTAssertFalse(store.lastRunConnectsToDevice)
        try await fixture.run(.write(.patch))
        XCTAssertTrue(store.lastRunConnectsToDevice)

        store.clear()
        XCTAssertFalse(store.lastRunConnectsToDevice)
    }

    func testARefusalWhileABackupRunsLeavesTheBackupAlone() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(
            responses: testBootHookResponses().map { .init(events: $0.events, pauseBeforeEvents: true) }
        ))
        defer { fixture.runner.finishAll() }
        let store = fixture.store
        store.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 1 }

        for start in [
            { store.backUpAndInspectFirmware(password: "pw", profile: fixture.profile) },
            { store.planFirmware(mode: .patch, profile: fixture.profile) },
            { store.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile) },
        ] {
            XCTAssertEqual(start().rejectionMessage, WorkflowLocalError.operationAlreadyRunning.message)
        }
        XCTAssertEqual(store.state, .readingBanks)
        XCTAssertNil(store.error)

        fixture.runner.finishAll()
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        XCTAssertEqual(store.state, .planAvailable)
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    func testPanelSubtitleSaysWhatTheBootHookChanges() throws {
        let presentation = try BootHookTestFixture().presentation

        XCTAssertEqual(
            presentation.subtitle,
            "Modify the firmware of NetBSD 4 devices to automatically start the installed Samba services after a restart."
        )
        // The card keeps its own, shorter line.
        XCTAssertNotEqual(presentation.subtitle, MaintenanceWorkflow.bootHook.presentationSubtitle)
    }

    func testStatusLineFollowsEachStepAndNeverRepeatsTheBadge() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.patchNeedsPowerCycle)
        ))
        var lines: [String] = []
        func record(file: StaticString = #filePath, line: UInt = #line) {
            let presentation = fixture.presentation
            XCTAssertNotEqual(presentation.statusMessage, presentation.stateTitle, file: file, line: line)
            lines.append(presentation.statusMessage)
        }

        record()
        try await fixture.run(.backup)
        record()
        try await fixture.run(.plan(.patch))
        record()
        try await fixture.run(.write(.patch))
        record()

        XCTAssertEqual(lines, [
            "Back up and inspect firmware before planning a patch or a restore.",
            try XCTUnwrap(fixture.store.backup?.localizedSummary),
            "Patch plan ready. Choose “Write Patch” to install the boot hook.",
            "Flash patch write validated; manual power cycle required."
        ])
    }

    func testButtonsComeInTwoRows() throws {
        XCTAssertEqual(try BootHookTestFixture().presentation.actionRows, [
            [.backUpAndInspectFirmware, .planPatch, .planRestore, .writePatch, .writeRestore],
            [.checkAppleFirmware, .validateAppleFirmware]
        ])
    }

    func testOnlyTheBootHooksOwnRunAnimatesItsStatusLine() async throws {
        // A runner's gate stays open once released, so each run gets its own.
        func detail(_ workflow: MaintenanceWorkflow, _ fixture: BootHookTestFixture<PausingStoreTestRunner>) -> MaintenanceWorkflowDetailPresentation {
            MaintenanceWorkflowDetailPresentation(store: fixture.maintenanceStore, workflow: workflow)
        }

        // Another workflow's run holds the device but is not the boot hook's.
        let activation = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))
            ], pauseBeforeEvents: true)
        ]), onDeviceLane: true)
        defer { activation.runner.finishAll() }
        XCTAssertFalse(detail(.bootHook, activation).isRunning)
        activation.maintenanceStore.runActivation(password: "pw", profile: activation.profile)
        try await waitUntilStoreState { activation.runner.calls.count == 1 }
        XCTAssertTrue(detail(.activate, activation).isRunning)
        XCTAssertFalse(detail(.bootHook, activation).isRunning)

        let backup = try BootHookTestFixture(runner: PausingStoreTestRunner(
            responses: testBootHookResponses().map { .init(events: $0.events, pauseBeforeEvents: true) }
        ), onDeviceLane: true)
        defer { backup.runner.finishAll() }
        backup.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: backup.profile)
        try await waitUntilStoreState { backup.runner.calls.count == 1 }
        XCTAssertTrue(detail(.bootHook, backup).isRunning)
        XCTAssertFalse(detail(.activate, backup).isRunning)
        backup.runner.finishAll()
        try await waitUntilStoreState { !backup.maintenanceStore.isBusy }
        XCTAssertFalse(detail(.bootHook, backup).isRunning)
    }

    func testBootHookActionSymbolsResolveToSFSymbols() {
        for action in MaintenanceActionPolicy.actions(for: .bootHook) {
            XCTAssertNotNil(NSImage(systemSymbolName: action.systemImage, accessibilityDescription: nil), action.systemImage)
        }
    }

    func testFlashWriteParamsDefaultRestoreToRebootAndPatchToManualPowerCycle() {
        let restore = OperationParams.Flash.write(
            backupDir: "/tmp/flash-backup",
            mode: .restore
        )
        let patch = OperationParams.Flash.write(
            backupDir: "/tmp/flash-backup",
            mode: .patch
        )

        XCTAssertEqual(restore["reboot_after_write"], .bool(true))
        XCTAssertEqual(restore["wait_after_reboot"], .bool(true))
        XCTAssertEqual(patch["reboot_after_write"], .bool(false))
        XCTAssertNil(patch["wait_after_reboot"])
    }

    func testBackupAndPlanFlowTracksStructuredPayloads() async throws {
        let fixture = try BootHookTestFixture(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "read_flash", risk: "remote_read", cancellable: true),
                testBootHookResult(testBootHookBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "plan_flash", risk: "local_write", cancellable: true),
                testBootHookResult(testBootHookPlanPayload(mode: .patch))
            ])
        ])
        let store = fixture.store
        let runner = fixture.runner

        fixture.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        XCTAssertEqual(store.state, .readingBanks)
        try await waitUntilStoreState { store.backup != nil && store.state == .planAvailable && !store.isBusy }

        fixture.maintenanceStore.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { store.plan != nil && store.canWritePatch }

        XCTAssertEqual(runner.calls.count, 2)
        XCTAssertEqual(runner.calls[0].operation, "flash")
        XCTAssertEqual(runner.calls[0].params["action"], .string("backup"))
        XCTAssertEqual(runner.calls[0].params["credentials"], .object(["password": .string("pw")]))
        XCTAssertEqual(runner.calls[1].params["action"], .string("plan"))
        XCTAssertEqual(runner.calls[1].params["backup_dir"], .string("/tmp/flash-backup"))
        XCTAssertEqual(runner.calls[1].params["mode"], .string("patch"))
    }

    func testEachFlashTransitionWaitsForHelperCompletion() async throws {
        // Each case has a fresh gate: releasing backup must not unpause plan/write.
        for pausedStep in 0...2 {
            let responses = testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .patch),
                write: testBootHookWritePayload(.patchNeedsPowerCycle)
            )
            let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: responses.enumerated().map { index, response in
                .init(events: response.events, pauseAfterEvents: index == pausedStep)
            }))
            let runner = fixture.runner
            defer { runner.finishAll() }
            let store = fixture.store
            let profile = fixture.profile
            for step in 0...2 {
                switch step {
                case 0: fixture.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: profile)
                case 1: fixture.maintenanceStore.planFirmware(mode: .patch, profile: profile)
                default: fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: profile)
                }
                try await waitUntilStoreState {
                    switch step {
                    case 0: return store.backup != nil
                    case 1: return store.plan != nil
                    default: return store.writeResult != nil
                    }
                }
                if step == pausedStep {
                    XCTAssertTrue(store.isBusy)
                    XCTAssertFalse(fixture.maintenanceStore.canBackUpFirmware)
                    if step < 2 {
                        // After the write the stale backup refuses these anyway.
                        XCTAssertFalse(store.canPlan)
                        XCTAssertFalse(store.canWritePatch)
                    }
                    XCTAssertEqual(runner.calls.count, step + 1)
                    runner.finishAll()
                }
                try await waitUntilStoreState { !store.isBusy }
            }
            XCTAssertEqual(runner.calls.map { $0.params["action"] }, [.string("backup"), .string("plan"), .string("write")])
            XCTAssertTrue(store.backupSnapshotStale)
            XCTAssertTrue(fixture.maintenanceStore.canBackUpFirmware)
        }
    }

    func testPublishesWhenBackendFinishesAfterBackupResult() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses())
        let store = fixture.store
        let finishPublished = expectation(description: "BootHookStore publishes after backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        store.objectWillChange
            .sink { [weak store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.state == .planAvailable,
                          store?.isBusy == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        fixture.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)

        try await waitUntilStoreState { store.state == .planAvailable }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isBusy)
        _ = cancellables
    }

    func testPlanFlashCarriesAppleFirmwareSelectionOptions() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(plan: testAppleRestoreFirmwarePayload()))
        let store = fixture.store

        try await fixture.run(.backup)
        store.firmwareVersion = " 7.8.1 "
        store.firmwareTemplatePath = " /tmp/firmware.basebinary "
        try await fixture.run(.plan(.downloadOnly))

        XCTAssertEqual(fixture.runner.calls.count, 2)
        XCTAssertEqual(fixture.runner.calls[1].params["firmware_version"], .string("7.8.1"))
        XCTAssertEqual(fixture.runner.calls[1].params["firmware_template"], .string("/tmp/firmware.basebinary"))
    }

    func testFirmwareSelectionEditsInvalidateExistingWritePlan() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch)
        ))
        let store = fixture.store

        try await fixture.run(.backup)
        store.firmwareVersion = "7.8.1"
        try await fixture.run(.plan(.patch))
        XCTAssertTrue(store.canWritePatch)

        store.firmwareVersion = "7.8.2"

        XCTAssertNil(store.plan)
        XCTAssertFalse(store.canWritePatch)
        XCTAssertEqual(store.state, .planAvailable)
    }

    func testAppleCheckPresentationShowsMatchDetails() async throws {
        let fixture = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: true))
        let store = fixture.store

        XCTAssertEqual(store.state, .appleCheckComplete)
        let presentation = fixture.presentation
        XCTAssertEqual(presentation.statusMessage, "Active firmware bank matches Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Apple Match", value: "yes")))
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Apple Version", value: "7.8.1")))
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Apple Payload SHA-256", value: "inner-sha")))
    }

    func testAppleCheckPresentationShowsMultiBankMatchDetails() async throws {
        let fixture = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: true, allBanks: true))
        let store = fixture.store

        XCTAssertEqual(store.state, .appleCheckComplete)
        let presentation = fixture.presentation
        XCTAssertEqual(presentation.statusMessage, "All candidate firmware banks match Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Apple Match (primary)", value: "yes")))
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Apple Match (secondary)", value: "yes")))
    }

    func testAppleCheckMismatchUsesDedicatedState() async throws {
        // One candidate bank, and two that both differ from Apple's firmware,
        // which get a row each.
        let cases: [(allBanks: Bool, rows: [String])] = [
            (false, ["Apple Match"]),
            (true, ["Apple Match (primary)", "Apple Match (secondary)"]),
        ]
        for (allBanks, rows) in cases {
            let fixture = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: false, allBanks: allBanks))

            XCTAssertEqual(fixture.store.state, .appleFirmwareMismatch, "all banks: \(allBanks)")
            for label in rows {
                XCTAssertTrue(fixture.presentation.bootHookRows.contains(PresentationRow(label: label, value: "no")), label)
            }
        }
    }

    func testValidateAppleRestoreFirmwarePresentationShowsPayloadDetails() async throws {
        let fixture = try await BootHookTestFixture.afterPlan(testAppleRestoreFirmwarePayload())
        let store = fixture.store

        XCTAssertEqual(store.state, .appleFirmwareReady)
        let presentation = fixture.presentation
        XCTAssertEqual(presentation.statusMessage, "Apple restore firmware validated (version 7.8.1, product 119).")
        XCTAssertEqual(presentation.title(for: .validateAppleFirmware), "Validate Apple Restore Firmware")
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Firmware Payload", value: "/tmp/flash-backup/primary.download_only.basebinary")))
        XCTAssertTrue(presentation.bootHookRows.contains(PresentationRow(label: "Firmware Payload SHA-256", value: "payload-sha")))
    }

    func testWriteConfirmationCancellationRestoresPlanAvailable() async throws {
        for mode in [FlashPlanMode.patch, .restore] {
            let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: mode)
            ) + [
                .init(events: [writeConfirmationRequired()], pauseAfterEvents: true)
            ]), onDeviceLane: true)
            let runner = fixture.runner
            defer { runner.finishAll() }
            let coordinator = fixture.coordinator
            let store = fixture.store
            let profile = fixture.profile

            try await fixture.run(.backup, .plan(mode))

            fixture.maintenanceStore.writeFirmware(mode: mode, password: "pw", profile: profile)
            try await waitUntilStoreState { store.state == .awaitingStrongConfirmation }
            if mode == .patch {
                // Write Patch was just pressed; the plan's own summary stays true.
                XCTAssertEqual(fixture.presentation.statusMessage, "Flash patch write plan generated.")
            }
            XCTAssertTrue(coordinator.isDeviceBusy(profile))
            XCTAssertNil(coordinator.readyConfirmation)
            runner.finishAll()
            try await waitUntilStoreState { coordinator.readyConfirmation != nil }
            let pending = try XCTUnwrap(coordinator.readyConfirmation)
            coordinator.cancel(pending)
            coordinator.cancel(pending)
            XCTAssertEqual(store.state, .planAvailable)
            if mode == .patch {
                XCTAssertEqual(fixture.presentation.statusMessage, "Patch plan ready. Choose “Write Patch” to install the boot hook.")
            }
            XCTAssertFalse(coordinator.isDeviceBusy(profile))
            XCTAssertFalse(store.isBusy)
            XCTAssertNil(store.currentStage)
            XCTAssertNil(coordinator.readyConfirmation)
            XCTAssertEqual(runner.calls.count, 3)
        }
    }

    func testATemplateChosenDuringARunDropsThePlanWhenTheRunEnds() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch)
        ) + [
            .init(events: [writeConfirmationRequired()])
        ]), onDeviceLane: true)
        defer { fixture.runner.finishAll() }
        let store = fixture.store
        let coordinator = fixture.coordinator
        try await fixture.run(.backup, .plan(.patch))

        fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { coordinator.readyConfirmation != nil }
        // As a Choose panel opened before Write Patch would set it.
        store.firmwareTemplatePath = "/tmp/other.basebinary"
        XCTAssertNotNil(store.plan)

        coordinator.cancel(try XCTUnwrap(coordinator.readyConfirmation))
        try await waitUntilStoreState { !store.isBusy }
        XCTAssertNil(store.plan)
        XCTAssertEqual(store.state, .planAvailable)
        XCTAssertTrue(fixture.maintenanceStore.canPlanFirmware)
        XCTAssertFalse(fixture.maintenanceStore.canWritePatch)
        XCTAssertNotEqual(fixture.presentation.statusMessage, "Flash patch write plan generated.")
    }

    func testATemplateChosenAfterTheLastEventButBeforeTheHelperExitsDropsThePlan() async throws {
        let responses = testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            failure: [
                BackendEvent(type: "stage", operation: "flash", stage: "pre_write_validation", risk: "remote_write", cancellable: false),
                testBootHookError("write failed")
            ]
        )
        // The write's helper sends its failure, then waits before it exits.
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: responses.indices.map {
            .init(events: responses[$0].events, result: responses[$0].result, pauseAfterEvents: $0 == responses.count - 1)
        }))
        defer { fixture.runner.finishAll() }
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch))

        fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { store.error != nil }
        XCTAssertTrue(store.isBusy)
        store.firmwareTemplatePath = "/tmp/other.basebinary"

        XCTAssertNil(store.plan)
        XCTAssertEqual(store.state, .failed)
        XCTAssertFalse(store.backupSnapshotStale)
    }

    func testCancellingTheWriteConfirmationThroughMaintenanceRestoresThePlan() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(plan: testBootHookPlanPayload(mode: .patch)) + [
            .init(
                events: [writeConfirmationRequired()],
                result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
            )
        ])
        let maintenance = fixture.maintenanceStore
        try await fixture.run(.backup, .plan(.patch))

        maintenance.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        try await waitUntilStoreState {
            maintenance.pendingConfirmation(for: .bootHook) != nil && !maintenance.isRunning
        }
        maintenance.cancelPendingConfirmation(for: .bootHook)

        try await waitUntilStoreState {
            fixture.store.state == .planAvailable && maintenance.pendingConfirmation(for: .bootHook) == nil
        }
        XCTAssertNil(maintenance.error)
        XCTAssertTrue(maintenance.canWritePatch)
        XCTAssertEqual(fixture.runner.calls.count, 3)
    }

    func testBootHookButtonsWaitWhileAnotherJobUsesTheDevice() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))
            ], pauseBeforeEvents: true)
        ] + testBootHookResponses()), onDeviceLane: true)
        defer { fixture.runner.finishAll() }
        let maintenance = fixture.maintenanceStore

        maintenance.runActivation(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 1 }

        // The boot hook's own lane is free; the device is not.
        XCTAssertFalse(fixture.store.isBusy)
        XCTAssertFalse(maintenance.canBackUpFirmware)
        XCTAssertFalse(MaintenanceWorkflowDetailPresentation(store: maintenance, workflow: .bootHook).isEnabled(.backUpAndInspectFirmware))
        let refused = maintenance.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        XCTAssertEqual(refused.rejectionMessage, "Another operation is already running.")
        XCTAssertEqual(fixture.runner.calls.count, 1)
        // The idle boot hook says why it did not start, and keeps nothing else.
        XCTAssertEqual(fixture.store.error?.code, WorkflowLocalError.operationAlreadyRunning.code)
        XCTAssertEqual(fixture.store.state, .failed)
        XCTAssertNil(fixture.store.backup)

        fixture.runner.finishAll()
        try await waitUntilStoreState { !maintenance.isBusy }
        XCTAssertTrue(maintenance.canBackUpFirmware)
        try await fixture.run(.backup)
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["activate", "flash"])
    }

    func testOtherWorkflowsWaitWhileTheBootHookUsesTheDevice() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses().map {
            .init(events: $0.events, pauseBeforeEvents: true)
        }), onDeviceLane: true)
        defer { fixture.runner.finishAll() }
        let maintenance = fixture.maintenanceStore

        maintenance.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 1 }

        XCTAssertFalse(maintenance.canRunActivation)
        XCTAssertFalse(maintenance.canCheckSSHAccess)
        XCTAssertTrue(maintenance.isRunning)
        XCTAssertTrue(maintenance.canCancel)

        fixture.runner.finishAll()
        try await waitUntilStoreState { !maintenance.isBusy }
        XCTAssertTrue(maintenance.canRunActivation)
        XCTAssertNotNil(fixture.store.backup)
    }

    func testValidatedPatchWriteShowsManualPowerCycleNotice() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.patch)
        let store = fixture.store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertTrue(store.showsManualPowerCycleNotice)

        store.dismissManualPowerCycleNotice()

        XCTAssertFalse(store.showsManualPowerCycleNotice)
    }

    func testValidatedRestoreWriteWithDefaultRebootDoesNotShowManualPowerCycleNotice() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.restore)
        let store = fixture.store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertFalse(store.showsManualPowerCycleNotice)
        XCTAssertFalse(fixture.presentation.warnings.contains(
            "Unplug the device, wait 10 seconds, then plug it back in."
        ))
    }

    func testValidatedRestoreWriteWithoutRebootShowsManualPowerCycleNotice() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.restore, .restoreNeedsManualReboot)
        let store = fixture.store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertTrue(store.showsManualPowerCycleNotice)
    }

    func testValidatedSecondaryBankRestoreShowsNoPowerCycleNoticeAndAsksForAFreshBackup() async throws {
        // The device keeps running its primary bank, so nothing needs restarting,
        // but the backup no longer describes the rewritten secondary bank.
        let fixture = try await BootHookTestFixture.afterWrite(.restore, .restoredSecondaryBank)
        let store = fixture.store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertFalse(store.showsManualPowerCycleNotice)
        XCTAssertTrue(store.backupSnapshotStale)
        XCTAssertFalse(store.canPlan)
        XCTAssertTrue(fixture.maintenanceStore.canBackUpFirmware)
    }

    func testValidatedWriteMarksSnapshotStaleAndDisablesPlanning() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.patch)
        let store = fixture.store
        let presentation = fixture.presentation

        XCTAssertTrue(store.backupSnapshotStale)
        XCTAssertNil(store.plan)
        XCTAssertTrue(fixture.maintenanceStore.canBackUpFirmware)
        XCTAssertFalse(store.canPlan)
        XCTAssertFalse(store.canWritePatch)
        let calls = fixture.runner.calls.count
        XCTAssertEqual(store.planFirmware(mode: .patch, profile: fixture.profile).rejectionMessage, WorkflowLocalError.flashBackupRequired.message)
        XCTAssertEqual(fixture.runner.calls.count, calls)
        XCTAssertEqual(presentation.title(for: .backUpAndInspectFirmware), "Back Up and Inspect Again")
        XCTAssertTrue(presentation.warnings.contains("Firmware was written after this backup. Back up and inspect again before planning another flash action."))
    }

    func testAWriteTheHelperFindsUnneededSaysSoAndDropsThePlan() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.notNeeded)
        ))
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch), .write(.patch))

        let presentation = fixture.presentation
        XCTAssertEqual(store.state, .writeNotNeeded)
        XCTAssertEqual(presentation.stateTitle, "No Write Needed")
        XCTAssertEqual(presentation.statusMessage, "Flash write was not needed.")
        // Nothing was written: the backup still holds, and only its plan goes.
        XCTAssertNil(store.plan)
        XCTAssertFalse(store.backupSnapshotStale)
        XCTAssertFalse(store.showsManualPowerCycleNotice)
        XCTAssertFalse(presentation.isEnabled(.writePatch))
        XCTAssertTrue(presentation.isEnabled(.planPatch))
        XCTAssertTrue(presentation.isEnabled(.planRestore))
    }

    func testAnOkWriteThatSaysNeitherKeepsThePlan() async throws {
        // Ok, firmware untouched, and not the helper's "not_needed": nothing
        // says the plan was wrong.
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.notNeeded, [
                "write_status": .string("skipped"),
                "summary_key": .string("flash_write_completed"),
            ])
        ))
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch), .write(.patch))

        XCTAssertEqual(store.state, .planAvailable)
        XCTAssertNotNil(store.plan)
        XCTAssertFalse(store.backupSnapshotStale)
        XCTAssertEqual(fixture.presentation.statusMessage, "Flash write completed.")
    }

    func testWriteFirmwareRefusesWithoutAPlanItCanWrite() async throws {
        let cases: [(name: String, plan: JSONValue?, mode: FlashPlanMode)] = [
            ("no plan", nil, .patch),
            ("a patch plan asked to restore", testBootHookPlanPayload(mode: .patch), .restore),
            ("a plan that writes nothing", testBootHookSatisfiedPatchPlanPayload(), .patch),
        ]
        for (name, plan, mode) in cases {
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(plan: plan ?? testBootHookPlanPayload(mode: .patch)))
            let store = fixture.store
            if plan == nil {
                try await fixture.run(.backup)
            } else {
                try await fixture.run(.backup, .plan(.patch))
            }
            let calls = fixture.runner.calls.count
            let keptPlan = store.plan

            XCTAssertEqual(
                store.writeFirmware(mode: mode, password: "pw", profile: fixture.profile).rejectionMessage,
                WorkflowLocalError.flashPlanRequired.message, name
            )
            XCTAssertEqual(store.error?.code, WorkflowLocalError.flashPlanRequired.code, name)
            XCTAssertEqual(store.plan, keptPlan, name)
            XCTAssertEqual(fixture.runner.calls.count, calls, name)
        }
    }

    func testFreshBackupClearsStaleSnapshotAfterWrite() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.patchNeedsPowerCycle)
        ) + testBootHookResponses(backup: testBootHookBackupPayload(banks: [
            testBootHookBank(name: "primary", backupValid: true, login: "already_patched"),
            testBootHookBank(name: "secondary", backupValid: true, login: "stock")
        ])))
        let store = fixture.store

        try await fixture.run(.backup, .plan(.patch), .write(.patch))
        XCTAssertTrue(store.backupSnapshotStale)

        try await fixture.run(.backup)
        XCTAssertFalse(store.backupSnapshotStale)
        XCTAssertEqual(store.state, .planAvailable)

        XCTAssertTrue(store.canPlan)
        XCTAssertEqual(fixture.presentation.title(for: .backUpAndInspectFirmware), "Back Up and Inspect")
        XCTAssertEqual(row("Boot Hook", in: fixture.presentation), "Installed")
    }

    func testStatusLineUsesWriteResultSummaryAfterWrite() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.patch)

        let presentation = fixture.presentation

        XCTAssertEqual(presentation.statusMessage, "Flash patch write validated; manual power cycle required.")
    }

    func testBackupRowsReportBankHealth() async throws {
        let cases: [(primary: Bool, secondary: Bool, expected: String)] = [
            (true, true, "Both valid"),
            (true, false, "Primary valid, secondary failed checks"),
            (false, true, "Primary failed checks, secondary valid"),
            (false, false, "Both failed checks")
        ]
        for testCase in cases {
            let fixture = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(activeBank: nil, banks: [
                testBootHookBank(name: "primary", backupValid: testCase.primary, login: "stock"),
                testBootHookBank(name: "secondary", backupValid: testCase.secondary, login: "stock")
            ]))

            let presentation = fixture.presentation

            XCTAssertEqual(row("Firmware Banks", in: presentation), testCase.expected, "\(testCase)")
        }
    }

    func testActiveBankRowShowsOnlyABankTheHelperPicked() async throws {
        let cases: [(activeBank: String?, primaryCandidate: Bool, secondaryCandidate: Bool, expected: String?)] = [
            // Both banks passing: the normal state, with no bank to name.
            (nil, true, true, nil),
            ("primary", true, false, "Primary"),
            // Both banks valid but only the secondary matches the running
            // kernel: the reason a patch of the primary is refused.
            ("secondary", false, true, "Secondary")
        ]
        for testCase in cases {
            let fixture = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(activeBank: testCase.activeBank, banks: [
                testBootHookBank(name: "primary", backupValid: true, activeCandidate: testCase.primaryCandidate, login: "stock"),
                testBootHookBank(name: "secondary", backupValid: true, activeCandidate: testCase.secondaryCandidate, login: "stock")
            ]))

            let presentation = fixture.presentation

            XCTAssertEqual(row("Active Bank", in: presentation), testCase.expected, "\(testCase)")
            XCTAssertEqual(row("Firmware Banks", in: presentation), "Both valid", "\(testCase)")
        }
    }

    func testBootHookRowFollowsThePrimaryBankLoginScript() async throws {
        let cases: [(primary: String?, secondary: String?, expected: String)] = [
            ("stock", "stock", "Not installed"),
            ("already_patched", "stock", "Installed"),
            // Only the primary bank is patched, so a patched secondary does not count.
            ("stock", "already_patched", "Not installed"),
            ("unknown", "stock", "Not recognized"),
            (nil, "stock", "Not recognized")
        ]
        for testCase in cases {
            let fixture = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(banks: [
                testBootHookBank(name: "primary", backupValid: testCase.primary != nil, login: testCase.primary),
                testBootHookBank(name: "secondary", backupValid: true, login: testCase.secondary)
            ]))

            XCTAssertEqual(row("Boot Hook", in: fixture.presentation), testCase.expected, "\(testCase)")
        }
    }

    func testBankRowsLeaveWithTheStaleBackupAfterAWrite() async throws {
        let fixture = try await BootHookTestFixture.afterWrite(.patch)

        let presentation = fixture.presentation

        // The backup still says the hook is not installed; the write just changed that.
        XCTAssertNil(row("Boot Hook", in: presentation))
        XCTAssertNil(row("Firmware Banks", in: presentation))
        XCTAssertNil(row("Active Bank", in: presentation))
        XCTAssertEqual(row("Backup", in: presentation), "/tmp/flash-backup")
        XCTAssertTrue(presentation.warnings.contains("Firmware was written after this backup. Back up and inspect again before planning another flash action."))
    }

    func testWriteInProgressDoesNotAskForWritePatch() async throws {
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch)
        ) + [
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "write_primary_bank", risk: "remote_write", cancellable: false)
            ], pauseAfterEvents: true)
        ]))
        defer { fixture.runner.finishAll() }
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch))

        fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        // The store says writing as soon as it starts; wait for the helper's
        // own write stage, which comes only once the helper runs.
        try await waitUntilStoreState { store.currentStage?.stage == "write_primary_bank" }

        let presentation = fixture.presentation
        XCTAssertEqual(store.state, .writing)
        XCTAssertNotNil(store.plan)
        XCTAssertFalse(presentation.isEnabled(.writePatch))
        XCTAssertEqual(presentation.statusMessage, "Flash patch write plan generated.")
        // The backup is there; the store is busy, and says so.
        XCTAssertEqual(
            store.planFirmware(mode: .restore, profile: fixture.profile).rejectionMessage,
            WorkflowLocalError.operationAlreadyRunning.message
        )
        XCTAssertEqual(fixture.runner.calls.count, 3)
    }

    func testFailedWriteAsksForWritePatchOnlyWhenFirmwareWasUntouched() async throws {
        let cases: [(stage: String, asksForWritePatch: Bool)] = [
            // Refused before anything was written: the plan still holds.
            ("pre_write_validation", true),
            // Failed while writing: the backup no longer describes the banks.
            ("write_primary_bank", false)
        ]
        for testCase in cases {
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .patch),
                failure: [
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "remote_write", cancellable: false),
                    testBootHookError("write failed")
                ]
            ))
            let store = fixture.store
            try await fixture.run(.backup, .plan(.patch))
            fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { store.state == .failed && !store.isBusy }

            let presentation = fixture.presentation

            XCTAssertEqual(presentation.isEnabled(.writePatch), testCase.asksForWritePatch, testCase.stage)
            XCTAssertEqual(
                presentation.statusMessage == "Patch plan ready. Choose “Write Patch” to install the boot hook.",
                testCase.asksForWritePatch,
                testCase.stage
            )
        }
    }

    func testAWriteResultTheAppCannotUseStillMarksTheBackupStaleOnceTheWriteBegan() async throws {
        let unreadable = BackendEvent(type: "result", operation: "flash", ok: true, payload: .object(["schema_version": .number(1)]))
        let failed = BackendEvent(type: "result", operation: "flash", ok: false, payload: testSummaryPayload("Flash write failed.", key: "flash_write_failed"))
        let cases: [(name: String, result: BackendEvent, stage: String, backupStale: Bool)] = [
            ("unreadable result after writing", unreadable, "write_primary_bank", true),
            ("false result after writing", failed, "write_primary_bank", true),
            // Refused before anything was written: the backup still holds.
            ("unreadable result before writing", unreadable, "pre_write_validation", false),
            ("false result before writing", failed, "pre_write_validation", false),
        ]
        for testCase in cases {
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .patch),
                failure: [
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "remote_write", cancellable: false),
                    testCase.result
                ]
            ))
            let store = fixture.store
            try await fixture.run(.backup, .plan(.patch))
            fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { store.error != nil && !store.isBusy }

            XCTAssertEqual(store.state, .failed, testCase.name)
            XCTAssertEqual(store.backupSnapshotStale, testCase.backupStale, testCase.name)
            XCTAssertEqual(store.plan == nil, testCase.backupStale, testCase.name)
            XCTAssertEqual(fixture.maintenanceStore.canWritePatch, !testCase.backupStale, testCase.name)
        }
    }

    func testEachHelperStageShowsItsBadge() async throws {
        let cases: [(stage: String, state: BootHookState)] = [
            ("read_flash", .readingBanks),
            ("save_raw_backup", .savingBackup),
            ("inspect_backup", .analyzingBanks),
            ("analyze_flash", .analyzingBanks),
            ("plan_flash", .analyzingBanks),
            ("confirm_write", .awaitingStrongConfirmation),
            ("pre_write_validation", .readbackValidating),
            ("post_write_validation", .readbackValidating),
            ("write_primary_bank", .writing),
            ("write_active_bank", .writing),
            ("write_secondary_bank", .writing),
            ("reboot", .restoreRebooting),
            ("wait_for_reboot_down", .restoreRebooting),
            ("wait_for_reboot_up", .restoreRebooting),
            // A stage the app does not know keeps the badge it had.
            ("some_new_stage", .savingBackup),
        ]
        for testCase in cases {
            // A first stage that shows another badge, so a stage the app maps
            // wrongly cannot pass by leaving the badge as it was.
            let before = testCase.state == .savingBackup && testCase.stage != "some_new_stage" ? "read_flash" : "save_raw_backup"
            let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "stage", operation: "flash", stage: before, risk: "read_only", cancellable: false),
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "read_only", cancellable: false),
                ], pauseAfterEvents: true)
            ]))
            defer { fixture.runner.finishAll() }
            fixture.store.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { fixture.store.currentStage?.stage == testCase.stage }
            XCTAssertEqual(fixture.store.state, testCase.state, testCase.stage)
        }
    }

    func testAWriteThatFailsMarksTheBackupStaleOnlyOnceItMayHaveWritten() async throws {
        let cases: [(stage: String, backupStale: Bool)] = [
            ("inspect_backup", false),
            ("plan_flash", false),
            ("confirm_write", false),
            ("pre_write_validation", false),
            ("write_primary_bank", true),
            ("write_active_bank", true),
            ("write_secondary_bank", true),
            ("post_write_validation", true),
            ("reboot", true),
            ("wait_for_reboot_down", true),
            ("wait_for_reboot_up", true),
        ]
        for testCase in cases {
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .restore),
                failure: [
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "remote_write", cancellable: false),
                    testBootHookError("write failed")
                ]
            ))
            let store = fixture.store
            try await fixture.run(.backup, .plan(.restore))
            fixture.maintenanceStore.writeFirmware(mode: .restore, password: "pw", profile: fixture.profile)
            try await waitUntilStoreState { store.error != nil && !store.isBusy }

            XCTAssertEqual(store.backupSnapshotStale, testCase.backupStale, testCase.stage)
            XCTAssertEqual(store.plan == nil, testCase.backupStale, testCase.stage)
            XCTAssertEqual(fixture.maintenanceStore.canWriteRestore, !testCase.backupStale, testCase.stage)
        }
    }

    func testOnlyAFieldChangeThatChangesTheSelectionDropsThePlan() async throws {
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(plan: testBootHookPlanPayload(mode: .patch)))
        let store = fixture.store
        store.firmwareVersion = "7.8.1"
        try await fixture.run(.backup, .plan(.patch))

        // Spaces around the same value are no change.
        store.firmwareVersion = " 7.8.1 "
        XCTAssertNotNil(store.plan)
        XCTAssertTrue(fixture.maintenanceStore.canWritePatch)

        // Any real change drops the plan, and setting the value back does not
        // bring it back: the plan is gone, not hidden.
        store.firmwareVersion = "7.9"
        XCTAssertNil(store.plan)
        store.firmwareVersion = "7.8.1"
        XCTAssertNil(store.plan)
        XCTAssertEqual(store.state, .planAvailable)
        XCTAssertFalse(fixture.maintenanceStore.canWritePatch)
    }

    func testAFieldChangeAfterAWriteDropsItsResultWithThePlan() async throws {
        // The one state with both a plan and a write result: an ok write that
        // neither changed the firmware nor found nothing to do.
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            write: testBootHookWritePayload(.notNeeded, [
                "write_status": .string("skipped"),
                "summary_key": .string("flash_write_completed"),
            ])
        ))
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch), .write(.patch))
        XCTAssertNotNil(store.plan)
        XCTAssertNotNil(store.writeResult)

        store.firmwareTemplatePath = "/tmp/other.basebinary"
        XCTAssertNil(store.plan)
        XCTAssertNil(store.writeResult)
    }

    func testASelectionChangedWhileAPlanRunsDiscardsThatPlan() async throws {
        let responses = testBootHookResponses(plan: testBootHookPlanPayload(mode: .patch))
        // Only the plan waits, so the test can change a field during it.
        let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: responses.indices.map {
            .init(events: responses[$0].events, result: responses[$0].result, pauseBeforeEvents: $0 == responses.count - 1)
        }))
        defer { fixture.runner.finishAll() }
        let store = fixture.store
        try await fixture.run(.backup)
        fixture.maintenanceStore.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { fixture.runner.calls.count == 2 }
        store.firmwareVersion = "7.9"

        fixture.runner.finishAll()
        try await waitUntilStoreState { !store.isBusy }
        XCTAssertNil(store.plan)
        XCTAssertNil(store.error)
        XCTAssertEqual(store.state, .planAvailable)
        XCTAssertFalse(fixture.maintenanceStore.canWritePatch)
        XCTAssertTrue(fixture.maintenanceStore.canPlanFirmware)
    }

    func testACancelledRunEndsAsAFailureAndKeepsWhatCameBefore() async throws {
        // A cancel ends a backup, or a plan made from one, as a failure; the
        // other maintenance workflows treat a cancel the same way.
        for cancelPlan in [false, true] {
            let stage = BackendEvent(
                type: "stage", operation: "flash", stage: cancelPlan ? "plan_flash" : "read_flash", risk: "read_only", cancellable: true
            )
            let responses = cancelPlan ? testBootHookResponses() + [.init(events: [stage])] : [.init(events: [stage])]
            let fixture = try BootHookTestFixture(
                runner: PausingStoreTestRunner.pausing(responses, after: { $0 == responses.count - 1 }), onDeviceLane: true
            )
            defer { fixture.runner.finishAll() }
            let store = fixture.store
            if cancelPlan {
                try await fixture.run(.backup)
                fixture.maintenanceStore.planFirmware(mode: .patch, profile: fixture.profile)
            } else {
                fixture.maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: fixture.profile)
            }
            try await waitUntilStoreState { fixture.coordinator.canCancel(profileID: fixture.profile.id) }

            fixture.coordinator.cancel(profileID: fixture.profile.id)
            try await waitUntilStoreState { store.error != nil && !fixture.maintenanceStore.isBusy }
            XCTAssertEqual(store.state, .failed, "plan: \(cancelPlan)")
            XCTAssertEqual(store.error?.code, "cancelled", "plan: \(cancelPlan)")
            XCTAssertNil(store.plan, "plan: \(cancelPlan)")
            XCTAssertEqual(store.backup != nil, cancelPlan, "plan: \(cancelPlan)")
            XCTAssertTrue(fixture.maintenanceStore.canBackUpFirmware, "plan: \(cancelPlan)")
            XCTAssertEqual(fixture.maintenanceStore.canPlanFirmware, cancelPlan, "plan: \(cancelPlan)")
        }
    }

    func testEitherSignOfAWriteMarksTheBackupStale() async throws {
        // A write that may have changed the device without being validated,
        // and a validated write whose record says nothing about the device.
        let cases: [(name: String, write: JSONValue)] = [
            ("may have changed the device", testBootHookWritePayload(.unvalidated)),
            ("validated", testBootHookWritePayload(.patchNeedsPowerCycle, [
                "write_outcome": .object([
                    "status": .string("validated"), "mode": .string("patch"), "write_validated": .bool(true),
                    "write_may_have_modified_device": .bool(false),
                ])
            ])),
        ]
        for (name, write) in cases {
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(plan: testBootHookPlanPayload(mode: .patch), write: write))
            try await fixture.run(.backup, .plan(.patch), .write(.patch))
            XCTAssertTrue(fixture.store.backupSnapshotStale, name)
            XCTAssertNil(fixture.store.plan, name)
        }
    }

    func testAFalseResultKeepsTheHelpersOwnCode() async throws {
        let failed = BackendEvent(type: "result", operation: "flash", code: "secondary_bank_invalid", ok: false,
                                  payload: testSummaryPayload("Patching was refused.", key: "flash_patch_refused"))
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(failure: [failed]))
        try await fixture.run(.backup)
        fixture.maintenanceStore.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { fixture.store.error != nil && !fixture.store.isBusy }

        XCTAssertEqual(fixture.store.state, .failed)
        XCTAssertEqual(fixture.store.error?.code, "secondary_bank_invalid")
    }

    func testAConfirmationTheAppCannotReadFailsTheWrite() async throws {
        // No confirmation details: nothing to confirm, and the helper exits.
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: .patch),
            failure: [BackendEvent(type: "error", operation: "flash", code: "confirmation_required", message: "Confirm?")]
        ))
        let store = fixture.store
        try await fixture.run(.backup, .plan(.patch))
        fixture.maintenanceStore.writeFirmware(mode: .patch, password: "pw", profile: fixture.profile)
        try await waitUntilStoreState { store.error != nil && !store.isBusy }

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.error?.code, "contract_decode_failed")
        XCTAssertNil(store.pendingConfirmation)
        // Nothing was written, so the plan still holds.
        XCTAssertNotNil(store.plan)
        XCTAssertTrue(fixture.maintenanceStore.canWritePatch)
        // The run is over: a field change drops the plan at once.
        store.firmwareVersion = "7.9.1"
        XCTAssertNil(store.plan)
    }

    func testPlansThatCannotWriteAPatchKeepTheirOwnSummary() async throws {
        let cases: [(plan: JSONValue, expected: String)] = [
            (testBootHookPlanPayload(mode: .restore), "Flash restore write plan generated."),
            (testBootHookSatisfiedPatchPlanPayload(), "Flash plan is already satisfied; no write is needed.")
        ]
        for testCase in cases {
            let fixture = try await BootHookTestFixture.afterPlan(testCase.plan)

            let presentation = fixture.presentation

            XCTAssertFalse(presentation.isEnabled(.writePatch), testCase.expected)
            XCTAssertEqual(presentation.statusMessage, testCase.expected)
        }
    }

    func testFlashFailureShowsInAnErrorBoxNotInTheStatusLine() async throws {
        let failure = "refusing to patch primary because the secondary (backup) firmware bank is not a valid backup"
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(failure: [testBootHookError(failure)]))
        let store = fixture.store
        try await fixture.run(.backup)
        fixture.maintenanceStore.planFirmware(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { store.error != nil && !store.isBusy }

        let error = try XCTUnwrap(store.error)
        XCTAssertEqual(fixture.presentation.error, error)
        XCTAssertNotEqual(fixture.presentation.statusMessage, error.message)
        XCTAssertEqual(error.diagnosticMessage, failure)
        // The flash section's own buttons rerun each step, so the dashboard
        // gives its box no retry (DashboardStoreTests).
        XCTAssertEqual(error.operation, "flash")
    }

    private func writeConfirmationRequired() -> BackendEvent {
        BackendEvent(
            type: "error",
            operation: "flash",
            code: "confirmation_required",
            message: "Confirm?",
            details: .object([
                "confirmation_id": .string("confirm-1"),
                "presentation_id": .string("flash.patch_write"),
                "presentation_values": .object(["host": .string("10.0.0.2")])
            ])
        )
    }

    private func row(_ label: String, in presentation: MaintenanceWorkflowDetailPresentation) -> String? {
        presentation.bootHookRows.first { $0.label == label }?.value
    }
}
