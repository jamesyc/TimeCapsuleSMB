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
            suggestedOperation: "fsck",
            actionIDs: ["open_finder", "install_smb"]
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "doctor",
            code: "remote_error",
            message: "Disk did not mount.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertTrue(actions.contains(RecoveryAction(title: "Run Disk Repair", kind: .diskRepair)))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Open Finder", kind: .openFinder)))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Install SMB", kind: .installSMB)))
    }

    func testDeployRecoveryDoesNotShowFinderOrInstallSMBActions() throws {
        let recovery = try recoveryValue(
            title: "No HFS volumes found",
            actions: ["Retry deploy."],
            suggestedOperation: "deploy",
            actionIDs: ["open_finder", "install_smb"]
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertFalse(actions.contains { $0.kind == .openFinder })
        XCTAssertFalse(actions.contains { $0.kind == .installSMB })
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Retry", kind: .retry)))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics)))
    }

    func testHumanRecoveryTextDoesNotCreateActionButtons() throws {
        let recovery = try recoveryValue(
            title: "Disk issue",
            actions: ["Wake the disk by opening it in Finder.", "Retry deploy."],
            suggestedOperation: "unknown"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Disk did not mount.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertFalse(actions.contains(where: { $0.kind == .openFinder }))
        XCTAssertFalse(actions.contains(where: { $0.kind == .installSMB }))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Retry", kind: .retry)))
    }
}
