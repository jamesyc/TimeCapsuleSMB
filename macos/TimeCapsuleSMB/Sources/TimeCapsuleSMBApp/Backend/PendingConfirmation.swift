import Foundation

struct PendingConfirmation: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let actionTitle: String
    let operation: String
    let params: [String: JSONValue]
    let context: DeviceRuntimeContext?

    init?(
        confirmationEvent event: BackendEvent,
        originalParams: [String: JSONValue],
        context: DeviceRuntimeContext? = nil
    ) {
        guard
            event.type == "error",
            event.code == "confirmation_required",
            case .object(let details)? = event.details,
            case .string(let confirmationId)? = details["confirmation_id"]
        else {
            return nil
        }

        let presentation = ConfirmationPresentation(details: details)
        self.title = presentation?.title
            ?? Self.detailString(details, "title")
            ?? L10n.string("confirm.backend.title")
        self.message = presentation?.message
            ?? Self.detailString(details, "message")
            ?? event.message
            ?? L10n.string("confirm.backend.message")
        self.actionTitle = presentation?.actionTitle
            ?? Self.detailString(details, "action_title")
            ?? L10n.string("action.confirm")
        self.operation = event.operation
        var confirmedParams = originalParams
        confirmedParams["confirmation_id"] = .string(confirmationId)
        self.params = confirmedParams
        self.context = context
    }

    private static func detailString(_ details: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = details[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }
}

private struct ConfirmationPresentation {
    let title: String
    let message: String
    let actionTitle: String

    init?(details: [String: JSONValue]) {
        guard
            let presentationKey = Self.detailString(details, "presentation_id"),
            let title = Self.localizedString("confirm.\(presentationKey).title"),
            let message = Self.localizedMessage(for: presentationKey, details: details)
        else {
            return nil
        }
        self.title = title
        self.message = message
        self.actionTitle = Self.localizedString("confirm.\(presentationKey).action")
            ?? Self.detailString(details, "action_title")
            ?? L10n.string("action.confirm")
    }

    private static func localizedMessage(for presentationKey: String, details: [String: JSONValue]) -> String? {
        let messageKey = "confirm.\(presentationKey).message"
        guard let template = localizedString(messageKey) else {
            return nil
        }
        let values = detailObject(details, "presentation_values")
        switch presentationKey {
        case "configure.enable_ssh_reboot",
             "deploy.activate_now",
             "deploy.netbsd4",
             "deploy.netbsd4_no_wait",
             "deploy.no_reboot",
             "deploy.reboot",
             "deploy.reboot_no_wait":
            guard let deviceName = stringValue(values, "device_name") else {
                return nil
            }
            return format(template, deviceName)
        case "repair_xattrs":
            guard let path = stringValue(values, "path") else {
                return nil
            }
            return format(template, path)
        default:
            return template
        }
    }

    private static func localizedString(_ key: String) -> String? {
        let value = L10n.string(key)
        return value == key ? nil : value
    }

    private static func format(_ template: String, _ arguments: CVarArg...) -> String {
        String(format: template, locale: Locale.current, arguments: arguments)
    }

    private static func detailString(_ details: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = details[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }

    private static func detailObject(_ details: [String: JSONValue], _ key: String) -> [String: JSONValue] {
        guard case .object(let values)? = details[key] else {
            return [:]
        }
        return values
    }

    private static func stringValue(_ values: [String: JSONValue], _ key: String) -> String? {
        guard case .string(let value)? = values[key] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : value
    }
}
