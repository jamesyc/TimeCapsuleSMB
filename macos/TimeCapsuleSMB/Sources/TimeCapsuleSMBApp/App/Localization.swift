import Foundation

enum L10n {
    private static let lock = NSLock()
    private static var selectedLanguage: AppLanguage = .system

    static var currentLanguage: AppLanguage {
        lock.lock()
        defer { lock.unlock() }
        return selectedLanguage
    }

    static func apply(language: AppLanguage) {
        lock.lock()
        selectedLanguage = language
        lock.unlock()
    }

    static func string(_ key: String) -> String {
        string(key, language: currentLanguage)
    }

    static func format(_ key: String, _ arguments: CVarArg...) -> String {
        let language = currentLanguage
        return String(format: string(key, language: language), locale: language.locale, arguments: arguments)
    }

    static func string(_ key: String, language: AppLanguage) -> String {
        let fallback = AppResourceBundle.bundle.localizedString(forKey: key, value: key, table: nil)
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
        guard let identifier = language.localizationIdentifier else {
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
