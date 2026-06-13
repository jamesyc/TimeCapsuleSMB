import Foundation
import Security

protocol KeychainClient: AnyObject {
    func copyMatching(_ query: [String: Any], result: inout CFTypeRef?) -> OSStatus
    func add(_ query: [String: Any]) -> OSStatus
    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus
    func delete(_ query: [String: Any]) -> OSStatus
    func message(for status: OSStatus) -> String?
}

final class SystemKeychainClient: KeychainClient {
    func copyMatching(_ query: [String: Any], result: inout CFTypeRef?) -> OSStatus {
        SecItemCopyMatching(query as CFDictionary, &result)
    }

    func add(_ query: [String: Any]) -> OSStatus {
        SecItemAdd(query as CFDictionary, nil)
    }

    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
    }

    func delete(_ query: [String: Any]) -> OSStatus {
        SecItemDelete(query as CFDictionary)
    }

    func message(for status: OSStatus) -> String? {
        SecCopyErrorMessageString(status, nil) as String?
    }
}

enum PasswordStoreError: Error, Equatable, LocalizedError {
    case missing
    case unavailable(String)

    var errorDescription: String? {
        switch self {
        case .missing:
            return L10n.string("password.error.missing")
        case .unavailable(let message):
            return message
        }
    }
}

enum CredentialAvailability: Equatable {
    case available
    case missing
    case authenticationRequired
    case unavailable(String)
}

protocol PasswordStore: AnyObject {
    func password(for account: String) throws -> String
    func save(_ password: String, for account: String) throws
    func deletePassword(for account: String) throws
    func credentialAvailability(for account: String) -> CredentialAvailability
}

final class KeychainPasswordStore: PasswordStore {
    static let service = "TimeCapsuleSMB.DevicePassword"

    private let service: String
    private let accessibility: CFString
    private let keychainClient: KeychainClient
    private var cachedPasswords: [String: String] = [:]

    init(
        service: String = KeychainPasswordStore.service,
        accessibility: CFString = kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        keychainClient: KeychainClient = SystemKeychainClient()
    ) {
        self.service = service
        self.accessibility = accessibility
        self.keychainClient = keychainClient
    }

    func password(for account: String) throws -> String {
        if let cachedPassword = cachedPasswords[account] {
            return cachedPassword
        }
        var query = baseQuery(account: account)
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecReturnData as String] = true

        var result: CFTypeRef?
        let status = keychainClient.copyMatching(query, result: &result)
        if status == errSecItemNotFound {
            throw PasswordStoreError.missing
        }
        guard status == errSecSuccess else {
            throw PasswordStoreError.unavailable(message(for: status))
        }
        guard let data = result as? Data,
              let password = String(data: data, encoding: .utf8) else {
            throw PasswordStoreError.unavailable(L10n.string("password.error.unreadable_keychain_item"))
        }
        cachedPasswords[account] = password
        return password
    }

    func save(_ password: String, for account: String) throws {
        let data = Data(password.utf8)
        var query = baseQuery(account: account)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: accessibility
        ]
        let status = keychainClient.update(query, attributes: attributes)
        if status == errSecSuccess {
            cachedPasswords[account] = password
            return
        }
        if status != errSecItemNotFound {
            throw PasswordStoreError.unavailable(message(for: status))
        }
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = accessibility
        let addStatus = keychainClient.add(query)
        guard addStatus == errSecSuccess else {
            throw PasswordStoreError.unavailable(message(for: addStatus))
        }
        cachedPasswords[account] = password
    }

    func deletePassword(for account: String) throws {
        let status = keychainClient.delete(baseQuery(account: account))
        if status == errSecSuccess || status == errSecItemNotFound {
            cachedPasswords.removeValue(forKey: account)
            return
        }
        throw PasswordStoreError.unavailable(message(for: status))
    }

    func credentialAvailability(for account: String) -> CredentialAvailability {
        var query = baseQuery(account: account)
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecReturnAttributes as String] = true
        query[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUISkip

        var result: CFTypeRef?
        let status = keychainClient.copyMatching(query, result: &result)
        if status == errSecSuccess {
            return .available
        }
        if status == errSecItemNotFound {
            return .missing
        }
        if status == errSecInteractionNotAllowed {
            return .authenticationRequired
        }
        return .unavailable(message(for: status))
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }

    private func message(for status: OSStatus) -> String {
        if let message = keychainClient.message(for: status) {
            return message
        }
        return L10n.format("password.error.keychain_status", status)
    }
}
