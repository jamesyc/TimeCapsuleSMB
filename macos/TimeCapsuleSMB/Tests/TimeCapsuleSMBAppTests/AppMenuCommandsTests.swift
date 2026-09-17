import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppMenuCommandsTests: XCTestCase {
    func testRequestCheckForUpdatesTriggersManualCheck() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: .object([
                    "schema_version": .number(1),
                    "should_block": .bool(false),
                    "update_available": .bool(false),
                    "checked_url": .string("https://example.invalid/version.json"),
                    "message": .string("Current."),
                    "download_url": .string("https://example.invalid/download"),
                    "local_version_code": .number(30001),
                    "current_version": .number(30001),
                    "min_supported_version": .number(20121),
                    "latest_tag": .string("v3.0.0-1"),
                    "source": .string("network"),
                    "summary": .string("TimeCapsuleSMB is up to date."),
                    "release": .null
                ]))
            ])
        ])
        let fixture = try await AppViewFixture(runner: runner)

        AppMenuCommands.requestCheckForUpdates()

        try await waitUntilStoreState { fixture.appStore.appUpdateStore.manualCheckOutcome != nil }
        XCTAssertEqual(runner.calls.last?.operation, "update-check")
        XCTAssertEqual(fixture.appStore.appUpdateStore.manualCheckOutcome, .upToDate(localVersionCode: 30001))
    }

    func testCheckForUpdatesTitleIsLocalized() {
        XCTAssertEqual(AppMenuCommands.checkForUpdatesTitle, "Check for Updates…")
    }
}
