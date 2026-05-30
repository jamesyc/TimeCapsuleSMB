import Foundation

enum OperationCredentialInjector {
    static func injectingPassword(
        _ password: String?,
        into params: [String: JSONValue]
    ) -> [String: JSONValue] {
        guard let password,
              !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              params["credentials"] == nil else {
            return params
        }

        var updated = params
        updated["credentials"] = .object(["password": .string(password)])
        return updated
    }
}
