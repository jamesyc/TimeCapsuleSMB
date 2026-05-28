import Foundation
import Security

protocol SMBAccountResolving {
    func account(for profile: DeviceProfile) -> String?
}

struct KeychainSMBAccountResolver: SMBAccountResolving {
    private let keychainClient: KeychainClient

    init(keychainClient: KeychainClient = SystemKeychainClient()) {
        self.keychainClient = keychainClient
    }

    func account(for profile: DeviceProfile) -> String? {
        for server in serverCandidates(for: profile) {
            if let account = account(forServer: server) {
                return account
            }
        }
        return nil
    }

    private func serverCandidates(for profile: DeviceProfile) -> [String] {
        var candidates = SMBAddressPolicy.credentialServerCandidates(for: profile)
        for server in Array(candidates) {
            let lowercased = server.lowercased()
            if lowercased != server && !candidates.contains(lowercased) {
                candidates.append(lowercased)
            }
        }
        return candidates
    }

    private func account(forServer server: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassInternetPassword,
            kSecAttrProtocol as String: kSecAttrProtocolSMB,
            kSecAttrServer as String: server,
            kSecReturnAttributes as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]

        var result: CFTypeRef?
        let status = keychainClient.copyMatching(query, result: &result)
        guard status == errSecSuccess,
              let attributes = result as? [String: Any],
              let account = attributes[kSecAttrAccount as String] as? String else {
            return nil
        }
        let trimmed = account.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
