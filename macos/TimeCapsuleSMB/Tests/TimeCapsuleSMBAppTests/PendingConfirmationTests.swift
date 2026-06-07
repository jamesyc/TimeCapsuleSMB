import XCTest
@testable import TimeCapsuleSMBApp

final class PendingConfirmationTests: XCTestCase {
    func testLocalizedStringsLoadFromResourceBundle() {
        XCTAssertEqual(L10n.string("screen.readiness"), "Readiness")
        XCTAssertEqual(L10n.string("toolbar.cancel"), "Cancel")
        XCTAssertEqual(L10n.string("toolbar.diagnostics"), "Diagnostics")
        XCTAssertEqual(L10n.string("helper.error.cancelled"), "Operation cancelled.")
        XCTAssertEqual(L10n.string("confirm.backend.message"), "Continue with this operation?")
        XCTAssertEqual(L10n.format("event.summary.result", "deploy", "Finished"), "deploy: Finished")
    }

    func testUninstallPlanParamsCarryNoRebootSelection() {
        let params = OperationParams.Uninstall.params(dryRun: true, noReboot: true, noWait: true, mountWait: 9)

        XCTAssertEqual(params["dry_run"], .bool(true))
        XCTAssertEqual(params["no_reboot"], .bool(true))
        XCTAssertEqual(params["no_wait"], .bool(true))
        XCTAssertEqual(params["mount_wait"], .number(9))
        XCTAssertNil(params["credentials"])
    }

