import XCTest
@testable import TimeCapsuleSMBApp

final class PendingConfirmationTests: XCTestCase {
    func testLocalizedStringsLoadFromResourceBundle() {
        XCTAssertEqual(L10n.string("screen.readiness"), "Readiness")
        XCTAssertEqual(L10n.string("button.uninstall_plan"), "Uninstall Plan")
        XCTAssertEqual(L10n.string("helper.error.cancelled"), "Operation cancelled.")
        XCTAssertEqual(L10n.format("event.summary.result", "deploy", "finished"), "deploy: finished")
    }

    func testUninstallPlanParamsCarryNoRebootSelection() {
        let params = OperationParams.uninstallPlan(noReboot: true, noWait: true, mountWait: 9)

        XCTAssertEqual(params["dry_run"], .bool(true))
        XCTAssertEqual(params["no_reboot"], .bool(true))
        XCTAssertEqual(params["no_wait"], .bool(true))
        XCTAssertEqual(params["mount_wait"], .number(9))
    }

    func testDeployConfirmationCarriesDeployAndRebootConsent() {
        let confirmation = PendingConfirmation.deploy(noReboot: false, nbnsEnabled: true, debugLogging: true, mountWait: 45, noWait: true)

        XCTAssertEqual(confirmation.operation, "deploy")
        XCTAssertEqual(confirmation.params["dry_run"], .bool(false))
        XCTAssertEqual(confirmation.params["confirm_deploy"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_reboot"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_netbsd4_activation"], .bool(true))
        XCTAssertEqual(confirmation.params["no_reboot"], .bool(false))
        XCTAssertEqual(confirmation.params["nbns_enabled"], .bool(true))
        XCTAssertEqual(confirmation.params["debug_logging"], .bool(true))
        XCTAssertEqual(confirmation.params["mount_wait"], .number(45))
        XCTAssertEqual(confirmation.params["no_wait"], .bool(true))
    }

    func testUninstallConfirmationCarriesUninstallAndNoRebootConsent() {
        let confirmation = PendingConfirmation.uninstall(noReboot: true, mountWait: 12, noWait: true)

        XCTAssertEqual(confirmation.operation, "uninstall")
        XCTAssertEqual(confirmation.params["dry_run"], .bool(false))
        XCTAssertEqual(confirmation.params["confirm_uninstall"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_reboot"], .bool(false))
        XCTAssertEqual(confirmation.params["no_reboot"], .bool(true))
        XCTAssertEqual(confirmation.params["mount_wait"], .number(12))
        XCTAssertEqual(confirmation.params["no_wait"], .bool(true))
    }

    func testMaintenanceConfirmationsCarryExplicitOperationConsent() {
        let fsck = PendingConfirmation.fsck(volume: "Data", noReboot: true, mountWait: 18, noWait: true)
        let repair = PendingConfirmation.repairXattrs(path: "/Volumes/Data")

        XCTAssertEqual(fsck.operation, "fsck")
        XCTAssertEqual(fsck.params["confirm_fsck"], .bool(true))
        XCTAssertEqual(fsck.params["no_reboot"], .bool(true))
        XCTAssertEqual(fsck.params["mount_wait"], .number(18))
        XCTAssertEqual(fsck.params["no_wait"], .bool(true))
        XCTAssertEqual(fsck.params["volume"], .string("Data"))

        XCTAssertEqual(repair.operation, "repair-xattrs")
        XCTAssertEqual(repair.params["path"], .string("/Volumes/Data"))
        XCTAssertEqual(repair.params["dry_run"], .bool(false))
        XCTAssertEqual(repair.params["confirm_repair"], .bool(true))
    }
}
