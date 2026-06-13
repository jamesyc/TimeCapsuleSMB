import Security
import XCTest
@testable import TimeCapsuleSMBApp

final class PasswordStoreTests: XCTestCase {
    func testSaveReadUpdateAndDeletePassword() throws {
        let store = InMemoryPasswordStore()

        try store.save("first", for: "device")
        XCTAssertEqual(try store.password(for: "device"), "first")
        XCTAssertEqual(store.state(for: "device"), .available)

        try store.save("second", for: "device")
        XCTAssertEqual(try store.password(for: "device"), "second")

        try store.deletePassword(for: "device")
        XCTAssertThrowsError(try store.password(for: "device")) { error in
            XCTAssertEqual(error as? PasswordStoreError, .missing)
        }
        XCTAssertEqual(store.state(for: "device"), .missing)
    }

    func testInvalidAndUnavailableStates() throws {
        let store = InMemoryPasswordStore(passwords: ["device": "pw"])

        store.markInvalid(account: "device")
        XCTAssertEqual(store.state(for: "device"), .invalid)

        store.readFailure = .read
        XCTAssertEqual(store.state(for: "device"), .keychainUnavailable)
        XCTAssertThrowsError(try store.password(for: "device")) { error in
            guard case PasswordStoreError.unavailable = error else {
                return XCTFail("unexpected error \(error)")
            }
        }
    }

    func testSaveAndDeleteFailuresSurfaceUnavailable() {
        let store = InMemoryPasswordStore()
        store.saveFailure = .save

        XCTAssertThrowsError(try store.save("pw", for: "device")) { error in
            guard case PasswordStoreError.unavailable = error else {
                return XCTFail("unexpected error \(error)")
            }
        }

        store.saveFailure = nil
        store.deleteFailure = .delete
        XCTAssertThrowsError(try store.deletePassword(for: "device")) { error in
            guard case PasswordStoreError.unavailable = error else {
                return XCTFail("unexpected error \(error)")
            }
        }
    }

    func testKeychainStoreAddsPasswordWithWhenUnlockedThisDeviceOnlyAccessibility() throws {
        let keychain = RecordingKeychainClient()
        keychain.updateStatus = errSecItemNotFound
        let store = KeychainPasswordStore(service: "test.service", keychainClient: keychain)

        try store.save("secret", for: "device")

        XCTAssertEqual(keychain.addedQuery?[kSecAttrService as String] as? String, "test.service")
        XCTAssertEqual(keychain.addedQuery?[kSecAttrAccount as String] as? String, "device")
        XCTAssertEqual(keychain.addedQuery?[kSecAttrAccessible as String] as? String, kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String)
        XCTAssertEqual(keychain.addedQuery?[kSecValueData as String] as? Data, Data("secret".utf8))
    }

    func testKeychainStoreMigratesAccessibilityOnPasswordUpdate() throws {
        let keychain = RecordingKeychainClient()
        keychain.updateStatus = errSecSuccess
        let store = KeychainPasswordStore(service: "test.service", keychainClient: keychain)

        try store.save("updated", for: "device")

        XCTAssertNil(keychain.addedQuery)
        XCTAssertEqual(keychain.updatedAttributes?[kSecAttrAccessible as String] as? String, kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String)
        XCTAssertEqual(keychain.updatedAttributes?[kSecValueData as String] as? Data, Data("updated".utf8))
    }

    func testKeychainAvailabilityCheckDoesNotReturnPasswordDataOrShowAuthenticationUI() {
        let keychain = RecordingKeychainClient()
        keychain.copyStatus = errSecSuccess
        keychain.copyResult = [kSecAttrAccount as String: "device"] as CFDictionary
        let store = KeychainPasswordStore(service: "test.service", keychainClient: keychain)

        XCTAssertEqual(store.credentialAvailability(for: "device"), .available)

        XCTAssertEqual(keychain.copiedQuery?[kSecAttrService as String] as? String, "test.service")
        XCTAssertEqual(keychain.copiedQuery?[kSecAttrAccount as String] as? String, "device")
        XCTAssertEqual(keychain.copiedQuery?[kSecReturnAttributes as String] as? Bool, true)
        XCTAssertNil(keychain.copiedQuery?[kSecReturnData as String])
        XCTAssertEqual(keychain.copiedQuery?[kSecUseAuthenticationUI as String] as? String, kSecUseAuthenticationUISkip as String)
    }

    func testKeychainAvailabilityReportsAuthenticationRequiredWithoutPrompting() {
        let keychain = RecordingKeychainClient()
        keychain.copyStatus = errSecInteractionNotAllowed
        let store = KeychainPasswordStore(service: "test.service", keychainClient: keychain)

        XCTAssertEqual(store.credentialAvailability(for: "device"), .authenticationRequired)
        XCTAssertEqual(keychain.copiedQuery?[kSecUseAuthenticationUI as String] as? String, kSecUseAuthenticationUISkip as String)
    }

    func testKeychainPasswordReadIsCachedForSession() throws {
        let keychain = RecordingKeychainClient()
        keychain.copyStatus = errSecSuccess
        keychain.copyResult = Data("secret".utf8) as CFData
        let store = KeychainPasswordStore(service: "test.service", keychainClient: keychain)

        XCTAssertEqual(try store.password(for: "device"), "secret")
        XCTAssertEqual(try store.password(for: "device"), "secret")

        XCTAssertEqual(keychain.copyCount, 1)
    }
}

private final class RecordingKeychainClient: KeychainClient {
    var copyStatus: OSStatus = errSecItemNotFound
    var copyResult: CFTypeRef?
    var addStatus: OSStatus = errSecSuccess
    var updateStatus: OSStatus = errSecItemNotFound
    var deleteStatus: OSStatus = errSecSuccess

    private(set) var copiedQuery: [String: Any]?
    private(set) var addedQuery: [String: Any]?
    private(set) var updatedQuery: [String: Any]?
    private(set) var updatedAttributes: [String: Any]?
    private(set) var deletedQuery: [String: Any]?
    private(set) var copyCount = 0

    func copyMatching(_ query: [String: Any], result: inout CFTypeRef?) -> OSStatus {
        copyCount += 1
        copiedQuery = query
        result = copyResult
        return copyStatus
    }

    func add(_ query: [String: Any]) -> OSStatus {
        addedQuery = query
        return addStatus
    }

    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        updatedQuery = query
        updatedAttributes = attributes
        return updateStatus
    }

    func delete(_ query: [String: Any]) -> OSStatus {
        deletedQuery = query
        return deleteStatus
    }

    func message(for status: OSStatus) -> String? {
        "status \(status)"
    }
}
