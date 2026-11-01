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
        let profile = try testNetBSD4Profile()
        // The store's own default policy, so not the fixture, which names one.
        let store = FlashWorkflowStore()

        store.refresh(profile: profile)

        XCTAssertEqual(store.state, .writeLocked)
        XCTAssertTrue(store.canBackup)
    }

    func testReadOnlyPolicyAllowsAnalysisButNotWrites() throws {
        let profile = try testNetBSD4Profile()

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .readOnly)

        XCTAssertEqual(eligibility.state, .eligibleForReadOnlyAnalysis)
        XCTAssertTrue(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testNonNetBSD4DeviceIsUnavailable() throws {
        let profile = try testDeviceProfile(payloadFamily: "netbsd6_samba4")

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .writesEnabled)

        XCTAssertEqual(eligibility.state, .unavailable)
        XCTAssertFalse(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testBootHookSectionVisibilityIsLimitedToNetBSD4Profiles() throws {
        let netbsd4 = try testNetBSD4Profile()
        let netbsd6 = try testDeviceProfile(payloadFamily: "netbsd6_samba4")

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

        store.backupAndInspect(password: "pw", profile: fixture.profile)
        XCTAssertEqual(store.state, .readingBanks)
        try await waitUntilStoreState { store.backup != nil && store.state == .planAvailable && !store.isBusy }

        store.planFlash(mode: .patch, profile: fixture.profile)
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
        let fixture = try BootHookTestFixture(responses: testBootHookResponses())
        let store = fixture.store
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

        store.backupAndInspect(password: "pw", profile: fixture.profile)

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
        let store = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: true)).store

        XCTAssertEqual(store.state, .appleCheckComplete)
        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Active firmware bank matches Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match", value: "yes")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Version", value: "7.8.1")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Payload SHA-256", value: "inner-sha")))
    }

    func testAppleCheckPresentationShowsMultiBankMatchDetails() async throws {
        let store = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: true, allBanks: true)).store

        XCTAssertEqual(store.state, .appleCheckComplete)
        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "All candidate firmware banks match Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match (primary)", value: "yes")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match (secondary)", value: "yes")))
    }

    func testAppleCheckMismatchUsesDedicatedState() async throws {
        let store = try await BootHookTestFixture.afterPlan(testAppleCheckPayload(matched: false)).store

        XCTAssertEqual(store.state, .appleFirmwareMismatch)
        XCTAssertTrue(FlashPresentation(store: store).rows.contains(PresentationRow(label: "Apple Match", value: "no")))
    }

    func testValidateAppleRestoreFirmwarePresentationShowsPayloadDetails() async throws {
        let store = try await BootHookTestFixture.afterPlan(testAppleRestoreFirmwarePayload()).store

        XCTAssertEqual(store.state, .appleFirmwareReady)
        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Apple restore firmware validated (version 7.8.1, product 119).")
        XCTAssertEqual(presentation.title(for: .downloadApple), "Validate Apple Restore Firmware")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload", value: "/tmp/flash-backup/primary.download_only.basebinary")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload SHA-256", value: "payload-sha")))
    }

    func testWriteConfirmationCancellationRestoresPlanAvailable() async throws {
        for mode in [FlashPlanMode.patch, .restore] {
            let fixture = try BootHookTestFixture(runner: PausingStoreTestRunner(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: mode)
            ) + [
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
            ]), onDeviceLane: true)
            let runner = fixture.runner
            defer { runner.finishAll() }
            let coordinator = fixture.coordinator
            let store = fixture.store
            let profile = fixture.profile

            try await fixture.run(.backup, .plan(mode))

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
        let store = try await BootHookTestFixture.afterWrite(.patch).store

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
        let store = try await BootHookTestFixture.afterWrite(.restore).store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertNil(store.manualPowerCycleNotice)
        XCTAssertFalse(FlashPresentation(store: store).warnings.contains(
            "Unplug the device, wait 10 seconds, then plug it back in."
        ))
    }

    func testValidatedRestoreWriteWithoutRebootShowsManualPowerCycleNotice() async throws {
        let store = try await BootHookTestFixture.afterWrite(.restore, .restoreNeedsManualReboot).store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertEqual(store.manualPowerCycleNotice?.mode, .restore)
    }

    func testValidatedSecondaryBankRestoreShowsNoPowerCycleNoticeAndAsksForAFreshBackup() async throws {
        // The device keeps running its primary bank, so nothing needs restarting,
        // but the backup no longer describes the rewritten secondary bank.
        let store = try await BootHookTestFixture.afterWrite(.restore, .restoredSecondaryBank).store

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertNil(store.manualPowerCycleNotice)
        XCTAssertTrue(store.backupSnapshotStale)
        XCTAssertFalse(store.canPlan)
        XCTAssertTrue(store.canBackup)
    }

    func testValidatedWriteMarksSnapshotStaleAndDisablesPlanning() async throws {
        let store = try await BootHookTestFixture.afterWrite(.patch).store
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
        XCTAssertEqual(FlashPresentation(store: store).title(for: .backupAndInspect), "Back Up and Inspect")
        XCTAssertEqual(row("Boot Hook", in: FlashPresentation(store: store)), "Installed")
    }

    func testFlashPresentationUsesWriteResultSummaryAfterWrite() async throws {
        let store = try await BootHookTestFixture.afterWrite(.patch).store

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
            let store = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(activeBank: nil, banks: [
                testBootHookBank(name: "primary", backupValid: testCase.primary, login: "stock"),
                testBootHookBank(name: "secondary", backupValid: testCase.secondary, login: "stock")
            ])).store

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
            let store = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(activeBank: testCase.activeBank, banks: [
                testBootHookBank(name: "primary", backupValid: true, activeCandidate: testCase.primaryCandidate, login: "stock"),
                testBootHookBank(name: "secondary", backupValid: true, activeCandidate: testCase.secondaryCandidate, login: "stock")
            ])).store

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
            let store = try await BootHookTestFixture.afterBackup(testBootHookBackupPayload(banks: [
                testBootHookBank(name: "primary", backupValid: testCase.primary != nil, login: testCase.primary),
                testBootHookBank(name: "secondary", backupValid: true, login: testCase.secondary)
            ])).store

            XCTAssertEqual(row("Boot Hook", in: FlashPresentation(store: store)), testCase.expected, "\(testCase)")
        }
    }

    func testBankRowsLeaveWithTheStaleBackupAfterAWrite() async throws {
        let store = try await BootHookTestFixture.afterWrite(.patch).store

        let presentation = FlashPresentation(store: store)

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

        store.write(mode: .patch, password: "pw", profile: fixture.profile)
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
            let fixture = try BootHookTestFixture(responses: testBootHookResponses(
                plan: testBootHookPlanPayload(mode: .patch),
                failure: [
                    BackendEvent(type: "stage", operation: "flash", stage: testCase.stage, risk: "remote_write", cancellable: false),
                    testBootHookError("write failed")
                ]
            ))
            let store = fixture.store
            try await fixture.run(.backup, .plan(.patch))
            store.write(mode: .patch, password: "pw", profile: fixture.profile)
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
        let cases: [(plan: JSONValue, expected: String)] = [
            (testBootHookPlanPayload(mode: .restore), "Flash restore write plan generated."),
            (testBootHookSatisfiedPatchPlanPayload(), "Flash plan is already satisfied; no write is needed.")
        ]
        for testCase in cases {
            let store = try await BootHookTestFixture.afterPlan(testCase.plan).store

            let presentation = FlashPresentation(store: store)

            XCTAssertFalse(presentation.isEnabled(.writePatch), testCase.expected)
            XCTAssertEqual(presentation.message, testCase.expected)
        }
    }

    func testFlashFailureShowsInAnErrorBoxNotInTheStatusLine() async throws {
        let failure = "refusing to patch primary because the secondary (backup) firmware bank is not a valid backup"
        let fixture = try BootHookTestFixture(responses: testBootHookResponses(failure: [testBootHookError(failure)]))
        let store = fixture.store
        try await fixture.run(.backup)
        store.planFlash(mode: .patch, profile: fixture.profile)
        try await waitUntilStoreState { store.error != nil && !store.isBusy }

        let error = try XCTUnwrap(store.error)
        XCTAssertNotEqual(FlashPresentation(store: store).message, error.message)
        XCTAssertEqual(error.diagnosticMessage, failure)
        // The flash section's own buttons rerun each step, so the dashboard
        // gives its box no retry (DashboardStoreTests).
        XCTAssertEqual(error.operation, "flash")
    }

    private func row(_ label: String, in presentation: FlashPresentation) -> String? {
        presentation.rows.first { $0.label == label }?.value
    }
}
