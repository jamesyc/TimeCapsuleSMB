import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class BackendEventTests: XCTestCase {
    func testBackendEventDecodesContractFields() throws {
        let data = """
        {"schema_version":1,"request_id":"req-1","type":"error","operation":"deploy","code":"remote_error","message":"failed","debug":{"stderr":"detail"}}
        """.data(using: .utf8)!

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.schemaVersion, 1)
        XCTAssertEqual(event.requestId, "req-1")
        XCTAssertEqual(event.type, "error")
        XCTAssertEqual(event.operation, "deploy")
        XCTAssertEqual(event.code, "remote_error")
        XCTAssertEqual(event.message, "failed")
        XCTAssertEqual(event.debug, .object(["stderr": .string("detail")]))
    }

    func testJSONValueRoundTripsNestedObjects() throws {
        let value = JSONValue.object([
            "operation": .string("paths"),
            "params": .object([
                "dry_run": .bool(true),
                "mount_wait": .number(30),
                "items": .array([.string("one"), .null])
            ])
        ])

        let data = try JSONEncoder().encode(value)
        let decoded = try JSONDecoder().decode(JSONValue.self, from: data)

        XCTAssertEqual(decoded, value)
    }
}
