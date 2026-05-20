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
            await recorder.append($0)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(events.map(\.type), ["stage", "result"])
        XCTAssertEqual(events.last?.ok, true)
    }

    func testRunnerWaitsForEventDeliveryBeforeReturning() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            cat >/dev/null
            echo '{"schema_version":1,"request_id":"req","type":"result","operation":"paths","ok":true,"payload":{"ok":true}}'
            """
        )
        let runner = HelperRunner(locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default))
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: helper.path, operation: "paths", params: [:]) { event in
            try? await Task.sleep(nanoseconds: 50_000_000)
            await recorder.append(event)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(events.map(\.type), ["result"])
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
            await recorder.append($0)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(events.last?.type, "error")
        XCTAssertEqual(events.last?.code, "missing_terminal_event")
        XCTAssertEqual(events.last?.message, L10n.string("helper.error.missing_terminal_event"))
        XCTAssertEqual(events.last?.debug, .object(["stderr": .string("stderr detail\n")]))
    }

    func testRunnerDrainsLargeStderrWhileHelperIsRunning() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            i=0
            while [ "$i" -lt 2000 ]; do
                printf '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\\n' >&2
                i=$((i + 1))
            done
            cat >/dev/null
            echo '{"schema_version":1,"request_id":"req","type":"result","operation":"doctor","ok":true,"payload":{"ok":true}}'
            """
        )
        let runner = HelperRunner(locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default))
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: helper.path, operation: "doctor", params: [:]) {
            await recorder.append($0)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(result.stderr.count, 64 * 1024)
        XCTAssertEqual(events.last?.type, "result")
        XCTAssertEqual(events.last?.ok, true)
    }

    func testRunnerDecodesTruncatedUTF8StderrWithReplacementCharacter() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            cat >/dev/null
            printf '\\303\\251' >&2
            """
        )
        let runner = HelperRunner(
            locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default),
            stderrLimit: 1
        )
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: helper.path, operation: "doctor", params: [:]) {
            await recorder.append($0)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 0)
        XCTAssertEqual(result.stderr, "\u{FFFD}")
        XCTAssertEqual(events.last?.code, "missing_terminal_event")
    }

    func testRunnerReportsMissingHelper() async {
        let locator = HelperLocator(environment: [:], currentDirectory: URL(fileURLWithPath: NSTemporaryDirectory()), bundle: .main, fileManager: .default)
        let runner = HelperRunner(locator: locator)
        let recorder = EventRecorder()

        let result = await runner.run(helperPath: "/missing/tcapsule", operation: "paths", params: [:]) {
            await recorder.append($0)
        }

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 1)
        XCTAssertEqual(events.last?.type, "error")
        XCTAssertEqual(events.last?.code, "helper_not_found")
    }

    func testRunnerCancelsLongRunningHelper() async throws {
        let temp = try TemporaryDirectory()
        let helper = try makeHelper(
            in: temp.url,
            body: """
            cat >/dev/null
            while true; do
                sleep 1
            done
            """
        )
        let runner = HelperRunner(locator: HelperLocator(environment: [:], currentDirectory: temp.url, bundle: .main, fileManager: .default))
        let recorder = EventRecorder()

        let task = Task {
            await runner.run(helperPath: helper.path, operation: "doctor", params: [:]) {
                await recorder.append($0)
            }
        }
        try await Task.sleep(nanoseconds: 100_000_000)
        task.cancel()
        let result = await task.value

        let events = await recorder.events
        XCTAssertEqual(result.exitCode, 130)
        XCTAssertEqual(events.last?.type, "error")
        XCTAssertEqual(events.last?.code, "cancelled")
        XCTAssertEqual(events.last?.message, L10n.string("helper.error.cancelled"))
    }

    private func makeHelper(in directory: URL, body: String) throws -> URL {
        let helper = directory.appendingPathComponent("tcapsule")
        try "#!/bin/sh\n\(body)\n".write(to: helper, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: helper.path)
        return helper
    }
}

private actor EventRecorder {
    private var storage: [BackendEvent] = []

    var events: [BackendEvent] {
        storage
    }

    func append(_ event: BackendEvent) {
        storage.append(event)
    }
}
