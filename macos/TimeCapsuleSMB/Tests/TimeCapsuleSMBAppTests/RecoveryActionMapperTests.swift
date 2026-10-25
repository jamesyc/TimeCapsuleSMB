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

    func testUnknownParamErrorOffersNoRetry() throws {
        // As the helper sends it: resending the same request fails the same way.
        let recovery = try JSONValue.object([
            "title": .string("Unknown parameter"),
            "message": .string("The helper does not accept a parameter this request sent, so the app and helper are out of step."),
            "actions": .array([
                .string("Update or reinstall TimeCapsuleSMB so the app and helper use the same API contract."),
                .string("If “Helper path” is set in “Settings”, clear it.")
            ]),
            "action_ids": .array([]),
            "retryable": .bool(false),
            "suggested_operation": .null
        ]).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "fsck",
            code: "unknown_param",
            message: "unknown parameter for fsck: no_wiat",
            recovery: recovery
        )

        XCTAssertEqual(RecoveryActionMapper.actions(for: error).map(\.kind), [.copyDiagnostics])
        let guidance = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(guidance.title, "Unknown parameter")
        XCTAssertEqual(guidance.steps.last, "If “Helper path” is set in “Settings”, clear it.")
    }

    func testAuthFailureLineIsItsGuidanceTitleInEveryFlow() throws {
        let doctorError = try helperError(operation: "doctor", code: "auth_failed", message: "Password rejected.")
        let configureError = try helperError(
            operation: "configure",
            code: "auth_failed",
            message: "The AirPort admin password did not work.",
            localizationKey: "configure.auth_failed"
        )
        let setSSHError = try helperError(
            operation: "set-ssh",
            code: "auth_failed",
            message: "The AirPort admin password did not work."
        )

        L10n.apply(language: .english)
        XCTAssertEqual(doctorError.message, "Authentication failed")
        XCTAssertEqual(configureError.message, "AirPort password rejected")
        XCTAssertEqual(setSSHError.message, doctorError.message)

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(doctorError.message, "身份验证失败")
        XCTAssertEqual(configureError.message, "AirPort 密码被拒绝")
        XCTAssertEqual(setSSHError.message, doctorError.message)
        // The helper's English stays the diagnostic text.
        XCTAssertEqual(configureError.diagnosticMessage, "The AirPort admin password did not work.")
    }

    func testSSHEnableTimeoutLineIsItsGuidanceTitleInEveryFlow() throws {
        let configureError = try helperError(
            operation: "configure",
            code: "ssh_enable_timeout",
            message: "SSH did not open after enabling via ACP."
        )
        let setSSHError = try helperError(
            operation: "set-ssh",
            code: "ssh_enable_timeout",
            message: "Failed to enable SSH via ACP: SSH did not open after enabling via ACP."
        )

        L10n.apply(language: .english)
        XCTAssertEqual(configureError.message, "SSH has not opened yet")
        XCTAssertEqual(setSSHError.message, configureError.message)

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(configureError.message, "SSH 尚未启动")
        XCTAssertEqual(setSSHError.message, configureError.message)
    }

    func testFailingDiskSymptomLinesAreTheirGuidanceTitles() throws {
        let noHFS = try helperError(
            operation: "deploy",
            code: "deploy_no_hfs_partition",
            message: "A disk was found, but no valid HFS partition was detected.",
            localizationKey: "deploy.deploy_no_hfs_partition"
        )
        let writeTest = try helperError(
            operation: "deploy",
            code: "disk_write_test_unresponsive",
            message: "Timed out waiting for ssh command to finish: write test"
        )
        let managerStop = try helperError(
            operation: "deploy",
            code: "manager_stop_timeout",
            message: "process manager did not stop",
            localizationKey: "deploy.manager_stop_timeout"
        )
        let upload = try helperError(
            operation: "deploy",
            code: "payload_upload_timeout",
            message: "Timed out copying smbd to remote path /Volumes/dk2/.samba4/smbd over SSH",
            localizationKey: "deploy.payload_upload_timeout"
        )

        L10n.apply(language: .english)
        XCTAssertEqual(
            [noHFS, writeTest, managerStop, upload].map(\.message),
            [
                "The disk is not ready for installation",
                "The disk did not respond",
                "A service on the device is stuck",
                "Copying the installation files timed out",
            ]
        )

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(
            [noHFS, writeTest, managerStop, upload].map(\.message),
            ["磁盘尚未准备好用于安装", "磁盘没有响应", "设备上的某项服务卡住了", "复制安装文件超时"]
        )
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
        XCTAssertEqual(english.message, "ACP or SSH authentication failed while configuring the device.")
        XCTAssertEqual(english.details, "The AirPort admin password did not work.")
        XCTAssertEqual(error.message, "AirPort password rejected")
        XCTAssertEqual(english.steps.first, "Re-enter the AirPort admin password.")

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "AirPort 密码被拒绝")
        XCTAssertEqual(chinese.message, "配置设备时 ACP 或 SSH 身份验证失败。")
        // The helper's own text stays English: it is for diagnostics.
        XCTAssertEqual(chinese.details, "The AirPort admin password did not work.")
        XCTAssertEqual(error.message, "AirPort 密码被拒绝")
        XCTAssertEqual(chinese.steps, ["重新输入 AirPort 管理员密码。", "确认所选设备是目标苹果设备。"])
    }

    func testSuggestedOperationAndActionIDsMapToUserFacingActions() throws {
        let recovery = try recoveryValue(
            title: "Disk issue",
            actions: ["Wake the disk by opening it in Finder.", "Run “Install / Update Samba” again."],
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

        // "open_finder" is not an action ID the backend emits, so it produces no button.
        XCTAssertEqual(actions, [
            RecoveryAction(title: "Install Samba", kind: .installSMB),
            RecoveryAction(title: "Run Disk Repair", kind: .diskRepair),
            RecoveryAction(title: "Retry", kind: .retry),
            RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics)
        ])
    }

    func testDeployRecoveryDoesNotShowInstallSMBOrUnknownActions() throws {
        let recovery = try recoveryValue(
            title: "No HFS volumes found",
            actions: ["Run “Install / Update Samba” again."],
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

        XCTAssertEqual(actions.map(\.kind), [.retry, .copyDiagnostics])
        XCTAssertEqual(actions.first, RecoveryAction(title: "Retry", kind: .retry))
        XCTAssertEqual(actions.last, RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics))
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

    func testNoRouteOnEveryAcpAttemptShowsTheLocalNetworkCard() throws {
        // As the helper sends it to Add Device and to SSH Access: the generic
        // English message and the Local Network card, with no suggested operation.
        let recovery = try JSONValue.object([
            "title": .string("Local Network access blocked"),
            "message": .string("macOS is blocking TimeCapsuleSMB from accessing devices on your local network."),
            "actions": .array([
                .string("Open “System Settings” > “Privacy & Security” > “Local Network”."),
                .string("Enable TimeCapsuleSMB in “Local Network”."),
                .string("Quit and reopen TimeCapsuleSMB, then try again."),
                .string("If TimeCapsuleSMB is already on, turn it off and on again. If that doesn't help, delete other copies of TimeCapsuleSMB and restart the Mac.")
            ]),
            "action_ids": .array([.string("open_system_settings"), .string("retry")]),
            "retryable": .bool(true),
            "suggested_operation": .null,
            "localization_key": .string("local_network_permission_denied")
        ]).decode(BackendRecoveryPayload.self)

        for operation in ["configure", "set-ssh"] {
            let error = BackendErrorViewModel(
                operation: operation,
                code: "acp_no_route",
                message: "Could not connect to ACP on 192.168.0.251:5009. Check the device IP address or hostname, and turn off any VPN or firewall that filters local network traffic.",
                recovery: recovery
            )

            L10n.apply(language: .english)
            let english = RecoveryGuidancePresentation(error: error)
            XCTAssertEqual(english.title, "Local Network access blocked", operation)
            // The line is the card's title, as for every error.
            XCTAssertEqual(error.message, english.title, operation)
            XCTAssertEqual(english.steps.count, 4, operation)
            // Every button works on both screens: none names another operation.
            XCTAssertEqual(
                RecoveryActionMapper.actions(for: error).map(\.kind),
                [.openSystemSettings, .retry, .copyDiagnostics],
                operation
            )

            L10n.apply(language: .simplifiedChinese)
            let chinese = RecoveryGuidancePresentation(error: error)
            XCTAssertEqual(chinese.title, "本地网络访问被阻止", operation)
            XCTAssertEqual(error.message, chinese.title, operation)
            XCTAssertEqual(chinese.details, error.diagnosticMessage, operation)
        }
    }

    func testSSHTurnedOffRecoveryOffersSSHAccessAndRetry() throws {
        // As the helper sends it: no suggested operation, one action ID.
        let recovery = try JSONValue.object([
            "title": .string("SSH is turned off"),
            "message": .string(
                "The device answers AirPort ACP, but its SSH port is closed. SSH turns off after a reset, or when it is disabled in SSH Access."
            ),
            "actions": .array([.string("Open SSH Access and choose Enable SSH, then try again.")]),
            "action_ids": .array([.string("open_ssh_access")]),
            "retryable": .bool(true),
            "suggested_operation": .null,
            "localization_key": .string("ssh_disabled")
        ]).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "ssh_disabled",
            message: "SSH is turned off on 10.0.0.2: AirPort ACP answers on port 5009, but SSH port 22 is closed.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        XCTAssertEqual(RecoveryActionMapper.actions(for: error), [
            RecoveryAction(title: "Open SSH Access", kind: .openSSHAccess),
            RecoveryAction(title: "Retry", kind: .retry),
            RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics)
        ])
        XCTAssertEqual(error.message, "SSH is turned off")
        XCTAssertEqual(RecoveryGuidancePresentation(error: error).title, "SSH is turned off")

        L10n.apply(language: .german)
        XCTAssertEqual(RecoveryActionMapper.actions(for: error).first?.title, "SSH-Zugriff öffnen")
        XCTAssertEqual(error.message, "SSH ist deaktiviert")
    }

    func testUnreachableDeviceErrorIsLocalizedWithoutUnsupportedModelAdvice() throws {
        let recovery = try JSONValue.object([
            "title": .string("Device not reachable"),
            "message": .string("Neither SSH nor AirPort ACP answered at the device's saved address."),
            "actions": .array([
                .string("Make sure the device is turned on and connected to the same network or Wi-Fi as this Mac."),
                .string("If the device is restarting, wait a few minutes, then try again.")
            ]),
            "action_ids": .array([]),
            "retryable": .bool(true),
            "suggested_operation": .null,
            "localization_key": .string("device_unreachable")
        ]).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "activate",
            code: "device_unreachable",
            message: "The device is not answering at 10.0.0.2: neither SSH port 22 nor AirPort ACP port 5009 is reachable (timed out).",
            recovery: recovery
        )

        L10n.apply(language: .english)
        XCTAssertEqual(RecoveryActionMapper.actions(for: error).map(\.kind), [.retry, .copyDiagnostics])
        let guidance = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(error.message, "Device not reachable")
        XCTAssertFalse(guidance.steps.contains { $0.contains("AirPort Express") || $0.contains("Forget") })
    }

    func testHumanRecoveryTextDoesNotCreateActionButtons() throws {
        let recovery = try recoveryValue(
            title: "Disk issue",
            actions: ["Wake the disk by opening it in Finder.", "Run “Install / Update Samba” again."],
            suggestedOperation: "unknown"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "remote_error",
            message: "Disk did not mount.",
            recovery: recovery
        )

        let actions = RecoveryActionMapper.actions(for: error)

        XCTAssertEqual(actions.map(\.kind), [.generic, .retry, .copyDiagnostics])
        XCTAssertFalse(actions.contains { $0.title.contains("Finder") })
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
            english.message,
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
                "Make sure you are connected to the same network or Wi-Fi as the device.",
                "On NetBSD 4 devices, run “Activate” once SSH is reachable; the install did not get far enough to activate Samba after the reboot.",
                "If your device resets itself, see https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
            ],
            actionIDs: ["run_checkup"],
            message: "The device went down but SSH did not return before the timeout.",
            localizationKey: "deploy.reboot_not_finished"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "reboot_not_finished",
            message: "Timed out waiting for SSH after reboot.",
            recovery: recovery
        )

        L10n.apply(language: .english)
        let english = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(english.title, "Reboot did not finish")
        XCTAssertEqual(
            english.message,
            "The installation files were copied and the restart was requested, but the device did not answer over SSH again in time. It may still be starting up, or it may have come back at a different IP address."
        )
        XCTAssertEqual(english.steps.count, 4)
        XCTAssertEqual(english.steps[1], "Make sure you are connected to the same network or Wi-Fi as the device.")
        XCTAssertEqual(
            english.steps[3],
            "If your device resets itself, see https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
        )

        L10n.apply(language: .simplifiedChinese)
        let chinese = RecoveryGuidancePresentation(error: error)
        XCTAssertEqual(chinese.title, "重启未完成")
        XCTAssertEqual(chinese.steps[0], "再等待几分钟。")
        XCTAssertEqual(chinese.steps.count, 4)
        XCTAssertEqual(
            chinese.steps[3],
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
            english.message,
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
        XCTAssertEqual(chinese.message, "AirPort Extreme 6th generation 响应非常慢。请重启设备。然后等待 SSH 恢复，再重试。")
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

        XCTAssertEqual(chinese.message, fallbackMessage)
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

        XCTAssertEqual(presentation.title, "No HFS volumes found")
        XCTAssertNil(presentation.message)
        XCTAssertNil(presentation.details)
        XCTAssertTrue(presentation.steps.isEmpty)
    }

    func testRetryIsOfferedOnlyWhereTheScreenCanRerunTheOperation() throws {
        let error = BackendErrorViewModel(
            operation: "flash",
            code: "validation_failed",
            message: "refusing to patch primary",
            recovery: try recoveryValue(title: "Request validation failed", actions: []).decode(BackendRecoveryPayload.self)
        )

        XCTAssertTrue(RecoveryActionMapper.actions(for: error).contains { $0.kind == .retry })
        let withoutRerun = RecoveryActionMapper.actions(for: error) { $0 != .retry }
        XCTAssertFalse(withoutRerun.contains { $0.kind == .retry })
        XCTAssertTrue(withoutRerun.contains { $0.kind == .copyDiagnostics })
    }

    func testErrorBoxShowsGuidanceInTheAppLanguageAndTheHelpersEnglishBelow() throws {
        let raw = "Copying the SMB payload timed out. The disk may be failing or unable to spin up."
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "payload_upload_timeout",
            message: raw,
            recovery: try recoveryValue(
                title: "Copying the installation files timed out",
                actions: ["Run “Disk Repair”.", "Connect by Ethernet.", "Replace the disk."],
                message: "The disk may be failing.",
                localizationKey: "deploy.payload_upload_timeout"
            ).decode(BackendRecoveryPayload.self)
        )

        L10n.apply(language: .german)
        let german = RecoveryGuidancePresentation(error: error)

        XCTAssertEqual(german.title, "Das Kopieren der Installationsdateien hat das Zeitlimit überschritten")
        XCTAssertTrue(german.message?.hasPrefix("Die Festplatte fällt möglicherweise aus") == true, german.message ?? "")
        XCTAssertEqual(german.steps.first, "Führen Sie die „Festplattenreparatur“ aus.")
        XCTAssertEqual(german.steps.count, 3)
        // The helper's text is diagnostics: English, whatever the app language.
        XCTAssertEqual(german.details, raw)
        let lines = [german.title, german.message ?? ""] + german.steps
        XCTAssertEqual(Set(lines).count, lines.count)
    }

    func testErrorBoxLeavesOutTheHelpersTextWhenTheGuidanceAlreadySaysIt() throws {
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "payload_upload_timeout",
            message: "Copying the installation files timed out.",
            recovery: try recoveryValue(
                title: "Copying the installation files timed out",
                actions: [],
                message: "The disk may be failing.",
                localizationKey: "deploy.payload_upload_timeout"
            ).decode(BackendRecoveryPayload.self)
        )

        L10n.apply(language: .english)
        XCTAssertNil(RecoveryGuidancePresentation(error: error).details)

        // Another language cannot repeat the English words, so the text shows.
        L10n.apply(language: .german)
        XCTAssertEqual(RecoveryGuidancePresentation(error: error).details, "Copying the installation files timed out.")
    }

    func testABoxShowsNoButtonItsScreenWouldIgnore() throws {
        let recovery = try recoveryValue(
            title: "The device reported an error",
            actions: [],
            actionIDs: ["open_system_settings"],
            localizationKey: "remote_error"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(operation: "configure", code: "remote_error", message: "failed", recovery: recovery)

        // The helper suggests Checkup; a screen that cannot run it gets no button for it.
        XCTAssertTrue(RecoveryActionMapper.actions(for: error).contains { $0.kind == .runCheckup })
        let shown = RecoveryActionMapper.actions(for: error) { [.openSystemSettings, .retry].contains($0) }.map(\.kind)
        XCTAssertEqual(shown, [.openSystemSettings, .retry, .copyDiagnostics])
        // Copy Diagnostics is the box's own, whatever the screen can do.
        XCTAssertEqual(RecoveryActionMapper.actions(for: error) { _ in false }.map(\.kind), [.copyDiagnostics])
    }

    func testErrorWithoutGuidanceShowsItsOwnTextOnce() {
        let error = BackendErrorViewModel(operation: "deploy", code: "remote_error", message: "Disk did not mount.")

        let presentation = RecoveryGuidancePresentation(error: error)

        XCTAssertEqual(presentation.title, "Disk did not mount.")
        XCTAssertNil(presentation.message)
        XCTAssertNil(presentation.details)
    }

    func testOneLineErrorIsTheTranslatedTitle() throws {
        let raw = "Native HFS metadata migration (copy) failed.\nphase=copy elapsed_seconds=83.371"
        let recovery = try recoveryValue(
            title: "The metadata migration failed",
            actions: [],
            localizationKey: "deploy.xattr_migration_failed"
        ).decode(BackendRecoveryPayload.self)
        let withGuidance = BackendErrorViewModel(
            operation: "deploy", code: "xattr_migration_failed", message: raw, recovery: recovery
        )
        let withoutGuidance = BackendErrorViewModel(operation: "deploy", code: "xattr_migration_failed", message: raw)

        L10n.apply(language: .german)
        XCTAssertEqual(withGuidance.message, "Die Metadaten-Migration ist fehlgeschlagen")
        XCTAssertEqual(withGuidance.diagnosticMessage, raw)
        // Nothing translated describes it: the helper's text is all there is.
        XCTAssertEqual(withoutGuidance.message, raw)

        // Every one-line error has the same shape: the title, never a sentence.
        let unreachable = try BackendErrorViewModel(
            operation: "deploy",
            code: "device_unreachable",
            message: "The device is not answering at 10.0.0.2.",
            recovery: recoveryValue(title: "Device not reachable", actions: [], localizationKey: "device_unreachable")
                .decode(BackendRecoveryPayload.self)
        )
        XCTAssertEqual(unreachable.message, "Gerät nicht erreichbar")
    }

    func testDeployGuidanceUsesStructuredRecoveryInsteadOfGenericTip() throws {
        let recovery = try recoveryValue(
            title: "Reboot did not finish",
            actions: ["Wait a few more minutes."],
            message: "The payload was uploaded.",
            localizationKey: "deploy.reboot_not_finished"
        ).decode(BackendRecoveryPayload.self)
        let error = BackendErrorViewModel(
            operation: "deploy",
            code: "reboot_not_finished",
            message: "Timed out waiting for SSH after reboot.",
            recovery: recovery
        )

        XCTAssertNil(DeployFailureGuidancePolicy.guidance(for: error))
    }
}
