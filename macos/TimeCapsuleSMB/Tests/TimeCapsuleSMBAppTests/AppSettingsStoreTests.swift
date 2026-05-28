import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppSettingsStoreTests: XCTestCase {
    func testLoadMissingSettingsUsesDefaults() async throws {
        let temp = try TemporaryDirectory()
        let store = AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json"))

        await store.load()

        XCTAssertEqual(store.state, .loaded)
        XCTAssertEqual(store.settings, .default)
        XCTAssertNil(store.error)
    }

    func testSaveAndLoadRoundTripsAllSettings() async throws {
        let temp = try TemporaryDirectory()
        let settingsURL = temp.url.appendingPathComponent("settings.json")
        let saved = AppSettings(
            language: .simplifiedChinese,
            defaultBonjourTimeoutSeconds: 12.5,
            defaultDeviceSettings: DeviceProfileSettings(
                nbnsEnabled: false,
                internalShareUseDiskRoot: true,
                anyProtocol: true,
                debugLogging: true,
                mountWaitSeconds: 45,
                ataIdleSeconds: 600,
                ataStandby: 900
            ),
            telemetryEnabled: false,
            helperPathOverride: "/tmp/tcapsule",
            showRawBackendEventsByDefault: false,
            checkForUpdatesOnLaunch: false,
            versionCheckURL: "https://example.invalid/version.json",
            timeMachineWarningsEnabled: false
        )

        let writer = AppSettingsStore(settingsURL: settingsURL)
        try await writer.save(saved)
        let reader = AppSettingsStore(settingsURL: settingsURL)
        await reader.load()

        XCTAssertEqual(reader.state, .loaded)
        XCTAssertEqual(reader.settings, saved)
    }

    func testLegacySettingsWithoutLanguageUseSystemDefault() async throws {
        let temp = try TemporaryDirectory()
        let settingsURL = temp.url.appendingPathComponent("settings.json")
        try #"{"telemetryEnabled":false}"#.write(to: settingsURL, atomically: true, encoding: .utf8)
        let store = AppSettingsStore(settingsURL: settingsURL)

        await store.load()

        XCTAssertEqual(store.state, .loaded)
        XCTAssertEqual(store.settings.language, .system)
        XCTAssertFalse(store.settings.telemetryEnabled)
    }

    func testCorruptSettingsFailsWithoutReplacingDefaults() async throws {
        let temp = try TemporaryDirectory()
        let settingsURL = temp.url.appendingPathComponent("settings.json")
        try "{".write(to: settingsURL, atomically: true, encoding: .utf8)
        let store = AppSettingsStore(settingsURL: settingsURL)

        await store.load()

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.settings, .default)
        XCTAssertNotNil(store.error)
    }

    func testDraftValidationRejectsBadNumbersAndURLs() throws {
        var draft = AppSettingsDraft(settings: .default)
        draft.defaultBonjourTimeoutSeconds = "-1"
        XCTAssertThrowsError(try draft.validatedSettings()) { error in
            XCTAssertEqual(error as? AppSettingsValidationError, .invalidBonjourTimeout)
        }

        draft = AppSettingsDraft(settings: .default)
        draft.ataStandby = "abc"
        XCTAssertThrowsError(try draft.validatedSettings()) { error in
            XCTAssertEqual(error as? AppSettingsValidationError, .invalidAtaStandby)
        }

        draft = AppSettingsDraft(settings: .default)
        draft.versionCheckURL = "file:///tmp/version.json"
        XCTAssertThrowsError(try draft.validatedSettings()) { error in
            XCTAssertEqual(error as? AppSettingsValidationError, .invalidVersionCheckURL)
        }

        draft = AppSettingsDraft(settings: .default)
        draft.language = .simplifiedChinese
        XCTAssertEqual(try draft.validatedSettings().language, .simplifiedChinese)
    }

    func testLocalizationLanguageOverrideUsesSelectedBundleAndEnglishFallback() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        XCTAssertEqual(L10n.string("app_settings.title", language: .english), "Settings")
        XCTAssertEqual(L10n.string("app_settings.title", language: .simplifiedChinese), "设置")
        XCTAssertEqual(
            L10n.string("app_settings.subtitle", language: .simplifiedChinese),
            "新设备默认值和 App 级别行为。"
        )
        XCTAssertEqual(L10n.string("sidebar.activity", language: .simplifiedChinese), "活动")
        XCTAssertEqual(L10n.string("activity.active", language: .simplifiedChinese), "正在进行")
        XCTAssertEqual(
            L10n.format("activity.multiple_active", 2),
            "2 active operations"
        )

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(L10n.string("app_settings.title"), "设置")
        XCTAssertEqual(L10n.format("activity.multiple_active", 2), "2 个正在进行的操作")
    }

    func testSimplifiedChineseLocalizationCoversEnglishKeysAndFormatTokens() {
        let english = L10n.strings(language: .english)
        let simplifiedChinese = L10n.strings(language: .simplifiedChinese)

        XCTAssertFalse(english.isEmpty)
        XCTAssertEqual(Set(simplifiedChinese.keys), Set(english.keys))
        for key in english.keys {
            XCTAssertEqual(formatTokens(in: simplifiedChinese[key] ?? ""), formatTokens(in: english[key] ?? ""), key)
        }
    }

    func testStructuredLocalPresentationsRerenderAfterLanguageChange() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        let error = BackendErrorViewModel(operation: "deploy", localError: .deployPlanStale)
        let issue = BundleRuntimeIssue(code: .helperMissing, severity: .error)
        let checkup = DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 1_700_000_000),
            state: .passed,
            passCount: 2,
            warnCount: 1,
            failCount: 0,
            summary: "PASS 2, WARN 1, FAIL 0"
        )
        let deploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 1_700_000_000),
            state: .deployed,
            payloadFamily: nil,
            rebootRequested: nil,
            verified: true,
            summary: ""
        )

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(DoctorWorkflowState.running.title, "运行中")
        XCTAssertEqual(DeployWorkflowState.planStale.title, "计划已过期")
        XCTAssertEqual(MaintenanceWorkflow.fsck.title, "磁盘修复")
        XCTAssertEqual(FlashWorkflowState.writeLocked.title, "就绪")
        XCTAssertEqual(error.message, "部署前请检查并重新生成部署计划。")
        XCTAssertEqual(issue.message, "缺少捆绑的 TimeCapsuleSMB Helper。")
        XCTAssertEqual(issue.recovery, "重新安装 TimeCapsuleSMB。")
        XCTAssertEqual(checkup.localizedSummary, "PASS 2，WARN 1，FAIL 0")
        XCTAssertEqual(deploy.localizedSummary, "已安装，并已通过检查验证。")

        L10n.apply(language: .english)
        XCTAssertEqual(DoctorWorkflowState.running.title, "Running")
        XCTAssertEqual(DeployWorkflowState.planStale.title, "Plan Stale")
        XCTAssertEqual(MaintenanceWorkflow.fsck.title, "Disk Repair")
        XCTAssertEqual(FlashWorkflowState.writeLocked.title, "Ready")
        XCTAssertEqual(error.message, "Review and regenerate the deploy plan before deploying.")
        XCTAssertEqual(issue.message, "The bundled TimeCapsuleSMB helper is missing.")
        XCTAssertEqual(issue.recovery, "Reinstall TimeCapsuleSMB.")
        XCTAssertEqual(checkup.localizedSummary, "PASS 2, WARN 1, FAIL 0")
        XCTAssertEqual(deploy.localizedSummary, "Installed and verified by checkup.")
    }

    func testFocusedSimplifiedChineseKeysDoNotFallBackToEnglishUiCopy() {
        let expectedChinese = [
            "button.discover": "发现",
            "checkup.presentation.row.fail": "失败",
            "dashboard.overview.connection_target": "连接目标",
            "deploy.presentation.row.pre_upload_actions": "上传前操作",
            "diagnostics.title": "诊断",
            "install.advanced_options": "高级选项",
            "maintenance.workflow.repair_xattrs": "文件元数据修复",
            "profile_editor.display_name": "显示名称",
            "timeline.state.pending": "等待中",
            "toggle.enable_debug_logging": "启用调试日志",
            "value.never": "从未",
            "workflow.state.deploying": "正在部署"
        ]

        for (key, expectedValue) in expectedChinese {
            XCTAssertEqual(L10n.string(key, language: .simplifiedChinese), expectedValue, key)
            XCTAssertNotEqual(
                L10n.string(key, language: .simplifiedChinese),
                L10n.string(key, language: .english),
                key
            )
        }
    }

    func testSavingSettingsAppliesHelperPathAndRunsTelemetrySyncOnlyWhenNeeded() async throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let temp = try TemporaryDirectory()
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "set-telemetry", ok: true, payload: telemetryPayload(enabled: false))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let settingsStore = AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json"))
        await settingsStore.load()
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: settingsStore,
            deviceRegistry: DeviceRegistryStore(applicationSupportURL: temp.url),
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore()
        )

        var settings = AppSettings.default
        settings.language = .simplifiedChinese
        settings.telemetryEnabled = false
        try await appStore.saveAppSettings(settings)

        try await waitUntilStoreState { runner.calls.map(\.operation).contains("set-telemetry") }
        XCTAssertEqual(runner.calls.first?.params["enabled"], .bool(false))
        XCTAssertEqual(L10n.currentLanguage, .simplifiedChinese)

        var helperSettings = settings
        helperSettings.helperPathOverride = "/tmp/tcapsule-helper"
        try await appStore.saveAppSettings(helperSettings)

        XCTAssertEqual(appStore.backend.helperPath, "/tmp/tcapsule-helper")
    }

    private func telemetryPayload(enabled: Bool) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "install_id": .string("install-one"),
            "telemetry_enabled": .bool(enabled),
            "bootstrap_path": .string("/tmp/.bootstrap"),
            "summary": .string(enabled ? "telemetry is enabled." : "telemetry is disabled.")
        ])
    }

    private func formatTokens(in string: String) -> [String] {
        let pattern = "%(?:\\d+\\$)?[@df]"
        let regex = try! NSRegularExpression(pattern: pattern)
        let range = NSRange(string.startIndex..<string.endIndex, in: string)
        return regex.matches(in: string, range: range).map { match in
            String(string[Range(match.range, in: string)!])
        }
    }
}
