import XCTest
@testable import TimeCapsuleSMBApp

final class RecoveryActionMapperTests: XCTestCase {
    override func tearDown() {
        L10n.apply(language: .system)
        super.tearDown()
    }

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

    func testLocalNetworkRecoveryShowsSystemSettingsAction() throws {
        let recovery = try recoveryValue(
            title: "Local Network access blocked",
            actions: ["Open System Settings > Privacy & Security > Local Network."],
            suggestedOperation: "configure",
            actionIDs: ["open_system_settings"]
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "configure",
            code: "local_network_permission_denied",
            message: "macOS is blocking TimeCapsuleSMB.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertTrue(actions.contains(RecoveryAction(title: "Open System Settings", kind: .openSystemSettings)))
        XCTAssertTrue(actions.contains(RecoveryAction(title: "Retry", kind: .retry)))
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

    func testRecoveryGuidancePresentationLocalizesRebootTimeoutDetails() throws {
        let recovery = try recoveryValue(
            title: "Reboot did not finish",
            actions: [
                "Wait a few more minutes.",
                "If the device is reachable at a new IP, update TC_HOST or rerun configure.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                "On NetBSD 4 devices, run tcapsule activate once SSH is reachable; deploy did not get far enough to activate Samba after reboot."
            ],
            actionIDs: ["run_checkup"],
            message: "The device went down but SSH did not return before the timeout.",
            localizationKey: "deploy.remote_error.wait_for_reboot_up"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Timed out waiting for SSH after reboot.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        let english = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(english.title, "Reboot did not finish")
        XCTAssertEqual(
            english.detail,
            "The payload was uploaded and the reboot request succeeded, but the device did not accept SSH again before the 4 minute timeout. It may still be booting, or it may have come back with a different IP address."
        )
        XCTAssertEqual(english.steps.count, 4)
        XCTAssertEqual(english.steps[1], "If the device is reachable at a new IP, update TC_HOST or rerun configure.")

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "重启未完成")
        XCTAssertEqual(chinese.steps[0], "再等待几分钟。")
        XCTAssertEqual(chinese.steps.count, 4)
    }

    func testRecoveryGuidancePresentationLocalizesSlowDeviceSshTimeoutDetails() throws {
        let recovery = try recoveryValue(
            title: "Device is responding very slowly",
            actions: [
                "Reboot the device.",
                "Wait for SSH to come back.",
                "Retry the operation."
            ],
            actionIDs: ["run_checkup"],
            message: "The AirPort Extreme 6th generation is responding very slowly. Please reboot the device. Then wait for SSH to come back and retry.",
            localizationKey: "remote_error.ssh_timeout_slow_device",
            localizationValues: ["device_name": "AirPort Extreme 6th generation"]
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Timed out waiting for ssh command to finish.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        let english = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(english.title, "Device is responding very slowly")
        XCTAssertEqual(
            english.detail,
            "The AirPort Extreme 6th generation is responding very slowly. Please reboot the device. Then wait for SSH to come back and retry."
        )
        XCTAssertEqual(english.steps, [
            "Reboot the device.",
            "Wait for SSH to come back.",
            "Retry the operation."
        ])

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "设备响应非常慢")
        XCTAssertEqual(chinese.detail, "AirPort Extreme 6th generation 响应非常慢。请重启设备。然后等待 SSH 恢复，再重试。")
        XCTAssertEqual(chinese.steps, [
            "重启设备。",
            "等待 SSH 恢复。",
            "重试此操作。"
        ])
    }

    func testRecoveryGuidancePresentationFallsBackWhenLocalizedPlaceholderValueIsMissing() throws {
        let fallbackMessage = "The device is responding very slowly. Please reboot the device. Then wait for SSH to come back and retry."
        let recovery = try recoveryValue(
            title: "Device is responding very slowly",
            actions: [
                "Reboot the device.",
                "Wait for SSH to come back.",
                "Retry the operation."
            ],
            message: fallbackMessage,
            localizationKey: "remote_error.ssh_timeout_slow_device"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Timed out waiting for ssh command to finish.",
            recovery: recovery
        )

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)

        XCTAssertEqual(chinese.detail, fallbackMessage)
    }

    func testRecoveryGuidancePresentationSuppressesDuplicateMessage() throws {
        let recovery = try recoveryValue(
            title: "No HFS volumes found",
            actions: [],
            message: "No HFS volumes found"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "No HFS volumes found",
            recovery: recovery
        )

        let presentation = RecoveryGuidancePresentation(error: error)

        XCTAssertNil(presentation.detail)
        XCTAssertTrue(presentation.steps.isEmpty)
    }

    func testDeployGuidanceUsesStructuredRecoveryInsteadOfGenericTip() throws {
        let recovery = try recoveryValue(
            title: "Reboot did not finish",
            actions: ["Wait a few more minutes."],
            message: "The payload was uploaded.",
            localizationKey: "deploy.remote_error.wait_for_reboot_up"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Timed out waiting for SSH after reboot.",
            recovery: recovery
        )

        XCTAssertNil(DeployFailureGuidancePolicy.guidance(for: error))
    }
}
