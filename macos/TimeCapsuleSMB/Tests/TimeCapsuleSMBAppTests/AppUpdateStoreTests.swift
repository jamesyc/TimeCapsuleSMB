import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppUpdateStoreTests: XCTestCase {
    func testCheckNowMarksCurrentVersion() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: false))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        var settings = AppSettings.default
        settings.versionCheckURL = "https://example.invalid/version.json"

        store.checkNow(settings: settings)

        try await waitUntilStoreState { store.state == .current }
        XCTAssertEqual(runner.calls.map(\.operation), ["version-check"])
        XCTAssertEqual(runner.calls.first?.params["url"], .string("https://example.invalid/version.json"))
        XCTAssertEqual(store.payload?.source, "network")
    }

    func testCheckNowSurfacesUnavailableMetadataSeparatelyFromCurrentVersion() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: false, source: "unavailable"))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.state == .unavailable }
        XCTAssertEqual(store.payload?.summary, "version metadata is unavailable.")
    }

    func testCheckNowBlocksConcurrentUpdateChecks() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [], delayNanoseconds: 250_000_000)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)

        store.checkNow(settings: .default)
        store.checkNow(settings: .default)

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.error?.code, "operation_rejected")
    }

    private func versionCheckPayload(shouldBlock: Bool, source: String = "network") -> JSONValue {
        .object([
            "schema_version": .number(1),
            "should_block": .bool(shouldBlock),
            "checked_url": .string("https://example.invalid/version.json"),
            "message": .string(shouldBlock ? "Please update." : "Current."),
            "download_url": .string("https://example.invalid/download"),
            "local_version_code": .number(20125),
            "current_version": .number(20125),
            "min_supported_version": .number(20000),
            "latest_tag": .string("v2.1.4"),
            "source": .string(source),
            "summary": .string(source == "unavailable" ? "version metadata is unavailable." : "TimeCapsuleSMB is up to date.")
        ])
    }
}
