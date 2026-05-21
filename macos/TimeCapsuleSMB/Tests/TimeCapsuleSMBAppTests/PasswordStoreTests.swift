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
}
