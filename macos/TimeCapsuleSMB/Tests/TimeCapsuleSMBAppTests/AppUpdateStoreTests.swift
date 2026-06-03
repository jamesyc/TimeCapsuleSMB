import Combine
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

    func testPublishesWhenBackendFinishesAfterVersionCheckResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "version-check", ok: true, payload: versionCheckPayload(shouldBlock: false))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        let finishPublished = expectation(description: "AppUpdateStore publishes after backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        store.objectWillChange
            .sink { [weak store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.state == .current,
                          store?.isChecking == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.state == .current }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(store.isChecking)
        _ = cancellables
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
        XCTAssertEqual(store.payload?.summary, "Version metadata is unavailable.")
        XCTAssertEqual(store.payload?.localizedSummary, "Version metadata is unavailable.")
    }

    func testCheckNowMarksOptionalUpdateAvailable() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "version-check",
                    ok: true,
                    payload: versionCheckPayload(
                        shouldBlock: false,
                        updateAvailable: true,
                        localVersionCode: 20124,
                        currentVersion: 20125
                    )
                )
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.state == .updateAvailable }
        XCTAssertEqual(store.payload?.localizedSummary, "Update available.")
    }

    func testCheckNowBlocksConcurrentUpdateChecks() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)

        store.checkNow(settings: .default)
        try await waitUntilStoreState { runner.calls.count == 1 && store.isChecking }
        store.checkNow(settings: .default)

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.error?.code, "operation_rejected")
        runner.finishAll()
    }

    private func versionCheckPayload(
        shouldBlock: Bool,
        updateAvailable: Bool = false,
        source: String = "network",
        localVersionCode: Int = 20125,
        currentVersion: Int = 20125
    ) -> JSONValue {
        let summary: String
        if source == "unavailable" {
            summary = "Version metadata is unavailable."
        } else if shouldBlock {
            summary = "Update required."
        } else if updateAvailable {
            summary = "Update available."
        } else {
            summary = "TimeCapsuleSMB is up to date."
        }
        return .object([
            "schema_version": .number(1),
            "should_block": .bool(shouldBlock),
            "update_available": .bool(updateAvailable),
            "checked_url": .string("https://example.invalid/version.json"),
            "message": .string(shouldBlock ? "Please update." : "Current."),
            "download_url": .string("https://example.invalid/download"),
            "local_version_code": .number(Double(localVersionCode)),
            "current_version": .number(Double(currentVersion)),
            "min_supported_version": .number(20000),
            "latest_tag": .string("v2.1.4"),
            "source": .string(source),
            "summary": .string(summary)
        ])
    }
}
