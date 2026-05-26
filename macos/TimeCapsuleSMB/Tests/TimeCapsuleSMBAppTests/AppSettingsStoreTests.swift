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

        XCTAssertEqual(L10n.string("app_settings.title", language: .english), "Settings")
        XCTAssertEqual(L10n.string("app_settings.title", language: .simplifiedChinese), "设置")
        XCTAssertEqual(
            L10n.string("app_settings.subtitle", language: .simplifiedChinese),
            "新设备默认值和 App 级别行为。"
        )

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(L10n.string("app_settings.title"), "设置")
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
