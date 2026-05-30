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
            .manualPowerCycleRequired,
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
        let restore = OperationParams.flashWrite(
            backupDir: "/tmp/flash-backup",
            mode: .restore
        )
        let patch = OperationParams.flashWrite(
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
        try await waitUntilStoreState { store.backup != nil && store.state == .planAvailable }

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
        try await waitUntilStoreState { store.backup != nil }
        store.firmwareVersion = " 7.8.1 "
        store.firmwareTemplatePath = " /tmp/firmware.basebinary "

        store.planFlash(mode: .downloadOnly, profile: profile)
        try await waitUntilStoreState { runner.calls.count == 2 && store.plan != nil }

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
        try await waitUntilStoreState { store.backup != nil }
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
        try await waitUntilStoreState { store.backup != nil }
        store.planFlash(mode: .checkApple, profile: profile)
        try await waitUntilStoreState { store.state == .appleCheckComplete }

        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Active firmware bank matches Apple stock firmware 7.8.1.")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Match", value: "yes")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Version", value: "7.8.1")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Apple Payload SHA-256", value: "inner-sha")))
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
        try await waitUntilStoreState { store.backup != nil }
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
        try await waitUntilStoreState { store.backup != nil }
        store.planFlash(mode: .downloadOnly, profile: profile)
        try await waitUntilStoreState { store.state == .appleFirmwareReady }

        let presentation = FlashPresentation(store: store)
        XCTAssertEqual(presentation.message, "Apple restore firmware validated (version 7.8.1, product 116).")
        XCTAssertEqual(presentation.title(for: .downloadApple), "Validate Apple Restore Firmware")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload", value: "/tmp/flash-backup/primary.download_only.basebinary")))
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Firmware Payload SHA-256", value: "payload-sha")))
    }

    func testWriteConfirmationCancellationRestoresPlanAvailable() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashPlanPayload(mode: .patch, writeRequested: true))
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
            ])
        ])
        let backend = BackendClient(runner: runner)
        let store = FlashWorkflowStore(backend: backend)
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil }
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.plan != nil }

        store.write(mode: .patch, password: "pw", profile: profile)
        try await waitUntilStoreState { store.state == .awaitingStrongConfirmation && backend.pendingConfirmation != nil && !backend.isRunning }

        backend.cancelPendingConfirmation()

        try await waitUntilStoreState { store.state == .planAvailable && backend.pendingConfirmation == nil }
    }

    func testValidatedPatchWriteShowsManualPowerCycleNotice() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)

        XCTAssertEqual(store.state, .writeValidatedSnapshotStale)
        XCTAssertEqual(store.manualPowerCycleNotice?.mode, .patch)
        XCTAssertEqual(
            store.manualPowerCycleNotice?.message,
            "Flash write validation completed. Unplug the device, wait 10 seconds, then plug it back in. Wait for it to finish booting, then run Checkup. One firmware bank was left untouched."
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
                BackendEvent(type: "result", operation: "flash", ok: true, payload: flashBackupPayload())
            ])
        ])
        let store = FlashWorkflowStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        store.refresh(profile: profile)

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { store.backup != nil }
        store.planFlash(mode: .patch, profile: profile)
        try await waitUntilStoreState { store.plan != nil }
        store.write(mode: .patch, password: "pw", profile: profile)
        try await waitUntilStoreState { store.backupSnapshotStale }

        store.backupAndInspect(password: "pw", profile: profile)
        try await waitUntilStoreState { !store.backupSnapshotStale && store.state == .planAvailable }

        XCTAssertTrue(store.canPlan)
        XCTAssertEqual(FlashPresentation(store: store).title(for: .backupAndInspect), "Back Up and Inspect")
    }

    func testFlashPresentationUsesWriteResultSummaryAfterWrite() async throws {
        let store = try await storeAfterValidatedWrite(mode: .patch)

        let presentation = FlashPresentation(store: store)

        XCTAssertEqual(presentation.message, "Flash patch write validated; manual power cycle required.")
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
        try await waitUntilStoreState { store.backup != nil }
        store.planFlash(mode: mode, profile: profile)
        try await waitUntilStoreState { store.plan != nil }
        store.write(mode: mode, password: "pw", profile: profile)
        try await waitUntilStoreState { store.writeResult != nil }
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

    private func flashBackupPayload() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "backup_dir": .string("/tmp/flash-backup"),
            "host": .string("10.0.0.2"),
            "syap": .string("116"),
            "active_bank": .string("primary"),
            "banks": .array([
                .object([
                    "name": .string("primary"),
                    "device": .string("/dev/rflash0.raw"),
                    "size": .number(128),
                    "sha256": .string("abc"),
                    "backup_valid": .bool(true),
                    "active_candidate": .bool(true),
                    "would_write": .bool(false),
                    "write_decision": .string("no write")
                ])
            ]),
            "counts": .object(["banks": .number(1)]),
            "summary": .string("Flash backup saved to /tmp/flash-backup.")
        ])
    }

    private func flashPlanPayload(
        mode: FlashPlanMode,
        writeRequested: Bool,
        alreadySatisfied: Bool = false,
        appleMatched: Bool? = nil,
        includeFirmwarePayload: Bool = false
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
            "summary": .string(summary(for: mode, alreadySatisfied: alreadySatisfied))
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

    private func summary(for mode: FlashPlanMode, alreadySatisfied: Bool) -> String {
        switch mode {
        case .checkApple:
            return alreadySatisfied
                ? "Active firmware bank matches Apple stock firmware 7.8.1."
                : "Active firmware bank does not match Apple stock firmware 7.8.1."
        case .downloadOnly:
            return "Apple restore firmware validated (version 7.8.1, product 116)."
        case .patch, .restore:
            return "Flash \(mode.rawValue) plan generated."
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
