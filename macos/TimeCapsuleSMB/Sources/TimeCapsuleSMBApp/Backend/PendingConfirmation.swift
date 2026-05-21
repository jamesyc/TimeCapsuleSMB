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

        self.title = Self.detailString(details, "title") ?? L10n.string("confirm.backend.title")
        self.message = Self.detailString(details, "message") ?? event.message ?? L10n.string("confirm.backend.message")
        self.actionTitle = Self.detailString(details, "action_title") ?? L10n.string("action.confirm")
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
