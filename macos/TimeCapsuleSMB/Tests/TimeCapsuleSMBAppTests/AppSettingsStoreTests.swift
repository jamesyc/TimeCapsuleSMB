import Combine
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
            appearance: .dark,
            defaultBonjourTimeoutSeconds: 12.5,
            defaultDeviceSettings: DeviceProfileSettings(
                nbnsEnabled: false,
                internalShareUseDiskRoot: true,
                smbBrowseCompatibility: true,
                anyProtocol: true,
                fruitMetadataNetatalk: true,
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
        XCTAssertEqual(store.settings.appearance, .system)
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

        draft = AppSettingsDraft(settings: .default)
        draft.appearance = .dark
        XCTAssertEqual(try draft.validatedSettings().appearance, .dark)
    }

    func testLocalizationLanguageOverrideUsesSelectedBundleAndEnglishFallback() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        XCTAssertEqual(L10n.string("app_settings.title", language: .english), "Settings")
        XCTAssertEqual(L10n.string("app_settings.title", language: .simplifiedChinese), "设置")
        XCTAssertEqual(L10n.string("app_settings.title", language: .french), "Paramètres")
        XCTAssertEqual(L10n.string("app_settings.title", language: .german), "Einstellungen")
        XCTAssertEqual(L10n.string("app_settings.title", language: .dutch), "Instellingen")
        XCTAssertEqual(L10n.string("app_settings.title", language: .spanish), "Ajustes")
        XCTAssertEqual(L10n.string("app_settings.title", language: .italian), "Impostazioni")
        XCTAssertEqual(L10n.string("app_settings.title", language: .portuguese), "Configurações")
        XCTAssertEqual(L10n.string("app_settings.title", language: .russian), "Настройки")
        XCTAssertEqual(L10n.string("app_settings.title", language: .lithuanian), "Nustatymai")
        XCTAssertEqual(L10n.string("app_language.french", language: .french), "Français")
        XCTAssertEqual(L10n.string("app_language.german", language: .german), "Deutsch")
        XCTAssertEqual(L10n.string("app_language.russian", language: .russian), "Русский")
        XCTAssertEqual(L10n.string("app_language.lithuanian", language: .lithuanian), "Lietuvių")
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

    func testSupportedLocalizationsCoverEnglishKeysAndFormatTokens() {
        let english = L10n.strings(language: .english)
        let localizedLanguages = AppLanguage.allCases.filter { language in
            language.localizationIdentifier != nil && language != .english
        }

        XCTAssertFalse(english.isEmpty)
        for language in localizedLanguages {
            let localized = L10n.strings(language: language)
            XCTAssertEqual(Set(localized.keys), Set(english.keys), language.rawValue)
            for key in english.keys {
                XCTAssertEqual(
                    formatTokens(in: localized[key] ?? ""),
                    formatTokens(in: english[key] ?? ""),
                    "\(language.rawValue): \(key)"
                )
            }
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
        let deploy = testDeployState(
            startedAt: Date(timeIntervalSince1970: 1_700_000_000),
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000),
            finishedAt: Date(timeIntervalSince1970: 1_700_000_000),
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
        XCTAssertEqual(deploy.localizedSummary, "安装已完成。")
        XCTAssertEqual(L10n.string("install.timeline.title"), "状态")

        L10n.apply(language: .english)
        XCTAssertEqual(DoctorWorkflowState.running.title, "Running")
        XCTAssertEqual(DeployWorkflowState.planStale.title, "Plan Stale")
        XCTAssertEqual(MaintenanceWorkflow.fsck.title, "Disk Repair")
        XCTAssertEqual(FlashWorkflowState.writeLocked.title, "Ready")
        XCTAssertEqual(error.message, "Review and regenerate the deploy plan before deploying.")
        XCTAssertEqual(issue.message, "The bundled TimeCapsuleSMB helper is missing.")
        XCTAssertEqual(issue.recovery, "Reinstall TimeCapsuleSMB.")
        XCTAssertEqual(checkup.localizedSummary, "PASS 2, WARN 1, FAIL 0")
        XCTAssertEqual(deploy.localizedSummary, "Install completed.")
        XCTAssertEqual(L10n.string("install.timeline.title"), "Status")
    }

    func testFocusedSimplifiedChineseKeysDoNotFallBackToEnglishUiCopy() {
        let expectedChinese = [
            "button.discover": "发现",
            "app_appearance.dark": "深色",
            "checkup.presentation.row.fail": "失败",
            "backend.summary.doctor_checks_passed": "诊断检查通过。",
            "backend.summary.fsck_plan_generated": "已生成 fsck dry-run 计划。",
            "backend.summary.install_validation_passed": "安装验证通过。",
            "backend.summary.repair_xattrs_found": "发现 %d 个元数据问题，其中 %d 个可修复。",
            "dashboard.overview.connection_target": "连接目标",
            "deploy.presentation.row.pre_upload_actions": "上传前操作",
            "diagnostics.title": "诊断",
            "install.advanced_options": "高级选项",
            "maintenance.workflow.repair_xattrs": "文件元数据修复",
            "profile_editor.display_name": "显示名称",
            "timeline.state.pending": "等待中",
            "toggle.enable_debug_logging": "启用调试日志",
            "toggle.smb_browse_compatibility": "允许浏览 SMB 共享",
            "toggle.use_netatalk_metadata": "使用 Netatalk 存储元数据",
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

    func testSavingSettingsAppliesLanguageBeforePublishingSettings() async throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let temp = try TemporaryDirectory()
        let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let settingsStore = AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json"))
        await settingsStore.load()
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: settingsStore,
            deviceRegistry: DeviceRegistryStore(applicationSupportURL: temp.url),
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore()
        )
        var appearanceTitleAtPublication: String?
        var cancellables: Set<AnyCancellable> = []
        settingsStore.$settings
            .sink { settings in
                if settings.language == .simplifiedChinese {
                    appearanceTitleAtPublication = AppAppearance.dark.title
                }
            }
            .store(in: &cancellables)

        var settings = AppSettings.default
        settings.language = .simplifiedChinese
        try await appStore.saveAppSettings(settings)

        XCTAssertEqual(appearanceTitleAtPublication, "深色")
    }

    private func telemetryPayload(enabled: Bool) -> JSONValue {
        .object([
            "schema_version": .number(1),
            "install_id": .string("install-one"),
            "telemetry_enabled": .bool(enabled),
            "bootstrap_path": .string("/tmp/.bootstrap"),
            "summary": .string(enabled ? "Telemetry is enabled." : "Telemetry is disabled.")
        ])
    }

    private func formatTokens(in string: String) -> [String] {
        let pattern = "%(?:\\d+\\$)?(?:lld|[@df])"
        let regex = try! NSRegularExpression(pattern: pattern)
        let range = NSRange(string.startIndex..<string.endIndex, in: string)
        return regex.matches(in: string, range: range).map { match in
            String(string[Range(match.range, in: string)!])
        }
    }
}
