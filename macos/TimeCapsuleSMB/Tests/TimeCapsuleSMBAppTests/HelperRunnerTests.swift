import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class HelperRunnerTests: XCTestCase {
    func testRunnerStreamsEventsFromHelper() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            cat >/dev/null
            echo '{"schema_version":1,"request_id":"req","type":"stage","operation":"paths","stage":"start"}'
            echo '{"schema_version":1,"request_id":"req","type":"result","operation":"paths","ok":true,"payload":{"ok":true}}'
            """
        )
        let runner = HelperRunner(locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default))
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: helper.path, operation: "paths", params: [:]) {
            recorder.append($0)
        }

        let events = recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(events.map(\.type), ["stage", "result"])
        XCTAssertEqual(events.last?.ok, true)
    }

    func testRunnerSynthesizesErrorWhenHelperHasNoTerminalEvent() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            cat >/dev/null
            echo '{"type":"log","operation":"doctor","level":"info","message":"working"}'
            echo 'stderr detail' >&2
            """
        )
        let runner = HelperRunner(locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default))
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: helper.path, operation: "doctor", params: [:]) {
            recorder.append($0)
        }

        let events = recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(events.last?.type, "error")
        XCTAssertEqual(events.last?.code, "missing_terminal_event")
        XCTAssertEqual(events.last?.debug, .object(["stderr": .string("stderr detail\n")]))
    }

    func testRunnerReportsMissingHelper() async {
        let locator = HelperLocator(environment: [:], currentDirectory: URL(fileURLWithPath: NSTemporaryDirectory()), bundle: .main, fileManager: .default)
        let runner = HelperRunner(locator: locator)
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: "/missing/tcapsule", operation: "paths", params: [:]) {
            recorder.append($0)
        }

        XCTAssertEqual(result.exitCode, 1)
        XCTAssertEqual(recorder.events.last?.type, "error")
        XCTAssertEqual(recorder.events.last?.code, "helper_not_found")
    }

    private func makeHelper(in directory: URL, body: String) throws -> URL {
        let helper = directory.appendingPathComponent("tcapsule")
        try "#!/bin/sh\n\(body)\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)
        return helper
    }
}

private final class EventRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [BackendEvent] = []

    var events: [BackendEvent] {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func append(_ event: BackendEvent) {
        lock.lock()
        storage.append(event)
        lock.unlock()
    }
}
