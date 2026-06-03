import Security
import XCTest
@testable import TimeCapsuleSMBApp

final class SMBAccountResolverTests: XCTestCase {
    func testFindsSMBAccountForResolvedHostnameWithoutReadingPasswordData() {
        let keychain = AccountLookupKeychainClient(accountsByServer: [
            "AirPort-Time-Capsule.local": "jameschang"
        ])
        let resolver = KeychainSMBAccountResolver(keychainClient: keychain)
        let profile = makeProfile(
            host: "root@192.168.1.72",
            bonjourName: "AirPort Time Capsule",
            hostname: "AirPort-Time-Capsule.local."
        )

        XCTAssertEqual(resolver.account(for: profile), "jameschang")
        XCTAssertEqual(keychain.queries.count, 1)
        XCTAssertEqual(keychain.queries[0][kSecClass as String] as? String, kSecClassInternetPassword as String)
        XCTAssertEqual(keychain.queries[0][kSecAttrProtocol as String] as? String, kSecAttrProtocolSMB as String)
        XCTAssertEqual(keychain.queries[0][kSecReturnAttributes as String] as? Bool, true)
        XCTAssertNil(keychain.queries[0][kSecReturnData as String])
    }

    func testFallsBackToLowercaseServerCandidate() {
        let keychain = AccountLookupKeychainClient(accountsByServer: [
            "jamess-airport-time-capsule.local": "admin"
        ])
        let resolver = KeychainSMBAccountResolver(keychainClient: keychain)
        let profile = makeProfile(
            host: "root@192.168.1.217",
            bonjourName: "James's AirPort Time Capsule",
            hostname: "Jamess-AirPort-Time-Capsule.local."
        )

        XCTAssertEqual(resolver.account(for: profile), "admin")
        XCTAssertEqual(keychain.queries.map { $0[kSecAttrServer as String] as? String }, [
            "Jamess-AirPort-Time-Capsule.local",
            "192.168.1.217",
            "jamess-airport-time-capsule.local"
        ])
    }

    func testReturnsNilWhenNoSMBAccountExists() {
        let keychain = AccountLookupKeychainClient(accountsByServer: [:])
        let resolver = KeychainSMBAccountResolver(keychainClient: keychain)
        let profile = makeProfile(host: "root@10.0.0.2", bonjourName: nil, hostname: nil)

        XCTAssertNil(resolver.account(for: profile))
    }

    private func makeProfile(
        host: String,
        bonjourName: String?,
        hostname: String?
    ) -> DeviceProfile {
        DeviceProfile(
            id: "device-one",
            displayName: bonjourName ?? "Office Capsule",
            host: host,
            bonjourName: bonjourName,
            bonjourFullname: bonjourName.map { "\($0)._airport._tcp.local." },
            hostname: hostname,
            addresses: [],
            syap: nil,
            model: nil,
            osName: nil,
            osRelease: nil,
            arch: nil,
            elfEndianness: nil,
            payloadFamily: nil,
            deviceGeneration: nil,
            configPath: "/tmp/device-one/.env",
            keychainAccount: "device-one",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastCheckup: nil,
            lastDeployState: nil,
            settings: .default,
            passwordState: .available
        )
    }
}

private final class AccountLookupKeychainClient: KeychainClient {
    let accountsByServer: [String: String]
    private(set) var queries: [[String: Any]] = []

    init(accountsByServer: [String: String]) {
        self.accountsByServer = accountsByServer
    }

    func copyMatching(_ query: [String: Any], result: inout CFTypeRef?) -> OSStatus {
        queries.append(query)
        guard let server = query[kSecAttrServer as String] as? String,
              let account = accountsByServer[server] else {
            return errSecItemNotFound
        }
        result = [kSecAttrAccount as String: account] as CFDictionary
        return errSecSuccess
    }

    func add(_ query: [String: Any]) -> OSStatus {
        errSecSuccess
    }

    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        errSecSuccess
    }

    func delete(_ query: [String: Any]) -> OSStatus {
        errSecSuccess
    }

    func message(for status: OSStatus) -> String? {
        "status \(status)"
    }
}
