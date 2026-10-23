import AppKit
import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class FlashWorkflowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(FlashWorkflowState.allCases, [
            .unavailable,
            .disabledInThisBuild,
            .eligibleForReadOnlyAnalysis,
            .readingBanks,
            .savingBackup,
            .analyzingBanks,
            .planAvailable,
            .appleCheckComplete,
            .appleFirmwareMismatch,
            .appleFirmwareReady,
            .writeLocked,
            .awaitingStrongConfirmation,
            .writing,
            .readbackValidating,
            .writeValidated,
            .writeValidatedSnapshotStale,
            .restoreRebooting,
            .failed
        ])
    }

    func testDefaultPolicyEnablesFlashWritesForNetBSD4() throws {
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        let store = FlashWorkflowStore()

        store.refresh(profile: profile)

        XCTAssertEqual(store.state, .writeLocked)
        XCTAssertTrue(store.canBackup)
    }

    func testReadOnlyPolicyAllowsAnalysisButNotWrites() throws {
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .readOnly)

        XCTAssertEqual(eligibility.state, .eligibleForReadOnlyAnalysis)
        XCTAssertTrue(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testNonNetBSD4DeviceIsUnavailable() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .writesEnabled)

        XCTAssertEqual(eligibility.state, .unavailable)
        XCTAssertFalse(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testBootHookSectionVisibilityIsLimitedToNetBSD4Profiles() throws {
        let netbsd4 = try makeProfile(payloadFamily: "netbsd4_samba4")
        let netbsd6 = try makeProfile(payloadFamily: "netbsd6_samba4")

        XCTAssertTrue(FlashBootHookVisibilityPolicy.isVisible(for: netbsd4))
        XCTAssertFalse(FlashBootHookVisibilityPolicy.isVisible(for: netbsd6))
    }

    func testFlashActionSymbolsResolveToSFSymbols() {
        XCTAssertEqual(FlashUserAction.backupAndInspect.systemImage, "externaldrive.badge.questionmark")
        for action in [
            FlashUserAction.backupAndInspect,
            .planPatch,
            .planRestore,
            .checkApple,
            .downloadApple,
            .writePatch,
            .writeRestore
        ] {
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
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "read_flash", risk: "remote_read", cancellable: true),
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "plan_flash", risk: "local_write", cancellable: true),
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        XCTAssertEqual(store.state, .readingBanks)
        try await waitUntilStoreState { store.backup != nil && store.state == .planAvailable && !store.isBusy }

        store.planFlash(mode: .patch, profile: profile)
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
            let payloads = [flashBackupPayload(), flashPlanPayload(mode: .patch, writeRequested: true), flashWritePayload(mode: .patch)]
            let runner = PausingStoreTestRunner(responses: payloads.enumerated().map { index, payload in
                .init(events: [BackendEvent(type: "result", operation: "flash", ok: true, payload: payload)], pauseAfterEvents: index == pausedStep)
            })
            defer { runner.finishAll() }
            let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
            let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            store.refresh(profile: profile)
            for step in 0...2 {
                switch step {
                case 0: store.backupAndInspect(password: "pw", profile: profile)
                case 1: store.planFlash(mode: .patch, profile: profile)
                default: store.write(mode: .patch, password: "pw", profile: profile)
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
                    XCTAssertFalse(store.canBackup)
                    XCTAssertFalse(store.canPlan)
                    XCTAssertFalse(store.canWritePatch)
                    XCTAssertEqual(runner.calls.count, step + 1)
                    runner.finishAll()
                }
                try await waitUntilStoreState { !store.isBusy }
            }
            XCTAssertEqual(runner.calls.map { $0.params["action"] }, [.string("backup"), .string("plan"), .string("write")])
            XCTAssertTrue(store.backupSnapshotStale)
            XCTAssertTrue(store.canBackup)
        }
    }

    func testPublishesWhenBackendFinishesAfterBackupResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)
        let finishPublished = expectation(description: "FlashWorkflowStore publishes after backend running state clears")
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

        store.backupAndInspect(password: "pw", profile: profile)

        try await waitUntilStoreState { store.state == .planAvailable }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isBusy)
        _ = cancellables
    }

    func testPlanFlashCarriesAppleFirmwareSelectionOptions() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .downloadOnly, writeRequested: false))
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.firmwareVersion = " 7.8.1 "
        store.firmwareTemplatePath = " /tmp/firmware.basebinary "

        store.planFlash(mode: .downloadOnly, profile: profile)
        try await waitUntilStoreState { runner.calls.count == 2 && store.plan != nil && !store.isBusy }

        XCTAssertEqual(runner.calls[1].params["firmware_version"], .string("7.8.1"))
        XCTAssertEqual(runner.calls[1].params["firmware_template"], .string("/tmp/firmware.basebinary"))
    }

    func testFirmwareSelectionEditsInvalidateExistingWritePlan() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.firmwareVersion = "7.8.1"
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.canWritePatch }

        store.firmwareVersion = "7.8.2"

        XCTAssertNil(store.plan)
        XCTAssertFalse(store.canWritePatch)
        XCTAssertEqual(store.state, .planAvailable)
    }

    func testAppleCheckPresentationShowsMatchDetails() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "flash",
                    ok: true,
                    payload: flashPlanPayload(
                        mode: .checkApple,
                        writeRequested: false,
                        alreadySatisfied: true,
                        appleMatched: true
                    )
                )
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .checkApple, profile: profile)
        try await waitUntilStoreState { store.state == .appleCheckComplete }

        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Active firmware bank matches Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match", value: "yes")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Version", value: "7.8.1")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Payload SHA-256", value: "inner-sha")))
    }

    func testAppleCheckPresentationShowsMultiBankMatchDetails() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "flash",
                    ok: true,
                    payload: flashPlanPayload(
                        mode: .checkApple,
                        writeRequested: false,
                        alreadySatisfied: true,
                        appleMatched: true,
                        includeAppleFirmwareMatches: true
                    )
                )
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .checkApple, profile: profile)
        try await waitUntilStoreState { store.state == .appleCheckComplete }

        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "All candidate firmware banks match Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match (primary)", value: "yes")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match (secondary)", value: "yes")))
    }

    func testAppleCheckMismatchUsesDedicatedState() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "flash",
                    ok: true,
                    payload: flashPlanPayload(
                        mode: .checkApple,
                        writeRequested: false,
                        alreadySatisfied: false,
                        appleMatched: false
                    )
                )
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .checkApple, profile: profile)
        try await waitUntilStoreState { store.state == .appleFirmwareMismatch }

        XCTAssertTrue(FlashPresentation(store: store).rows.contains(PresentationRow(label: "Apple Match", value: "no")))
    }

    func testValidateAppleRestoreFirmwarePresentationShowsPayloadDetails() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "flash",
                    ok: true,
                    payload: flashPlanPayload(mode: .downloadOnly, writeRequested: false, includeFirmwarePayload: true)
                )
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .downloadOnly, profile: profile)
        try await waitUntilStoreState { store.state == .appleFirmwareReady }

        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Apple restore firmware validated (version 7.8.1, product 116).")
        XCTAssertEqual(presentation.title(for: .downloadApple), "Validate Apple Restore Firmware")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload", value: "/tmp/flash-backup/primary.download_only.basebinary")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload SHA-256", value: "payload-sha")))
    }

    func testWriteConfirmationCancellationRestoresPlanAvailable() async throws {
        for mode in [FlashPlanMode.patch, .restore] {
            let runner = PausingStoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
                ]),
                .init(events: [
                    BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: mode, writeRequested: true))
                ]),
                .init(events: [
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
                ], pauseAfterEvents: true)
            ])
            defer { runner.finishAll() }
            let backend = BackendClient(runner: runner)
            let coordinator = OperationCoordinator(backend: backend)
            let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            let store = FlashWorkflowStore(coordinator: coordinator, laneKey: .deviceWorkflow(profile.id, .flash))
            store.refresh(profile: profile)

            store.backupAndInspect(password: "pw", profile: profile)
            try await waitUntilStoreState { store.backup != nil && !store.isBusy }
            store.planFlash(mode: mode, profile: profile)
            try await waitUntilStoreState { store.plan != nil && !store.isBusy }

            store.write(mode: mode, password: "pw", profile: profile)
            try await waitUntilStoreState { store.state == .awaitingStrongConfirmation }
            if mode == .patch {
                // Write Patch was just pressed; the plan's own summary stays true.
                XCTAssertEqual(FlashPresentation(store: store).message, "Flash patch write plan generated.")
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
                XCTAssertEqual(FlashPresentation(store: store).message, "Patch plan ready. Choose “Write Patch” to install the boot hook.")
            }
            XCTAssertFalse(coordinator.isDeviceBusy(profile))
            XCTAssertFalse(store.isBusy)
            XCTAssertNil(store.currentStage)
            XCTAssertNil(coordinator.readyConfirmation)
            XCTAssertEqual(runner.calls.count, 3)
        }
    }

    func testValidatedPatchWriteShowsManualPowerCycleNotice() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertEqual(store.manualPowerCycleNotice?.mode, .patch)
        XCTAssertEqual(
            store.manualPowerCycleNotice?.message,
            "Flash write validation completed. Unplug the device, wait 10 seconds, then plug it back in. Wait for it to finish booting, then run “Checkup”. One firmware bank was left untouched."
        )
        XCTAssertEqual(store.manualPowerCycleNotice?.viewCheckupActionTitle, "View Checkup")

        store.dismissManualPowerCycleNotice()

        XCTAssertNil(store.manualPowerCycleNotice)
    }

    func testValidatedRestoreWriteWithDefaultRebootDoesNotShowManualPowerCycleNotice() async throws {
        let store = try await storeAfterValidatedWrite(mode: .restore)

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertNil(store.manualPowerCycleNotice)
        XCTAssertFalse(FlashPresentation(store: store).warnings.contains(
            "Unplug the device, wait 10 seconds, then plug it back in."
        ))
    }

    func testValidatedRestoreWriteWithoutRebootShowsManualPowerCycleNotice() async throws {
        let store = try await storeAfterValidatedWrite(
            mode: .restore,
            writePayload: flashWritePayload(
                mode: .restore,
                postWriteAction: "manual_reboot",
                rebootRequested: false,
                rebooted: false,
                waitedAfterReboot: false,
                summary: "Flash restore write validated; manual reboot required."
            )
        )

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertEqual(store.manualPowerCycleNotice?.mode, .restore)
    }

    func testValidatedSecondaryBankRestoreShowsNoPowerCycleNoticeAndAsksForAFreshBackup() async throws {
        // The device keeps running its primary bank, so nothing needs restarting,
        // but the backup no longer describes the rewritten secondary bank.
        let store = try await storeAfterValidatedWrite(
            mode: .restore,
            writePayload: flashWritePayload(
                mode: .restore,
                postWriteAction: "none",
                rebootRequested: false,
                rebooted: false,
                waitedAfterReboot: false,
                summary: "Secondary (backup) firmware bank restored and verified; no reboot needed."
            )
        )

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertNil(store.manualPowerCycleNotice)
        XCTAssertTrue(store.backupSnapshotStale)
        XCTAssertFalse(store.canPlan)
        XCTAssertTrue(store.canBackup)
    }

    func testValidatedWriteMarksSnapshotStaleAndDisablesPlanning() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)
        let presentation = FlashPresentation(store: store)

        XCTAssertTrue(store.backupSnapshotStale)
        XCTAssertNil(store.plan)
        XCTAssertTrue(store.canBackup)
        XCTAssertFalse(store.canPlan)
        XCTAssertFalse(store.canPlanWrites)
        XCTAssertFalse(store.canWritePatch)
        XCTAssertEqual(presentation.title(for: .backupAndInspect), "Back Up and Inspect Again")
        XCTAssertTrue(presentation.warnings.contains("Firmware was written after this backup. Back up and inspect again before planning another flash action."))
    }

    func testFreshBackupClearsStaleSnapshotAfterWrite() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashWritePayload(mode: .patch))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload(banks: [
                    flashBank(name: "primary", backupValid: true, login: "already_patched"),
                    flashBank(name: "secondary", backupValid: true, login: "stock")
                ]))
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.plan != nil && !store.isBusy }
        store.write(mode: .patch, password: "pw", profile: profile)
        try await waitUntilStoreState { store.backupSnapshotStale && !store.isBusy }

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { !store.backupSnapshotStale && store.state == .planAvailable && !store.isBusy }

        XCTAssertTrue(store.canPlan)
        XCTAssertEqual(FlashPresentation(store: store).title(for: .backupAndInspect), "Back Up and Inspect")
        XCTAssertEqual(row("Boot Hook", in: FlashPresentation(store: store)), "Installed")
    }

    func testFlashPresentationUsesWriteResultSummaryAfterWrite() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)

        let presentation = FlashPresentation(store: store)

        XCTAssertEqual(presentation.message, "Flash patch write validated; manual power cycle required.")
    }

    func testBackupRowsReportBankHealth() async throws {
        let cases: [(primary: Bool, secondary: Bool, expected: String)] = [
            (true, true, "Both valid"),
            (true, false, "Primary valid, secondary failed checks"),
            (false, true, "Primary failed checks, secondary valid"),
            (false, false, "Both failed checks")
        ]
        for testCase in cases {
            let store = try await storeAfterBackup(flashBackupPayload(activeBank: nil, banks: [
                flashBank(name: "primary", backupValid: testCase.primary, login: "stock"),
                flashBank(name: "secondary", backupValid: testCase.secondary, login: "stock")
            ]))

            let presentation = FlashPresentation(store: store)

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
            let store = try await storeAfterBackup(flashBackupPayload(activeBank: testCase.activeBank, banks: [
                flashBank(name: "primary", backupValid: true, activeCandidate: testCase.primaryCandidate, login: "stock"),
                flashBank(name: "secondary", backupValid: true, activeCandidate: testCase.secondaryCandidate, login: "stock")
            ]))

            let presentation = FlashPresentation(store: store)

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
            let store = try await storeAfterBackup(flashBackupPayload(banks: [
                flashBank(name: "primary", backupValid: testCase.primary != nil, login: testCase.primary),
                flashBank(name: "secondary", backupValid: true, login: testCase.secondary)
            ]))

            XCTAssertEqual(row("Boot Hook", in: FlashPresentation(store: store)), testCase.expected, "\(testCase)")
        }
    }

    func testBankRowsLeaveWithTheStaleBackupAfterAWrite() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)

        let presentation = FlashPresentation(store: store)

        // The backup still says the hook is not installed; the write just changed that.
        XCTAssertNil(row("Boot Hook", in: presentation))
        XCTAssertNil(row("Firmware Banks", in: presentation))
        XCTAssertNil(row("Active Bank", in: presentation))
        XCTAssertEqual(row("Backup", in: presentation), "/tmp/flash-backup")
        XCTAssertTrue(presentation.warnings.contains("Firmware was written after this backup. Back up and inspect again before planning another flash action."))
    }

    func testWriteInProgressDoesNotAskForWritePatch() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "flash", stage: "write_primary_bank", risk: "remote_write", cancellable: false)
            ], pauseAfterEvents: true)
        ])
        defer { runner.finishAll() }
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)
        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.plan != nil && !store.isBusy }

        store.write(mode: .patch, password: "pw", profile: profile)
        try await waitUntilStoreState { store.state == .writing }

        let presentation = FlashPresentation(store: store)
        XCTAssertNotNil(store.plan)
        XCTAssertFalse(presentation.isEnabled(.writePatch))
        XCTAssertEqual(presentation.message, "Flash patch write plan generated.")
    }

    func testFailedWriteAsksForWritePatchOnlyWhenFirmwareWasUntouched() async throws {
        let cases: [(stage: String, asksForWritePatch: Bool)] = [
            // Refused before anything was written: the plan still holds.
            ("pre_write_validation", true),
            // Failed while writing: the backup no longer describes the banks.
            ("write_primary_bank", false)
        ]
        for testCase in cases {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
                ]),
                .init(events: [
                    BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
                ]),
                .init(events: [
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "remote_write", cancellable: false),
                    BackendEvent(type: "error", operation: "flash", code: "validation_failed", message: "write failed")
                ])
            ])
            let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
            let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            store.refresh(profile: profile)
            store.backupAndInspect(password: "pw", profile: profile)
            try await waitUntilStoreState { store.backup != nil && !store.isBusy }
            store.planFlash(mode: .patch, profile: profile)
            try await waitUntilStoreState { store.plan != nil && !store.isBusy }
            store.write(mode: .patch, password: "pw", profile: profile)
            try await waitUntilStoreState { store.state == .failed && !store.isBusy }

            let presentation = FlashPresentation(store: store)

            XCTAssertEqual(presentation.isEnabled(.writePatch), testCase.asksForWritePatch, testCase.stage)
            XCTAssertEqual(
                presentation.message == "Patch plan ready. Choose “Write Patch” to install the boot hook.",
                testCase.asksForWritePatch,
                testCase.stage
            )
        }
    }

    func testPlansThatCannotWriteAPatchKeepTheirOwnSummary() async throws {
        let cases: [(mode: FlashPlanMode, writeRequested: Bool, alreadySatisfied: Bool, expected: String)] = [
            (.restore, true, false, "Flash restore write plan generated."),
            (.patch, false, true, "Flash plan is already satisfied; no write is needed.")
        ]
        for testCase in cases {
            let runner = StoreTestRunner(responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
                ]),
                .init(events: [
                    BackendEvent(
                        type: "result",
                        operation: "flash",
                        ok: true,
                        payload: flashPlanPayload(
                            mode: testCase.mode,
                            writeRequested: testCase.writeRequested,
                            alreadySatisfied: testCase.alreadySatisfied
                        )
                    )
                ])
            ])
            let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
            let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            store.refresh(profile: profile)
            store.backupAndInspect(password: "pw", profile: profile)
            try await waitUntilStoreState { store.backup != nil && !store.isBusy }
            store.planFlash(mode: testCase.mode, profile: profile)
            try await waitUntilStoreState { store.plan != nil && !store.isBusy }

            let presentation = FlashPresentation(store: store)

            XCTAssertFalse(presentation.isEnabled(.writePatch), "\(testCase)")
            XCTAssertEqual(presentation.message, testCase.expected, "\(testCase)")
        }
    }

    func testFlashFailureShowsInAnErrorBoxNotInTheStatusLine() async throws {
        let failure = "refusing to patch primary because the secondary (backup) firmware bank is not a valid backup"
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "error", operation: "flash", code: "validation_failed", message: failure)
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)
        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.error != nil && !store.isBusy }

        let error = try XCTUnwrap(store.error)
        XCTAssertNotEqual(FlashPresentation(store: store).message, error.message)
        XCTAssertEqual(error.diagnosticMessage, failure)
        // The flash section's own buttons rerun each step, so the dashboard
        // gives its box no retry (DashboardStoreTests).
        XCTAssertEqual(error.operation, "flash")
    }

    private func storeAfterValidatedWrite(
        mode: FlashPlanMode,
        writePayload: JSONValue? = nil
    ) async throws -> FlashWorkflowStore {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: mode, writeRequested: true))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: writePayload ?? flashWritePayload(mode: mode))
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        store.planFlash(mode: mode, profile: profile)
        try await waitUntilStoreState { store.plan != nil && !store.isBusy }
        store.write(mode: mode, password: "pw", profile: profile)
        try await waitUntilStoreState { store.writeResult != nil && !store.isBusy }
        return store
    }

    private func makeProfile(payloadFamily: String) throws -> DeviceProfile {
        DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }

    private func flashBackupPayload(
        activeBank: String? = "primary",
        banks: [JSONValue]? = nil
    ) -> JSONValue {
        let banks = banks ?? [
            flashBank(name: "primary", backupValid: true, login: "stock"),
            flashBank(name: "secondary", backupValid: true, login: "stock")
        ]
        return .object([
            "schema_version": .number(1),
            "backup_dir": .string("/tmp/flash-backup"),
            "host": .string("10.0.0.2"),
            "syap": .string("116"),
            "active_bank": activeBank.map(JSONValue.string) ?? .null,
            "banks": .array(banks),
            "counts": .object(["banks": .number(Double(banks.count))]),
            "summary": .string("Flash backup saved to /tmp/flash-backup.")
        ])
    }

    /// A bank as the helper reports it; `login: nil` is a bank whose analysis failed.
    /// A bank is an active candidate only if it is also a valid backup.
    private func flashBank(
        name: String,
        backupValid: Bool,
        activeCandidate: Bool? = nil,
        login: String?
    ) -> JSONValue {
        .object([
            "name": .string(name),
            "device": .string(name == "primary" ? "/dev/rflash0.raw" : "/dev/rflash1.raw"),
            "size": .number(128),
            "sha256": .string("abc"),
            "backup_valid": .bool(backupValid),
            "active_candidate": .bool(activeCandidate ?? backupValid),
            "would_write": .bool(false),
            "write_decision": .string("no write"),
            "login": login.map { .object(["classification": .string($0)]) } ?? .null
        ])
    }

    private func storeAfterBackup(_ payload: JSONValue) async throws -> FlashWorkflowStore {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: payload)
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)
        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil && !store.isBusy }
        return store
    }

    private func row(_ label: String, in presentation: FlashPresentation) -> String? {
        presentation.rows.first { $0.label == label }?.value
    }

    private func flashPlanPayload(
        mode: FlashPlanMode,
        writeRequested: Bool,
        alreadySatisfied: Bool = false,
        appleMatched: Bool? = nil,
        includeFirmwarePayload: Bool = false,
        includeAppleFirmwareMatches: Bool = false
    ) -> JSONValue {
        var payload: [String: JSONValue] = [
            "schema_version": .number(1),
            "backup_dir": .string("/tmp/flash-backup"),
            "mode": .string(mode.rawValue),
            "write_requested": .bool(writeRequested),
            "already_satisfied": .bool(alreadySatisfied),
            "active_bank": .string("primary"),
            "banks": .array([]),
            "flash_plan": .object(["mode": .string(mode.rawValue)]),
            "summary": .string(summary(for: mode, writeRequested: writeRequested, alreadySatisfied: alreadySatisfied))
        ]
        if let appleMatched {
            payload["apple_firmware_match"] = .object([
                "matched": .bool(appleMatched),
                "template_source": .string("catalog"),
                "template_product_id": .string("116"),
                "template_version": .string("7.8.1"),
                "template_sha256": .string("template-sha"),
                "inner_sha256": .string("inner-sha"),
                "inner_size": .number(123),
                "key_id": .string("key-one"),
                "inner_model": .number(116),
                "inner_version": .string("0x00070801")
            ])
        }
        if includeAppleFirmwareMatches {
            let match: [String: JSONValue] = [
                "matched": .bool(true),
                "template_source": .string("catalog"),
                "template_product_id": .string("116"),
                "template_version": .string("7.8.1"),
                "template_sha256": .string("template-sha"),
                "inner_sha256": .string("inner-sha"),
                "inner_size": .number(123),
                "key_id": .string("key-one"),
                "inner_model": .number(116),
                "inner_version": .string("0x00070801")
            ]
            payload["apple_match_status"] = .string("all_candidates_match")
            payload["apple_firmware_matches"] = .array([
                .object(["bank": .string("primary"), "match": .object(match)]),
                .object(["bank": .string("secondary"), "match": .object(match)])
            ])
            payload["summary"] = .string("All candidate firmware banks match Apple stock firmware 7.8.1.")
        }
        if includeFirmwarePayload {
            payload["firmware_payload"] = .object([
                "template_source": .string("catalog"),
                "template_path": .string("/tmp/firmware.basebinary"),
                "template_product_id": .string("116"),
                "template_version": .string("7.8.1"),
                "template_sha256": .string("template-sha"),
                "payload_sha256": .string("payload-sha"),
                "payload_size": .number(456),
                "expected_prefix_sha256": .string("prefix-sha"),
                "expected_prefix_size": .number(123),
                "key_id": .string("key-one"),
                "inner_model": .number(116),
                "inner_version": .string("0x00070801"),
                "inner_payload_size": .number(123)
            ])
            payload["firmware_payload_path"] = .string("/tmp/flash-backup/primary.download_only.basebinary")
        }
        return .object(payload)
    }

    /// The summary `flash_plan_payload` gives each plan.
    private func summary(for mode: FlashPlanMode, writeRequested: Bool, alreadySatisfied: Bool) -> String {
        switch mode {
        case .checkApple:
            return alreadySatisfied
                ? "Active firmware bank matches Apple stock firmware 7.8.1."
                : "Active firmware bank does not match Apple stock firmware 7.8.1."
        case .downloadOnly:
            return "Apple restore firmware validated (version 7.8.1, product 116)."
        case .patch, .restore:
            if alreadySatisfied {
                return "Flash plan is already satisfied; no write is needed."
            }
            return writeRequested ? "Flash \(mode.rawValue) write plan generated." : "Flash \(mode.rawValue) plan generated."
        }
    }

    private func flashWritePayload(
        mode: FlashPlanMode,
        postWriteAction: String? = nil,
        rebootRequested: Bool? = nil,
        rebooted: Bool? = nil,
        waitedAfterReboot: Bool? = nil,
        summary: String? = nil
    ) -> JSONValue {
        let resolvedPostWriteAction = postWriteAction ?? (mode == .restore ? "ssh_reboot" : "manual_power_cycle")
        let resolvedRebootRequested = rebootRequested ?? (mode == .restore)
        let resolvedRebooted = rebooted ?? (mode == .restore)
        let resolvedWaitedAfterReboot = waitedAfterReboot ?? (mode == .restore)
        let resolvedSummary = summary ?? {
            if mode == .restore {
                return "Flash restore write validated; device rebooted."
            }
            return "Flash \(mode.rawValue) write validated; manual power cycle required."
        }()
        return .object([
            "schema_version": .number(1),
            "backup_dir": .string("/tmp/flash-backup"),
            "mode": .string(mode.rawValue),
            "write_status": .string("validated"),
            "write_validated": .bool(true),
            "post_write_action": .string(resolvedPostWriteAction),
            "reboot_requested": .bool(resolvedRebootRequested),
            "rebooted": .bool(resolvedRebooted),
            "waited_after_reboot": .bool(resolvedWaitedAfterReboot),
            "write_outcome": .object([
                "status": .string("validated"),
                "mode": .string(mode.rawValue),
                "write_validated": .bool(true),
                "write_may_have_modified_device": .bool(true),
                "post_write_action": .string(resolvedPostWriteAction),
                "reboot_requested": .bool(resolvedRebootRequested),
                "rebooted": .bool(resolvedRebooted),
                "waited_after_reboot": .bool(resolvedWaitedAfterReboot)
            ]),
            "summary": .string(resolvedSummary)
        ])
    }

}
