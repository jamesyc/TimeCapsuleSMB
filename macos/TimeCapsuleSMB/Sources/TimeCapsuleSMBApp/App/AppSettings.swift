import Combine
import Foundation

enum AppLanguage: String, CaseIterable, Codable, Identifiable, Equatable {
    case system
    case english = "en"
    case french = "fr"
    case german = "de"
    case dutch = "nl"
    case spanish = "es"
    case italian = "it"
    case portuguese = "pt"
    case russian = "ru"
    case lithuanian = "lt"
    case simplifiedChinese = "zh-Hans"

    var id: String {
        rawValue
    }

    var title: String {
        switch self {
        case .system:
            return L10n.string("app_language.system")
        case .english:
            return L10n.string("app_language.english")
        case .french:
            return L10n.string("app_language.french")
        case .german:
            return L10n.string("app_language.german")
        case .dutch:
            return L10n.string("app_language.dutch")
        case .spanish:
            return L10n.string("app_language.spanish")
        case .italian:
            return L10n.string("app_language.italian")
        case .portuguese:
            return L10n.string("app_language.portuguese")
        case .russian:
            return L10n.string("app_language.russian")
        case .lithuanian:
            return L10n.string("app_language.lithuanian")
        case .simplifiedChinese:
            return L10n.string("app_language.simplified_chinese")
        }
    }

    var localizationIdentifier: String? {
        switch self {
        case .system:
            return nil
        case .english:
            return rawValue
        case .french:
            return rawValue
        case .german:
            return rawValue
        case .dutch:
            return rawValue
        case .spanish:
            return rawValue
        case .italian:
            return rawValue
        case .portuguese:
            return rawValue
        case .russian:
            return rawValue
        case .lithuanian:
            return rawValue
        case .simplifiedChinese:
            return rawValue
        }
    }

