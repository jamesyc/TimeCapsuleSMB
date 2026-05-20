import XCTest
@testable import TimeCapsuleSMBApp

final class PendingConfirmationTests: XCTestCase {
    func testDeployConfirmationCarriesDeployAndRebootConsent() {
        let confirmation = PendingConfirmation.deploy(noReboot: false, nbnsEnabled: true, debugLogging: true)

        XCTAssertEqual(confirmation.operation, "deploy")
        XCTAssertEqual(confirmation.params["dry_run"], .bool(false))
        XCTAssertEqual(confirmation.params["confirm_deploy"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_reboot"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_netbsd4_activation"], .bool(true))
        XCTAssertEqual(confirmation.params["no_reboot"], .bool(false))
        XCTAssertEqual(confirmation.params["nbns_enabled"], .bool(true))
        XCTAssertEqual(confirmation.params["debug_logging"], .bool(true))
    }

    func testUninstallConfirmationCarriesUninstallAndNoRebootConsent() {
        let confirmation = PendingConfirmation.uninstall(noReboot: true)

        XCTAssertEqual(confirmation.operation, "uninstall")
        XCTAssertEqual(confirmation.params["dry_run"], .bool(false))
        XCTAssertEqual(confirmation.params["confirm_uninstall"], .bool(true))
        XCTAssertEqual(confirmation.params["confirm_reboot"], .bool(false))
        XCTAssertEqual(confirmation.params["no_reboot"], .bool(true))
    }

    func testMaintenanceConfirmationsCarryExplicitOperationConsent() {
        let fsck = PendingConfirmation.fsck(volume: "Data", noReboot: true)
        let repair = PendingConfirmation.repairXattrs(path: "/Volumes/Data")

        XCTAssertEqual(fsck.operation, "fsck")
        XCTAssertEqual(fsck.params["confirm_fsck"], .bool(true))
        XCTAssertEqual(fsck.params["no_reboot"], .bool(true))
        XCTAssertEqual(fsck.params["volume"], .string("Data"))

        XCTAssertEqual(repair.operation, "repair-xattrs")
        XCTAssertEqual(repair.params["path"], .string("/Volumes/Data"))
        XCTAssertEqual(repair.params["dry_run"], .bool(false))
        XCTAssertEqual(repair.params["confirm_repair"], .bool(true))
    }
}