    func testDeployRunParamsCarryOptionsWithoutFrontendConsentFlags() {
        let params = OperationParams.Deploy.params(
            dryRun: false,
            noReboot: false,
            noWait: true,
            nbnsEnabled: true,
            debugLogging: true,
            ataIdleSeconds: 0,
            ataStandby: 0,
            mountWait: 45
        )

        XCTAssertEqual(params["dry_run"], .bool(false))
        XCTAssertNil(params["confirm_deploy"])
        XCTAssertNil(params["confirm_reboot"])
        XCTAssertNil(params["confirm_netbsd4_activation"])
        XCTAssertEqual(params["no_reboot"], .bool(false))
        XCTAssertEqual(params["nbns_enabled"], .bool(true))
        XCTAssertEqual(params["debug_logging"], .bool(true))
        XCTAssertEqual(params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(params["ata_standby"], .number(0))
        XCTAssertEqual(params["mount_wait"], .number(45))
        XCTAssertEqual(params["no_wait"], .bool(true))
        XCTAssertEqual(params["internal_share_use_disk_root"], .bool(false))
        XCTAssertEqual(params["smb_browse_compatibility"], .bool(false))
        XCTAssertEqual(params["any_protocol"], .bool(false))
        XCTAssertNil(params["credentials"])
    }

    func testDeployPlanParamsCarryAdvancedRuntimeOverridesWhenEnabled() {
        let params = OperationParams.Deploy.params(
            dryRun: true,
            noReboot: false,
            noWait: false,
            nbnsEnabled: true,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            anyProtocol: true,
            debugLogging: false,
            ataIdleSeconds: 0,
            ataStandby: nil,
            mountWait: 30
        )

        XCTAssertEqual(params["dry_run"], .bool(true))
        XCTAssertEqual(params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(params["any_protocol"], .bool(true))
        XCTAssertEqual(params["debug_logging"], .bool(false))
        XCTAssertEqual(params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(params["ata_standby"], .string(""))
        XCTAssertNil(params["credentials"])
    }

    func testConfigureParamsUseSelectedRecordInsteadOfManualHostWhenProvided() {
        let selectedRecord = JSONValue.object([
            "name": .string("TC"),
            "hostname": .string("tc.local."),
            "ipv4": .array([.string("10.0.0.2")]),
            "properties": .object(["syAP": .string("119")])
        ])

        let params = OperationParams.Configure.save(
            host: "root@manual",
            selectedRecord: selectedRecord,
            password: "pw",
            debugLogging: true,
            internalShareUseDiskRoot: false,
            smbBrowseCompatibility: true,
            anyProtocol: true,
            ataIdleSeconds: 0,
            ataStandby: nil,
            includeAtaStandby: true
        )

        XCTAssertNil(params["host"])
        XCTAssertEqual(params["selected_record"], selectedRecord)
        XCTAssertEqual(params["password"], .string("pw"))
        XCTAssertEqual(params["debug_logging"], .bool(true))
        XCTAssertEqual(params["internal_share_use_disk_root"], .bool(false))
        XCTAssertEqual(params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(params["any_protocol"], .bool(true))
        XCTAssertEqual(params["ata_idle_seconds"], .number(0))
        XCTAssertEqual(params["ata_standby"], .string(""))
    }

    func testConfigureParamsDefaultBareManualHostToRootUser() {
        let params = OperationParams.Configure.save(
            host: " 10.0.0.2 ",
            password: "pw",
            debugLogging: false
        )

        XCTAssertEqual(params["host"], .string("root@10.0.0.2"))
        XCTAssertEqual(params["password"], .string("pw"))
        XCTAssertEqual(params["persist_password"], .bool(false))
        XCTAssertEqual(params["debug_logging"], .bool(false))
        XCTAssertNil(params["internal_share_use_disk_root"])
        XCTAssertNil(params["smb_browse_compatibility"])
        XCTAssertNil(params["any_protocol"])
    }

    func testConfigureParamsDefaultBareIPv6ManualHostToRootUser() {
        let params = OperationParams.Configure.save(
            host: " fd00::2 ",
            password: "pw",
            debugLogging: false
        )

        XCTAssertEqual(params["host"], .string("root@fd00::2"))
    }

    func testPendingConfirmationBuildsFromBackendEvent() throws {
        let event = BackendEvent(
            type: "error",
            operation: "uninstall",
            code: "confirmation_required",
            message: "Confirm uninstall.",
            details: .object([
                "title": .string("Confirm uninstall"),
                "message": .string("Remove files."),
                "action_title": .string("Uninstall"),
                "confirmation_id": .string("abc123")
            ])
        )
        let originalParams = OperationCredentialInjector.injectingPassword(
            "pw",
            into: OperationParams.Uninstall.params(dryRun: false, noReboot: true, noWait: true, mountWait: 12)
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: originalParams))

        XCTAssertEqual(confirmation.operation, "uninstall")
        XCTAssertEqual(confirmation.title, "Confirm uninstall")
        XCTAssertEqual(confirmation.message, "Remove files.")
        XCTAssertEqual(confirmation.actionTitle, "Uninstall")
        XCTAssertEqual(confirmation.params["confirmation_id"], .string("abc123"))
        XCTAssertEqual(confirmation.params["no_reboot"], .bool(true))
        XCTAssertEqual(confirmation.params["mount_wait"], .number(12))
        XCTAssertEqual(confirmation.params["no_wait"], .bool(true))
        XCTAssertEqual(confirmation.params["credentials"], .object(["password": .string("pw")]))
    }

    func testPendingConfirmationPrefersLocalizedPresentationForKnownBackendKey() throws {
        let event = BackendEvent(
            type: "error",
            operation: "deploy",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message."),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("deploy.reboot"),
                "presentation_values": .object(["device_name": .string("Time Capsule")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Deploy And Reboot?")
        XCTAssertEqual(confirmation.message, "Deploy TimeCapsuleSMB and reboot this Time Capsule?")
        XCTAssertEqual(confirmation.actionTitle, "Deploy and Reboot")
    }

    func testPendingConfirmationUsesLocalizedConfigureEnableSSHCopy() throws {
        let event = BackendEvent(
            type: "error",
            operation: "configure",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message."),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("configure.enable_ssh_reboot"),
                "presentation_values": .object(["device_name": .string("Office Capsule")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Enable SSH And Reboot?")
        XCTAssertEqual(
            confirmation.message,
            "SSH is closed on Office Capsule. Enable SSH using AirPort ACP and reboot this AirPort device?"
        )
        XCTAssertEqual(confirmation.actionTitle, "Enable SSH and Reboot")
    }

    func testPendingConfirmationUsesLocalizedActivationCopy() throws {
        let event = BackendEvent(
            type: "error",
            operation: "deploy",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message."),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("deploy.activate_now"),
                "presentation_values": .object(["device_name": .string("Time Capsule")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Deploy And Start SMB?")
        XCTAssertEqual(confirmation.message, "Deploy TimeCapsuleSMB to this Time Capsule and start SMB without rebooting it?")
        XCTAssertEqual(confirmation.actionTitle, "Deploy and Start SMB")
    }

    func testPendingConfirmationUsesRestoreWriteRebootCopy() throws {
        let event = BackendEvent(
            type: "error",
            operation: "flash",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message."),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("flash.restore_write"),
                "presentation_values": .object(["host": .string("10.0.0.2")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Restore Apple Firmware?")
        XCTAssertEqual(
            confirmation.message,
            "Restore Apple stock firmware to the active firmware bank on 10.0.0.2 and reboot after validation?"
        )
        XCTAssertEqual(confirmation.actionTitle, "Write Firmware")
    }

    func testPendingConfirmationUsesLocalizedNoWaitDeployCopy() throws {
        let event = BackendEvent(
            type: "error",
            operation: "deploy",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message."),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("deploy.netbsd4_no_wait"),
                "presentation_values": .object(["device_name": .string("Time Capsule")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Deploy And Request NetBSD4 Reboot?")
        XCTAssertEqual(
            confirmation.message,
            "Deploy TimeCapsuleSMB to this Time Capsule, request reboot, and return immediately without running Samba activation after SSH returns?"
        )
        XCTAssertEqual(confirmation.actionTitle, "Deploy and Request Reboot")
    }

    func testPendingConfirmationUsesLocalizedQuestionForUninstallWithoutReboot() throws {
        let event = BackendEvent(
            type: "error",
            operation: "uninstall",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "message": .string("Remove managed TimeCapsuleSMB files from the device."),
                "action_title": .string("Backend uninstall"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("uninstall.no_reboot"),
                "presentation_values": .object(["no_reboot": .bool(true)])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Uninstall?")
        XCTAssertEqual(confirmation.message, "Remove managed TimeCapsuleSMB files from the device?")
        XCTAssertEqual(confirmation.actionTitle, "Uninstall")
    }

    func testPendingConfirmationFormatsLocalizedPresentationValues() throws {
        let event = BackendEvent(
            type: "error",
            operation: "repair-xattrs",
            code: "confirmation_required",
            message: "Backend fallback.",
            details: .object([
                "message": .string("Repair xattrs."),
                "action_title": .string("Backend repair"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("repair_xattrs"),
                "presentation_values": .object(["path": .string("/Volumes/Data")])
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Repair Extended Attributes?")
        XCTAssertEqual(confirmation.message, "Repair known-safe macOS metadata issues under /Volumes/Data?")
        XCTAssertEqual(confirmation.actionTitle, "Repair xattrs")
    }

    func testPendingConfirmationFallsBackToBackendTextForUnknownPresentationKey() throws {
        let event = BackendEvent(
            type: "error",
            operation: "deploy",
            code: "confirmation_required",
            message: "Backend event fallback.",
            details: .object([
                "title": .string("Backend title"),
                "message": .string("Backend message?"),
                "action_title": .string("Backend action"),
                "confirmation_id": .string("abc123"),
                "presentation_id": .string("deploy.future")
            ])
        )

        let confirmation = try XCTUnwrap(PendingConfirmation(confirmationEvent: event, originalParams: [:]))

        XCTAssertEqual(confirmation.title, "Backend title")
        XCTAssertEqual(confirmation.message, "Backend message?")
        XCTAssertEqual(confirmation.actionTitle, "Backend action")
    }

    func testMaintenanceRunParamsDoNotCarryFrontendConsentFlags() {
        let fsck = OperationParams.Fsck.run(dryRun: false, volume: "Data", noReboot: true, noWait: true, mountWait: 18)
        let repair = OperationParams.RepairXattrs.params(
            dryRun: false,
            path: "/Volumes/Data",
            options: RepairXattrsOptions(
                recursive: false,
                maxDepth: 4,
                includeHidden: true,
                includeTimeMachine: true,
                fixPermissions: true,
                verbose: true
            )
        )

        XCTAssertNil(fsck["confirm_fsck"])
        XCTAssertEqual(fsck["dry_run"], .bool(false))
        XCTAssertEqual(fsck["no_reboot"], .bool(true))
        XCTAssertEqual(fsck["mount_wait"], .number(18))
        XCTAssertEqual(fsck["no_wait"], .bool(true))
        XCTAssertEqual(fsck["volume"], .string("Data"))

        XCTAssertEqual(repair["path"], .string("/Volumes/Data"))
        XCTAssertEqual(repair["dry_run"], .bool(false))
        XCTAssertEqual(repair["recursive"], .bool(false))
        XCTAssertEqual(repair["max_depth"], .number(4))
        XCTAssertEqual(repair["include_hidden"], .bool(true))
        XCTAssertEqual(repair["include_time_machine"], .bool(true))
        XCTAssertEqual(repair["fix_permissions"], .bool(true))
        XCTAssertEqual(repair["verbose"], .bool(true))
        XCTAssertNil(repair["confirm_repair"])
    }
}