    var locale: Locale {
        switch self {
        case .system:
            return .current
        case .english:
            return Locale(identifier: "en")
        case .french:
            return Locale(identifier: "fr")
        case .german:
            return Locale(identifier: "de")
        case .dutch:
            return Locale(identifier: "nl")
        case .spanish:
            return Locale(identifier: "es")
        case .italian:
            return Locale(identifier: "it")
        case .portuguese:
            return Locale(identifier: "pt")
        case .russian:
            return Locale(identifier: "ru")
        case .lithuanian:
            return Locale(identifier: "lt")
        case .simplifiedChinese:
            return Locale(identifier: "zh-Hans-CN")
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let rawValue = try container.decode(String.self)
        self = AppLanguage(rawValue: rawValue) ?? .system
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

enum AppAppearance: String, CaseIterable, Codable, Identifiable, Equatable {
    case system
    case light
    case dark

    var id: String {
        rawValue
    }

    var title: String {
        switch self {
        case .system:
            return L10n.string("app_appearance.system")
        case .light:
            return L10n.string("app_appearance.light")
        case .dark:
            return L10n.string("app_appearance.dark")
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let rawValue = try container.decode(String.self)
        self = AppAppearance(rawValue: rawValue) ?? .system
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

struct AppSettings: Codable, Equatable {
    var language: AppLanguage
    var appearance: AppAppearance
    var defaultBonjourTimeoutSeconds: Double
    var defaultDeviceSettings: DeviceProfileSettings
    var telemetryEnabled: Bool
    var helperPathOverride: String
    var showRawBackendEventsByDefault: Bool
    var checkForUpdatesOnLaunch: Bool
    var versionCheckURL: String
    var timeMachineWarningsEnabled: Bool

    static let `default` = AppSettings(
        language: .system,
        appearance: .system,
        defaultBonjourTimeoutSeconds: 6,
        defaultDeviceSettings: .default,
        telemetryEnabled: true,
        helperPathOverride: "",
        showRawBackendEventsByDefault: true,
        checkForUpdatesOnLaunch: true,
        versionCheckURL: "",
        timeMachineWarningsEnabled: true
    )

    init(
        language: AppLanguage = .system,
        appearance: AppAppearance = .system,
        defaultBonjourTimeoutSeconds: Double,
        defaultDeviceSettings: DeviceProfileSettings,
        telemetryEnabled: Bool,
        helperPathOverride: String,
        showRawBackendEventsByDefault: Bool,
        checkForUpdatesOnLaunch: Bool,
        versionCheckURL: String,
        timeMachineWarningsEnabled: Bool
    ) {
        self.language = language
        self.appearance = appearance
        self.defaultBonjourTimeoutSeconds = defaultBonjourTimeoutSeconds
        self.defaultDeviceSettings = defaultDeviceSettings
        self.telemetryEnabled = telemetryEnabled
        self.helperPathOverride = helperPathOverride
        self.showRawBackendEventsByDefault = showRawBackendEventsByDefault
        self.checkForUpdatesOnLaunch = checkForUpdatesOnLaunch
        self.versionCheckURL = versionCheckURL
        self.timeMachineWarningsEnabled = timeMachineWarningsEnabled
    }

    private enum CodingKeys: String, CodingKey {
        case language
        case appearance
        case defaultBonjourTimeoutSeconds
        case defaultDeviceSettings
        case telemetryEnabled
        case helperPathOverride
        case showRawBackendEventsByDefault
        case checkForUpdatesOnLaunch
        case versionCheckURL
        case timeMachineWarningsEnabled
    }

    init(from decoder: Decoder) throws {
        let defaults = Self.default
        let container = try decoder.container(keyedBy: CodingKeys.self)
        language = try container.decodeIfPresent(AppLanguage.self, forKey: .language) ?? defaults.language
        appearance = try container.decodeIfPresent(AppAppearance.self, forKey: .appearance) ?? defaults.appearance
        defaultBonjourTimeoutSeconds = Self.decodeNonNegativeDouble(
            from: container,
            forKey: .defaultBonjourTimeoutSeconds,
            defaultValue: defaults.defaultBonjourTimeoutSeconds
        )
        defaultDeviceSettings = try container.decodeIfPresent(DeviceProfileSettings.self, forKey: .defaultDeviceSettings)
            ?? defaults.defaultDeviceSettings
        telemetryEnabled = try container.decodeIfPresent(Bool.self, forKey: .telemetryEnabled) ?? defaults.telemetryEnabled
        helperPathOverride = try container.decodeIfPresent(String.self, forKey: .helperPathOverride) ?? defaults.helperPathOverride
        showRawBackendEventsByDefault = try container.decodeIfPresent(Bool.self, forKey: .showRawBackendEventsByDefault)
            ?? defaults.showRawBackendEventsByDefault
        checkForUpdatesOnLaunch = try container.decodeIfPresent(Bool.self, forKey: .checkForUpdatesOnLaunch)
            ?? defaults.checkForUpdatesOnLaunch
        versionCheckURL = try container.decodeIfPresent(String.self, forKey: .versionCheckURL) ?? defaults.versionCheckURL
        timeMachineWarningsEnabled = try container.decodeIfPresent(Bool.self, forKey: .timeMachineWarningsEnabled)
            ?? defaults.timeMachineWarningsEnabled
    }

    private static func decodeNonNegativeDouble(
        from container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys,
        defaultValue: Double
    ) -> Double {
        guard let value = try? container.decodeIfPresent(Double.self, forKey: key),
              value.isFinite,
              value >= 0
        else {
            return defaultValue
        }
        return value
    }
}

enum AppSettingsValidationError: Equatable, LocalizedError {
    case invalidBonjourTimeout
    case invalidMountWait
    case invalidAtaIdleSeconds
    case invalidAtaStandby
    case invalidVersionCheckURL

    var errorDescription: String? {
        switch self {
        case .invalidBonjourTimeout:
            return L10n.string("app_settings.error.bonjour_timeout")
        case .invalidMountWait:
            return L10n.string("app_settings.error.mount_wait")
        case .invalidAtaIdleSeconds:
            return L10n.string("app_settings.error.ata_idle")
        case .invalidAtaStandby:
            return L10n.string("app_settings.error.ata_standby")
        case .invalidVersionCheckURL:
            return L10n.string("app_settings.error.version_url")
        }
    }
}

enum AppSettingsState: String, Equatable {
    case idle
    case loading
    case loaded
    case saving
    case failed
}

enum AppSettingsStoreError: Equatable, LocalizedError {
    case corruptSettings(String)
    case io(String)

    var errorDescription: String? {
        switch self {
        case .corruptSettings(let message):
            return L10n.format("app_settings.error.corrupt", message)
        case .io(let message):
            return message
        }
    }
}

@MainActor
final class AppSettingsStore: ObservableObject {
    @Published private(set) var state: AppSettingsState = .idle
    @Published private(set) var settings: AppSettings = .default
    @Published private(set) var error: AppSettingsStoreError?

    let settingsURL: URL

    private let repository: AppSettingsRepository

    convenience init() {
        let appSupport = BundleLayout.applicationSupportDirectory() ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/TimeCapsuleSMB", isDirectory: true)
        self.init(settingsURL: appSupport.appendingPathComponent("app-settings.json"))
    }

    init(settingsURL: URL, fileManager: FileManager = .default) {
        self.settingsURL = settingsURL
        self.repository = AppSettingsRepository(settingsURL: settingsURL, fileManager: fileManager)
    }

    func load() async {
        state = .loading
        error = nil
        do {
            settings = try await repository.load()
            state = .loaded
        } catch {
            fail(error)
        }
    }

    func save(_ nextSettings: AppSettings, willPublish: ((AppSettings) -> Void)? = nil) async throws {
        state = .saving
        error = nil
        do {
            try await repository.save(nextSettings)
            willPublish?(nextSettings)
            settings = nextSettings
            state = .loaded
        } catch {
            fail(error)
            throw error
        }
    }

    func reset() async throws {
        try await save(.default)
    }

    private func fail(_ error: Error) {
        if let appSettingsError = error as? AppSettingsStoreError {
            self.error = appSettingsError
        } else {
            self.error = .io(error.localizedDescription)
        }
        state = .failed
    }
}

private actor AppSettingsRepository {
    private let settingsURL: URL
    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(settingsURL: URL, fileManager: FileManager) {
        self.settingsURL = settingsURL
        self.fileManager = fileManager

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        self.encoder = encoder
        self.decoder = JSONDecoder()
    }

    func load() throws -> AppSettings {
        guard fileManager.fileExists(atPath: settingsURL.path) else {
            return .default
        }
        do {
            let data = try Data(contentsOf: settingsURL)
            return try decoder.decode(AppSettings.self, from: data)
        } catch let decoding as DecodingError {
            throw AppSettingsStoreError.corruptSettings(String(describing: decoding))
        } catch let settingsError as AppSettingsStoreError {
            throw settingsError
        } catch {
            throw AppSettingsStoreError.io(error.localizedDescription)
        }
    }

    func save(_ settings: AppSettings) throws {
        do {
            try fileManager.createDirectory(
                at: settingsURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let data = try encoder.encode(settings)
            try data.write(to: settingsURL, options: [.atomic])
        } catch {
            throw AppSettingsStoreError.io(error.localizedDescription)
        }
    }
}

struct AppSettingsDraft: Equatable {
    var language: AppLanguage
    var appearance: AppAppearance
    var defaultBonjourTimeoutSeconds: String
    var nbnsEnabled: Bool
    var internalShareUseDiskRoot: Bool
    var smbBindLanOnly: Bool
    var smbBrowseCompatibility: Bool
    var mdnsAdvertiseAFP: Bool
    var anyProtocol: Bool
    var requireSMBEncryption: Bool
    var forceDisableSMBSigningAndEncryption: Bool
    var fruitMetadataNetatalk: Bool
    var debugLogging: Bool
    var mountWaitSeconds: String
    var ataIdleSeconds: String
    var ataStandby: String
    var telemetryEnabled: Bool
    var helperPathOverride: String
    var showRawBackendEventsByDefault: Bool
    var checkForUpdatesOnLaunch: Bool
    var versionCheckURL: String
    var timeMachineWarningsEnabled: Bool

    init(settings: AppSettings) {
        language = settings.language
        appearance = settings.appearance
        defaultBonjourTimeoutSeconds = Self.formatDouble(settings.defaultBonjourTimeoutSeconds)
        nbnsEnabled = settings.defaultDeviceSettings.nbnsEnabled
        internalShareUseDiskRoot = settings.defaultDeviceSettings.internalShareUseDiskRoot
        smbBindLanOnly = settings.defaultDeviceSettings.smbBindLanOnly
        smbBrowseCompatibility = settings.defaultDeviceSettings.smbBrowseCompatibility
        mdnsAdvertiseAFP = settings.defaultDeviceSettings.mdnsAdvertiseAFP
        anyProtocol = settings.defaultDeviceSettings.anyProtocol
        requireSMBEncryption = settings.defaultDeviceSettings.requireSMBEncryption
        forceDisableSMBSigningAndEncryption = settings.defaultDeviceSettings.forceDisableSMBSigningAndEncryption
        fruitMetadataNetatalk = settings.defaultDeviceSettings.fruitMetadataNetatalk
        debugLogging = settings.defaultDeviceSettings.debugLogging
        mountWaitSeconds = String(settings.defaultDeviceSettings.mountWaitSeconds)
        ataIdleSeconds = String(settings.defaultDeviceSettings.ataIdleSeconds)
        ataStandby = settings.defaultDeviceSettings.ataStandby.map(String.init) ?? ""
        telemetryEnabled = settings.telemetryEnabled
        helperPathOverride = settings.helperPathOverride
        showRawBackendEventsByDefault = settings.showRawBackendEventsByDefault
        checkForUpdatesOnLaunch = settings.checkForUpdatesOnLaunch
        versionCheckURL = settings.versionCheckURL
        timeMachineWarningsEnabled = settings.timeMachineWarningsEnabled
    }

    func validatedSettings() throws -> AppSettings {
        guard let bonjourTimeout = ValueParsers.nonNegativeDouble(defaultBonjourTimeoutSeconds) else {
            throw AppSettingsValidationError.invalidBonjourTimeout
        }
        guard let mountWait = ValueParsers.nonNegativeInteger(mountWaitSeconds) else {
            throw AppSettingsValidationError.invalidMountWait
        }
        guard let ataIdle = ValueParsers.nonNegativeInteger(ataIdleSeconds) else {
            throw AppSettingsValidationError.invalidAtaIdleSeconds
        }
        let trimmedAtaStandby = ataStandby.trimmingCharacters(in: .whitespacesAndNewlines)
        let parsedAtaStandby: Int?
        if trimmedAtaStandby.isEmpty {
            parsedAtaStandby = nil
        } else if let value = ValueParsers.nonNegativeInteger(trimmedAtaStandby) {
            parsedAtaStandby = value
        } else {
            throw AppSettingsValidationError.invalidAtaStandby
        }

        let trimmedVersionURL = versionCheckURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedVersionURL.isEmpty, !Self.isHTTPURL(trimmedVersionURL) {
            throw AppSettingsValidationError.invalidVersionCheckURL
        }

        return AppSettings(
            language: language,
            appearance: appearance,
            defaultBonjourTimeoutSeconds: bonjourTimeout,
            defaultDeviceSettings: DeviceProfileSettings(
                nbnsEnabled: nbnsEnabled,
                internalShareUseDiskRoot: internalShareUseDiskRoot,
                smbBindLanOnly: smbBindLanOnly,
                smbBrowseCompatibility: smbBrowseCompatibility,
                mdnsAdvertiseAFP: mdnsAdvertiseAFP,
                anyProtocol: anyProtocol,
                requireSMBEncryption: requireSMBEncryption,
                forceDisableSMBSigningAndEncryption: forceDisableSMBSigningAndEncryption,
                fruitMetadataNetatalk: fruitMetadataNetatalk,
                debugLogging: debugLogging,
                mountWaitSeconds: mountWait,
                ataIdleSeconds: ataIdle,
                ataStandby: parsedAtaStandby
            ),
            telemetryEnabled: telemetryEnabled,
            helperPathOverride: helperPathOverride.trimmingCharacters(in: .whitespacesAndNewlines),
            showRawBackendEventsByDefault: showRawBackendEventsByDefault,
            checkForUpdatesOnLaunch: checkForUpdatesOnLaunch,
            versionCheckURL: trimmedVersionURL,
            timeMachineWarningsEnabled: timeMachineWarningsEnabled
        )
    }

    private static func formatDouble(_ value: Double) -> String {
        guard value.rounded() == value else {
            return String(value)
        }
        return String(Int(value))
    }

    private static func isHTTPURL(_ text: String) -> Bool {
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              url.host != nil
        else {
            return false
        }
        return true
    }
}

@MainActor
final class AppSettingsEditorStore: ObservableObject {
    @Published var draft: AppSettingsDraft
    @Published private(set) var baseline: AppSettings
    @Published private(set) var isSaving = false
    @Published private(set) var errorMessage: String?

    init(settings: AppSettings = .default) {
        self.baseline = settings
        self.draft = AppSettingsDraft(settings: settings)
    }

    var hasChanges: Bool {
        guard let settings = try? draft.validatedSettings() else {
            return true
        }
        return settings != baseline
    }

    var validationError: String? {
        do {
            _ = try draft.validatedSettings()
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    var canSave: Bool {
        validationError == nil && hasChanges && !isSaving
    }

    func sync(settings: AppSettings) {
        guard !isSaving else {
            return
        }
        baseline = settings
        draft = AppSettingsDraft(settings: settings)
        errorMessage = nil
    }

    func resetDraft() {
        draft = AppSettingsDraft(settings: baseline)
        errorMessage = nil
    }

    func restoreDefaultsDraft() {
        draft = AppSettingsDraft(settings: .default)
        errorMessage = nil
    }

    func save(appStore: AppStore) async {
        do {
            let settings = try draft.validatedSettings()
            isSaving = true
            errorMessage = nil
            try await appStore.saveAppSettings(settings)
            baseline = settings
            draft = AppSettingsDraft(settings: settings)
        } catch {
            errorMessage = error.localizedDescription
        }
        isSaving = false
    }
}
