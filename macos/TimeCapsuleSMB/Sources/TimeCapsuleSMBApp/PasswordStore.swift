import Foundation
import Security

enum PasswordStoreError: Error, Equatable, LocalizedError {
    case missing
    case unavailable(String)

    var errorDescription: String? {
        switch self {
        case .missing:
            return "Password is missing."
        case .unavailable(let message):
            return message
        }
    }
}

protocol PasswordStore: AnyObject {
    func password(for account: String) throws -> String
    func save(_ password: String, for account: String) throws
    func deletePassword(for account: String) throws
    func state(for account: String) -> DevicePasswordState
}

final class KeychainPasswordStore: PasswordStore {
    static let service = "TimeCapsuleSMB.DevicePassword"

    private let service: String

    init(service: String = KeychainPasswordStore.service) {
        self.service = service
    }

    func password(for account: String) throws -> String {
        var query = baseQuery(account: account)
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecReturnData as String] = true

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            throw PasswordStoreError.missing
        }
        guard status == errSecSuccess else {
            throw PasswordStoreError.unavailable(message(for: status))
        }
        guard let data = result as? Data,
              let password = String(data: data, encoding: .utf8) else {
            throw PasswordStoreError.unavailable("Keychain returned an unreadable password.")
        }
        return password
    }

    func save(_ password: String, for account: String) throws {
        let data = Data(password.utf8)
        var query = baseQuery(account: account)
        let attributes = [kSecValueData as String: data]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecSuccess {
            return
        }
        if status != errSecItemNotFound {
            throw PasswordStoreError.unavailable(message(for: status))
        }
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let addStatus = SecItemAdd(query as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw PasswordStoreError.unavailable(message(for: addStatus))
        }
    }

    func deletePassword(for account: String) throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        if status == errSecSuccess || status == errSecItemNotFound {
            return
        }
        throw PasswordStoreError.unavailable(message(for: status))
    }

    func state(for account: String) -> DevicePasswordState {
        do {
            _ = try password(for: account)
            return .available
        } catch PasswordStoreError.missing {
            return .missing
        } catch {
            return .keychainUnavailable
        }
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }

    private func message(for status: OSStatus) -> String {
        if let message = SecCopyErrorMessageString(status, nil) as String? {
            return message
        }
        return "Keychain error \(status)."
    }
}

final class InMemoryPasswordStore: PasswordStore {
    enum Failure: Error {
        case read
        case save
        case delete
    }

    var readFailure: Failure?
    var saveFailure: Failure?
    var deleteFailure: Failure?

    private var passwords: [String: String]
    private var invalidAccounts: Set<String>

    init(passwords: [String: String] = [:], invalidAccounts: Set<String> = []) {
        self.passwords = passwords
        self.invalidAccounts = invalidAccounts
    }

    func password(for account: String) throws -> String {
        if readFailure != nil {
            throw PasswordStoreError.unavailable("In-memory password store read failed.")
        }
        guard let password = passwords[account] else {
            throw PasswordStoreError.missing
        }
        return password
    }

    func save(_ password: String, for account: String) throws {
        if saveFailure != nil {
            throw PasswordStoreError.unavailable("In-memory password store save failed.")
        }
        passwords[account] = password
        invalidAccounts.remove(account)
    }

    func deletePassword(for account: String) throws {
        if deleteFailure != nil {
            throw PasswordStoreError.unavailable("In-memory password store delete failed.")
        }
        passwords.removeValue(forKey: account)
        invalidAccounts.remove(account)
    }

    func markInvalid(account: String) {
        invalidAccounts.insert(account)
    }

    func state(for account: String) -> DevicePasswordState {
        if readFailure != nil {
            return .keychainUnavailable
        }
        if invalidAccounts.contains(account) {
            return .invalid
        }
        return passwords[account] == nil ? .missing : .available
    }
}
