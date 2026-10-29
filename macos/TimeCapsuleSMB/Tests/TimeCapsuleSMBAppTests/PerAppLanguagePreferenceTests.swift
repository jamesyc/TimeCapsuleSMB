import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class PerAppLanguagePreferenceTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suiteName = ""
    private var originalLanguage = AppLanguage.system

    override func setUp() async throws {
        try await super.setUp()
        suiteName = "PerAppLanguagePreferenceTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        originalLanguage = L10n.currentLanguage
    }

    override func tearDown() async throws {
        L10n.apply(language: originalLanguage)
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        try await super.tearDown()
    }

    /// What this app's own domain holds, without the global domain's list
    /// that `stringArray(forKey:)` falls back to.
    private var savedLanguages: [String]? {
        defaults.persistentDomain(forName: suiteName)?[PerAppLanguagePreference.key] as? [String]
    }

    func testChoosingALanguageMakesItTheAppsMacOSLanguage() {
        let preference = PerAppLanguagePreference(defaults: defaults, domain: suiteName)
        for language in AppLanguage.allCases where language != .system {
            preference.follow(from: .system, to: language)
            XCTAssertEqual(savedLanguages, [language.rawValue], "\(language)")
        }
    }

    func testChoosingSystemRemovesTheEarlierChoice() {
        let preference = PerAppLanguagePreference(defaults: defaults, domain: suiteName)
        preference.follow(from: .system, to: .german)

        preference.follow(from: .german, to: .system)

        XCTAssertNil(savedLanguages)
    }

    func testChoosingSystemKeepsALanguageSetInSystemSettings() {
        let preference = PerAppLanguagePreference(defaults: defaults, domain: suiteName)
        preference.follow(from: .system, to: .german)
        // System Settings then sets the app to French.
        defaults.set(["fr"], forKey: PerAppLanguagePreference.key)

        preference.follow(from: .german, to: .system)

        XCTAssertEqual(savedLanguages, ["fr"])
    }

    func testSavingSettingsWritesOnlyWhenTheLanguageChanges() async throws {
        let (appStore, _) = try await makeAppStore()

        var settings = AppSettings.default
        settings.language = .german
        try await appStore.saveAppSettings(settings)
        XCTAssertEqual(savedLanguages, ["de"])

        // A save that leaves the language alone does not undo System Settings.
        defaults.set(["fr"], forKey: PerAppLanguagePreference.key)
        settings.telemetryEnabled.toggle()
        try await appStore.saveAppSettings(settings)
        XCTAssertEqual(savedLanguages, ["fr"])

        settings.language = .system
        defaults.set(["de"], forKey: PerAppLanguagePreference.key)
        try await appStore.saveAppSettings(settings)
        XCTAssertNil(savedLanguages)
    }

    func testLaunchingWithASavedLanguageLeavesSystemSettingsAlone() async throws {
        let (first, settingsURL) = try await makeAppStore()
        var settings = AppSettings.default
        settings.language = .german
        try await first.saveAppSettings(settings)
        defaults.set(["fr"], forKey: PerAppLanguagePreference.key)
        // A new process starts with no language applied yet.
        L10n.apply(language: .system)

        let (relaunched, _) = try await makeAppStore(settingsURL: settingsURL)
        await relaunched.start()

        XCTAssertEqual(L10n.currentLanguage, .german)
        XCTAssertEqual(savedLanguages, ["fr"])
    }

    func testLaunchingWithALanguageMacOSNeverGotHandsItOver() async throws {
        let (first, settingsURL) = try await makeAppStore()
        var settings = AppSettings.default
        settings.language = .german
        try await first.saveAppSettings(settings)
        // As saved by a version that never wrote the language for macOS.
        defaults.removeObject(forKey: PerAppLanguagePreference.key)
        L10n.apply(language: .system)

        let (relaunched, _) = try await makeAppStore(settingsURL: settingsURL)
        await relaunched.start()

        XCTAssertEqual(L10n.currentLanguage, .german)
        XCTAssertEqual(savedLanguages, ["de"])
    }

    func testLaunchingWithSystemWritesNothing() async throws {
        let (appStore, _) = try await makeAppStore()

        await appStore.start()

        XCTAssertNil(savedLanguages)
    }

    func testSystemMeansTheTranslationMacOSWouldPick() {
        let cases: [([String], AppLanguage)] = [
            (["fr-CA", "de"], .french),
            (["ja", "de-AT"], .german),
            (["zh-Hans-CN"], .simplifiedChinese),
            (["pt-BR"], .portuguese),
            (["lt-LT", "en"], .lithuanian),
            (["ja"], .english),
            ([], .english),
        ]
        for (preferences, expected) in cases {
            XCTAssertEqual(L10n.systemLanguage(preferences: preferences), expected, "\(preferences)")
        }
    }

    func testSwitchingToSystemShowsMacOSsLanguageAtOnce() {
        L10n.apply(language: .german, systemPreferences: ["de"])

        L10n.apply(language: .system, systemPreferences: ["fr-FR", "en"])

        XCTAssertEqual(L10n.currentLanguage, .system)
        XCTAssertEqual(L10n.string("app_settings.language"), L10n.string("app_settings.language", language: .french))
        XCTAssertEqual(L10n.strings(language: .system), L10n.strings(language: .french))
    }

    func testSystemFormatsWithTheLanguageItShows() throws {
        L10n.apply(language: .system, systemPreferences: ["ru"])
        try XCTSkipIf(Locale.current.language.languageCode?.identifier == "ru", "macOS itself is in Russian here")

        // Russian plural rules for Russian text, not the launch locale's.
        XCTAssertEqual(AppLanguage.system.locale.language.languageCode?.identifier, "ru")
    }

    func testSystemKeepsTheUsersOwnLocaleWhileItSpeaksItsLanguage() throws {
        let code = Locale.current.language.languageCode?.identifier ?? ""
        let shipped = AppLanguage.allCases.first { $0 != .system && $0.locale.language.languageCode?.identifier == code }
        guard let shipped else {
            throw XCTSkip("macOS here speaks a language the app has no translation for")
        }

        L10n.apply(language: .system, systemPreferences: [shipped.rawValue])

        // Region and all, such as its decimal separator.
        XCTAssertEqual(AppLanguage.system.locale, Locale.current)
    }

    func testSystemKeepsTheRegionOfAMacInALanguageTheAppDoesNotTranslate() {
        // A Mac in Japanese gets English text, with Japan's date formats.
        let locale = AppLanguage.locale(for: .english, keeping: Locale(identifier: "ja_JP"))

        XCTAssertEqual(locale.language.languageCode?.identifier, "en")
        XCTAssertEqual(locale.region?.identifier, "JP")
    }

    func testSystemKeepsTheFormatsChosenInSystemSettings() {
        let mac = Locale(identifier: "ja_JP@hours=h12;fw=mon;measure=ussystem")

        let locale = AppLanguage.locale(for: .french, keeping: mac)

        XCTAssertEqual(locale.language.languageCode?.identifier, "fr")
        XCTAssertEqual(locale.region?.identifier, "JP")
        XCTAssertEqual(locale.hourCycle, .oneToTwelve)
        XCTAssertEqual(locale.firstDayOfWeek, .monday)
        XCTAssertEqual(locale.measurementSystem, .us)
    }

    func testSystemKeepsTheTranslationsRegionForAMacWithoutOne() {
        let locale = AppLanguage.locale(for: .simplifiedChinese, keeping: Locale(identifier: "ja"))

        XCTAssertEqual(locale.language.languageCode?.identifier, "zh")
        XCTAssertEqual(locale.language.script?.identifier, "Hans")
        XCTAssertEqual(locale.region?.identifier, "CN")
    }

    func testSystemKeepsTheWholeLocaleOfAMacInTheTranslatedLanguage() {
        for (current, translation) in [("en_DE", AppLanguage.english), ("zh_CN", .simplifiedChinese), ("pt_BR", .portuguese)] {
            let mac = Locale(identifier: current)
            XCTAssertEqual(AppLanguage.locale(for: translation, keeping: mac), mac, current)
        }
    }

    func testSavingSystemAfterALanguageShowsMacOSsLanguageAtOnce() async throws {
        let (appStore, _) = try await makeAppStore()
        let withoutOurChoice = L10n.systemLanguage(preferences: defaults.stringArray(forKey: PerAppLanguagePreference.key))
        try XCTSkipIf(withoutOurChoice == .german, "macOS itself prefers German here")
        var settings = AppSettings.default
        settings.language = .german
        try await appStore.saveAppSettings(settings)

        settings.language = .system
        try await appStore.saveAppSettings(settings)

        XCTAssertNil(savedLanguages)
        XCTAssertEqual(L10n.systemTranslation, withoutOurChoice)
        XCTAssertEqual(L10n.string("app_settings.language"), L10n.string("app_settings.language", language: withoutOurChoice))
    }

    func testSettingsSaysWhenMenusFollowTheLanguage() async throws {
        let fixture = try await AppViewFixture()
        let view = AppSettingsView(
            appStore: fixture.appStore,
            appSettingsStore: fixture.appStore.appSettingsStore,
            editor: fixture.composition.appSettingsEditorStore,
            updater: AppUpdater()
        )

        let texts = accessibleElements(view, size: CGSize(width: 900, height: 2400)).map(\.text)

        XCTAssertTrue(texts.contains(L10n.string("app_settings.language_relaunch_notice")), "\(texts)")
    }

    private func makeAppStore(settingsURL: URL? = nil) async throws -> (AppStore, URL) {
        let temp = try TemporaryDirectory()
        addTeardownBlock { _ = temp }
        let url = settingsURL ?? temp.url.appendingPathComponent("settings.json")
        let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let settingsStore = AppSettingsStore(settingsURL: url)
        await settingsStore.load()
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: settingsStore,
            deviceRegistry: DeviceRegistryStore(applicationSupportURL: temp.url),
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore(),
            perAppLanguage: PerAppLanguagePreference(defaults: defaults, domain: suiteName)
        )
        return (appStore, url)
    }
}
