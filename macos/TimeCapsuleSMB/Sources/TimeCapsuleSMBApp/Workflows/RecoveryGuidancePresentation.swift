import Foundation

/// An error box: guidance in the app's language, then the helper's own text,
/// which stays English so every report reads the same.
struct RecoveryGuidancePresentation: Equatable {
    let title: String
    let message: String?
    let steps: [String]
    let details: String?

    init(error: BackendErrorViewModel) {
        let localizedRecovery = BackendRecoveryLocalization.localized(error.recovery)
        let title = localizedRecovery?.title ?? error.message
        let message = localizedRecovery?.message.flatMap { message in
            message.isEmpty || message.lowercased() == title.lowercased() ? nil : message
        }
        let steps = localizedRecovery?.actions ?? []
        self.title = title
        self.message = message
        self.steps = steps
        self.details = Self.details(error.diagnosticMessage, shown: [title, message ?? ""] + steps)
    }

    /// The helper's text, unless the box already says it: an English box often
    /// repeats it in other words, and nothing is lost by leaving it out then.
    private static func details(_ text: String?, shown: [String]) -> String? {
        guard let text = text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            return nil
        }
        let textWords = words(text)
        let shownWords = words(shown.joined(separator: " "))
        let repeated = textWords.filter(shownWords.contains).count
        return Double(repeated) >= 0.8 * Double(textWords.count) ? nil : text
    }

    private static func words(_ text: String) -> Set<String> {
        Set(text.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init))
    }
}

struct LocalizedBackendRecovery: Equatable {
    let title: String
    let message: String?
    let actions: [String]
}

enum BackendRecoveryLocalization {
    static func localized(_ recovery: BackendRecoveryPayload?) -> LocalizedBackendRecovery? {
        guard let recovery else {
            return nil
        }
        guard let localizationKey = recovery.localizationKey else {
            return LocalizedBackendRecovery(
                title: recovery.title,
                message: recovery.message,
                actions: recovery.actions
            )
        }
        let keyPrefix = "backend.recovery.\(localizationKey)"
        let localizedMessage = localizedStringIfPresent("\(keyPrefix).message")
        return LocalizedBackendRecovery(
            title: localizedStringIfPresent("\(keyPrefix).title") ?? recovery.title,
            message: formatted(localizedMessage, values: recovery.localizationValues) ?? recovery.message,
            actions: localizedActions(keyPrefix: keyPrefix, fallback: recovery.actions)
        )
    }

    private static func localizedActions(keyPrefix: String, fallback: [String]) -> [String] {
        fallback.enumerated().map { index, action in
            localizedStringIfPresent("\(keyPrefix).action.\(index + 1)") ?? action
        }
    }

    private static func localizedStringIfPresent(_ key: String) -> String? {
        let value = L10n.string(key)
        return value == key ? nil : value
    }

    private static func formatted(_ template: String?, values: [String: String]) -> String? {
        guard let template else {
            return nil
        }
        guard template.contains("%@") else {
            return template
        }
        guard let deviceName = values["device_name"] else {
            return nil
        }
        return String(format: template, locale: L10n.currentLanguage.locale, deviceName)
    }
}
