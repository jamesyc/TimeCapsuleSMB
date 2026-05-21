import XCTest
@testable import TimeCapsuleSMBApp

final class RecoveryActionMapperTests: XCTestCase {
    func testAuthFailureStartsWithReplacePassword() {
        let error = BackendErrorViewModel(operation: "doctor", code: "auth_failed", message: "Password rejected.")

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertEqual(actions.first, RecoveryAction(title: "Replace Password", kind: .replacePassword))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics)))
    }

    func testSuggestedOperationMapsToUserFacingAction() throws {
        let recovery = try recoveryValue(
            title: "Disk issue",
            actions: ["Wake the disk by opening it in Finder.", "Retry deploy."],
            suggestedOperation: "fsck"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Disk did not mount.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertTrue(actions.contains(RecoveryAction(title: "Run Disk Repair", kind: .diskRepair)))
        XCTAssertTrue(actions.contains(where: { $0.kind == .openFinder }))
        XCTAssertTrue(actions.contains(where: { $0.kind == .installSMB }))
    }
}
