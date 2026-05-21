import XCTest
@testable import TimeCapsuleSMBApp

final class PendingConfirmationTests: XCTestCase {
    func testLocalizedStringsLoadFromResourceBundle() {
        XCTAssertEqual(L10n.string("screen.readiness"), "Readiness")
        XCTAssertEqual(L10n.string("button.uninstall_plan"), "Uninstall Plan")
        XCTAssertEqual(L10n.string("button.capabilities"), "Capabilities")
        XCTAssertEqual(L10n.string("helper.error.cancelled"), "Operation cancelled.")
        XCTAssertEqual(L10n.format("event.summary.result", "deploy", "finished"), "deploy: finished")
    }

    func testUninstallPlanParamsCarryNoRebootSelection() {
        let params = OperationParams.uninstallPlan(noReboot: true, noWait: true, mountWait: 9, password: "pw")

        XCTAssertEqual(params["dry_run"], .bool(true))
        XCTAssertEqual(params["no_reboot"], .bool(true))
        XCTAssertEqual(params["no_wait"], .bool(true))
        XCTAssertEqual(params["mount_wait"], .number(9))
        XCTAssertEqual(params["credentials"], .object(["password": .string("pw")]))
    }

    func testDeployRunParamsCarryOptionsWithoutFrontendConsentFlags() {
        let params = OperationParams.deployRun(
            noReboot: false,
            noWait: true,
            nbnsEnabled: true,
            debugLogging: true,
            mountWait: 45,
            password: ""
        )

        XCTAssertEqual(params["dry_run"], .bool(false))
        XCTAssertNil(params["confirm_deploy"])
        XCTAssertNil(params["confirm_reboot"])
        XCTAssertNil(params["confirm_netbsd4_activation"])
        XCTAssertEqual(params["no_reboot"], .bool(false))
        XCTAssertEqual(params["nbns_enabled"], .bool(true))
        XCTAssertEqual(params["debug_logging"], .bool(true))
        XCTAssertEqual(params["mount_wait"], .number(45))
        XCTAssertEqual(params["no_wait"], .bool(true))
        XCTAssertNil(params["credentials"])
    }

    func testConfigureParamsUseSelectedRecordInsteadOfManualHostWhenProvided() {
        let selectedRecord = JSONValue.object([
            "name": .string("TC"),
            "hostname": .string("tc.local."),
            "ipv4": .array([.string("10.0.0.2")]),
            "properties": .object(["syAP": .string("119")])
        ])

        let params = OperationParams.configure(
            host: "root@manual",
            selectedRecord: selectedRecord,
            password: "pw",
            debugLogging: true
        )

        XCTAssertNil(params["host"])
        XCTAssertEqual(params["selected_record"], selectedRecord)
        XCTAssertEqual(params["password"], .string("pw"))
        XCTAssertEqual(params["debug_logging"], .bool(true))
    }

    func testConfigureParamsDefaultBareManualHostToRootUser() {
        let params = OperationParams.configure(
            host: " 10.0.0.2 ",
            password: "pw",
            debugLogging: false
        )

        XCTAssertEqual(params["host"], .string("root@10.0.0.2"))
        XCTAssertEqual(params["password"], .string("pw"))
        XCTAssertEqual(params["persist_password"], .bool(false))
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
        let originalParams = OperationParams.uninstallRun(noReboot: true, noWait: true, mountWait: 12, password: "pw")

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

    func testMaintenanceRunParamsDoNotCarryFrontendConsentFlags() {
        let fsck = OperationParams.fsckRun(volume: "Data", noReboot: true, noWait: true, mountWait: 18, password: "")
        let repair = OperationParams.repairXattrsRun(path: "/Volumes/Data")

        XCTAssertNil(fsck["confirm_fsck"])
        XCTAssertEqual(fsck["no_reboot"], .bool(true))
        XCTAssertEqual(fsck["mount_wait"], .number(18))
        XCTAssertEqual(fsck["no_wait"], .bool(true))
        XCTAssertEqual(fsck["volume"], .string("Data"))

        XCTAssertEqual(repair["path"], .string("/Volumes/Data"))
        XCTAssertEqual(repair["dry_run"], .bool(false))
        XCTAssertNil(repair["confirm_repair"])
    }
}
