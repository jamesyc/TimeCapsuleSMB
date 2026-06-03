import Foundation

struct RecoveryGuidancePresentation: Equatable {
    let title: String
    let errorMessage: String
    let detail: String?
    let steps: [String]

    init(error: BackendErrorViewModel) {
        let localizedRecovery = BackendRecoveryLocalization.localized(error.recovery)
        self.title = localizedRecovery?.title ?? error.code
        self.errorMessage = error.message
        self.detail = Self.uniqueDetail(localizedRecovery?.message, title: title, errorMessage: error.message)
        self.steps = localizedRecovery?.actions ?? []
    }

    var hasStructuredGuidance: Bool {
        detail != nil || !steps.isEmpty
    }

    private static func uniqueDetail(_ detail: String?, title: String, errorMessage: String) -> String? {
        guard let detail else {
            return nil
        }
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        let normalized = trimmed.normalizedRecoveryPresentationText
        guard normalized != title.normalizedRecoveryPresentationText,
              normalized != errorMessage.normalizedRecoveryPresentationText else {
            return nil
        }
        return trimmed
    }
}

private struct LocalizedBackendRecovery: Equatable {
    let title: String
    let message: String?
    let actions: [String]
}

private enum BackendRecoveryLocalization {
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

private extension String {
    var normalizedRecoveryPresentationText: String {
        trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }
}
