import Foundation

/// Announces a change of the in-app language to views no store refreshes,
/// such as the menu commands on the executable's Scene.
@MainActor
final class AppLanguageChanges: ObservableObject {
    static let shared = AppLanguageChanges()
}

/// The app's own language in macOS's sense: `AppleLanguages` in the app's
/// defaults, which System Settings (Language & Region, Applications) also
/// writes. AppKit's menus and Sparkle's windows read it only at launch, so a
/// language chosen in Settings reaches them the next time the app opens.
struct PerAppLanguagePreference {
    static let key = "AppleLanguages"
    let defaults: UserDefaults
    /// The app's own defaults domain, which holds the value without the
    /// system's list that `defaults` falls back to.
    let domain: String

    /// macOS's preferred languages for this app now: its own choice, else
    /// the system's.
    var languages: [String]? {
        defaults.stringArray(forKey: Self.key)
    }

    /// Writes a language saved in Settings that never reached macOS, as when
    /// it was chosen before the app wrote this value, but only while the
    /// app's own defaults hold none: a language set in System Settings stays.
    func adopt(_ language: AppLanguage) {
        guard let identifier = language.localizationIdentifier,
              defaults.persistentDomain(forName: domain)?[Self.key] == nil else {
            return
        }
        defaults.set([identifier], forKey: Self.key)
    }

    /// Follows a language the user chose in Settings. Choosing System removes
    /// the value only when the earlier choice in Settings wrote it, so a
    /// language set in System Settings stays.
    func follow(from previous: AppLanguage, to language: AppLanguage) {
        if let identifier = language.localizationIdentifier {
            defaults.set([identifier], forKey: Self.key)
        } else if let previousIdentifier = previous.localizationIdentifier,
                  defaults.stringArray(forKey: Self.key) == [previousIdentifier] {
            defaults.removeObject(forKey: Self.key)
        }
    }
}

enum L10n {
    private static let lock = NSLock()
    private static var selectedLanguage: AppLanguage = .system
    private static var systemChoice = systemLanguage(preferences: macOSLanguages())

    static var currentLanguage: AppLanguage {
        lock.lock()
        defer { lock.unlock() }
        return selectedLanguage
    }

    /// Also settles what System means from macOS's languages as they are now.
    /// A bundle picks its language once per process, so leaving System to it
    /// would keep a language the app's own defaults held at launch.
    static func apply(language: AppLanguage, systemPreferences: [String]? = macOSLanguages()) {
        let system = systemLanguage(preferences: systemPreferences)
        lock.lock()
        selectedLanguage = language
        systemChoice = system
        lock.unlock()
    }

    /// The translation macOS would pick for these preferred languages,
    /// English when none matches.
    static func systemLanguage(preferences: [String]?) -> AppLanguage {
        let available = AppLanguage.allCases.compactMap(\.localizationIdentifier)
        let pick = Bundle.preferredLocalizations(from: available, forPreferences: preferences).first
        return AppLanguage.allCases.first { $0.localizationIdentifier == pick } ?? .english
    }

    /// The translation System stands for right now.
    static var systemTranslation: AppLanguage {
        lock.lock()
        defer { lock.unlock() }
        return systemChoice
    }

    static func macOSLanguages() -> [String]? {
        UserDefaults.standard.stringArray(forKey: PerAppLanguagePreference.key)
    }

    static func string(_ key: String) -> String {
        string(key, language: currentLanguage)
    }

    /// Plural keys live in Localizable.stringsdict: pass the template straight
    /// from `string(_:language:)` to `String(format:)`, since a copy loses its
    /// plural rules. Foundation picks the plural form from the locale passed.
    static func format(_ key: String, _ arguments: CVarArg...) -> String {
        let language = currentLanguage
        return String(format: string(key, language: language), locale: language.locale, arguments: arguments)
    }

    static func string(_ key: String, language: AppLanguage) -> String {
        let fallback = bundle(for: .english)?.localizedString(forKey: key, value: key, table: nil) ?? key
        guard let bundle = bundle(for: language) else {
            return fallback
        }
        return bundle.localizedString(forKey: key, value: fallback, table: nil)
    }

    static func strings(language: AppLanguage) -> [String: String] {
        guard let bundle = bundle(for: language) ?? bundle(for: .english),
              let url = bundle.url(forResource: "Localizable", withExtension: "strings"),
              let data = try? Data(contentsOf: url),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil),
              let strings = plist as? [String: String] else {
            return [:]
        }
        return strings
    }

    private static func bundle(for language: AppLanguage) -> Bundle? {
        guard let identifier = (language == .system ? systemTranslation : language).localizationIdentifier else {
            return nil
        }
        for candidate in [identifier, identifier.lowercased()] {
            if let path = AppResourceBundle.bundle.path(forResource: candidate, ofType: "lproj"),
               let bundle = Bundle(path: path) {
                return bundle
            }
        }
        return nil
    }
}
