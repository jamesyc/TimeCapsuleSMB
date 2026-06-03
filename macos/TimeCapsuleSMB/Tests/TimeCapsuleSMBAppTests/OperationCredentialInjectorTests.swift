import XCTest
@testable import TimeCapsuleSMBApp

final class OperationCredentialInjectorTests: XCTestCase {
    func testNilPasswordLeavesParamsUnchanged() {
        let params: [String: JSONValue] = ["dry_run": .bool(true)]

        XCTAssertEqual(OperationCredentialInjector.injectingPassword(nil, into: params), params)
    }

    func testEmptyPasswordLeavesParamsUnchanged() {
        let params: [String: JSONValue] = ["dry_run": .bool(true)]

        XCTAssertEqual(OperationCredentialInjector.injectingPassword(" \n ", into: params), params)
    }

    func testNonEmptyPasswordAddsCredentialsWithoutTrimmingValue() {
        let params: [String: JSONValue] = ["dry_run": .bool(true)]

        let injected = OperationCredentialInjector.injectingPassword(" secret ", into: params)

        XCTAssertEqual(injected["dry_run"], .bool(true))
        XCTAssertEqual(injected["credentials"], .object(["password": .string(" secret ")]))
    }

    func testExistingCredentialsArePreserved() {
        let params: [String: JSONValue] = [
            "credentials": .object(["password": .string("existing")])
        ]

        let injected = OperationCredentialInjector.injectingPassword("replacement", into: params)

        XCTAssertEqual(injected["credentials"], .object(["password": .string("existing")]))
    }
}
