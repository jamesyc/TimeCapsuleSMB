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

    func testBackendErrorViewModelLocalizesAuthFailureMessagesAcrossFlows() {
        let doctorError = BackendErrorViewModel(operation: "doctor", code: "auth_failed", message: "Password rejected.")
        let configureError = BackendErrorViewModel(
            operation: "configure",
            code: "auth_failed",
            message: "The AirPort admin password did not work."
        )

        L10n.apply(language: .english)
        XCTAssertEqual(doctorError.message, "The device rejected the supplied password or SSH credentials.")
        XCTAssertEqual(configureError.message, "The AirPort admin password did not work.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(doctorError.message, "设备拒绝了提供的密码或 SSH 凭据。")
        XCTAssertEqual(configureError.message, "AirPort 管理员密码无效。")
    }

    func testRecoveryGuidancePresentationLocalizesConfigureAuthFailure() throws {
        let recovery = try recoveryValue(
            title: "AirPort password rejected",
            actions: [
                "Re-enter the AirPort admin password.",
                "Confirm the selected device is the intended Apple device."
            ],
            suggestedOperation: "configure",
            actionIDs: ["replace_password"],
            message: "ACP or SSH authentication failed while configuring the device.",
            localizationKey: "configure.auth_failed"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "configure",
            code: "auth_failed",
            message: "The AirPort admin password did not work.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        let english = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(english.title, "AirPort password rejected")
        XCTAssertEqual(english.errorMessage, "The AirPort admin password did not work.")
        XCTAssertEqual(english.detail, "ACP or SSH authentication failed while configuring the device.")
        XCTAssertEqual(english.steps.first, "Re-enter the AirPort admin password.")

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "AirPort 密码被拒绝")
        XCTAssertEqual(chinese.errorMessage, "AirPort 管理员密码无效。")
        XCTAssertEqual(chinese.detail, "配置设备时 ACP 或 SSH 身份验证失败。")
        XCTAssertEqual(chinese.steps, ["重新输入 AirPort 管理员密码。", "确认所选设备是目标 Apple 设备。"])
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

    func testRecoveryGuidancePresentationLocalizesConfigureAcpPortProbeDetails() throws {
        let recovery = try recoveryValue(
            title: "AirPort not reachable at this address",
            actions: [
                "Disable VPN or security software that routes local network traffic, then try again.",
                "Check that the IP address is the Time Capsule or AirPort address.",
                "Confirm you are on the same network as the device.",
                "Use discovery or enter the current LAN IP address."
            ],
            suggestedOperation: "configure",
            message: "TimeCapsuleSMB could not reach the AirPort ACP service before enabling SSH. Backups or AirPort Utility may still work even when ACP is blocked.",
            localizationKey: "configure.remote_error.acp_port_probe"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "configure",
            code: "remote_error",
            message: "No AirPort ACP service responded at this address.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        let english = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(english.title, "AirPort not reachable at this address")
        XCTAssertEqual(
            english.detail,
            "TimeCapsuleSMB could not reach the AirPort ACP service before enabling SSH. Backups or AirPort Utility may still work even when ACP is blocked."
        )
        XCTAssertEqual(english.steps[0], "Disable VPN or security software that routes local network traffic, then try again.")
        XCTAssertEqual(english.steps.count, 4)

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "无法通过此地址访问 AirPort")
        XCTAssertEqual(chinese.steps[0], "停用会路由本地网络流量的 VPN 或安全软件，然后重试。")
        XCTAssertEqual(chinese.steps.count, 4)
    }

    func testRecoveryGuidancePresentationLocalizesRebootTimeoutDetails() throws {
        let recovery = try recoveryValue(
            title: "Reboot did not finish",
            actions: [
                "Wait a few more minutes.",
                "If the device is reachable at a new IP, update TC_HOST or rerun configure.",
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                "On NetBSD 4 devices, run tcapsule activate once SSH is reachable; deploy did not get far enough to activate Samba after reboot.",
                "If your device resets itself, see https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
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
        XCTAssertEqual(english.steps.count, 5)
        XCTAssertEqual(english.steps[1], "If the device is reachable at a new IP, update TC_HOST or rerun configure.")
        XCTAssertEqual(
            english.steps[4],
            "If your device resets itself, see https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
        )

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "重启未完成")
        XCTAssertEqual(chinese.steps[0], "再等待几分钟。")
        XCTAssertEqual(chinese.steps.count, 5)
        XCTAssertEqual(
            chinese.steps[4],
            "如果设备自行重置，请参见 https://github.com/jamesyc/TimeCapsuleSMB/issues/177。"
        )
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
