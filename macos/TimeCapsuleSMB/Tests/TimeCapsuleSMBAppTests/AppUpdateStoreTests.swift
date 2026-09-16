import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppUpdateStoreTests: XCTestCase {
    func testCheckNowMarksCurrentVersion() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: updateCheckPayload(shouldBlock: false))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        store.markUIReady()
        var settings = AppSettings.default
        settings.versionCheckURL = "https://example.invalid/version.json"
        settings.releaseInfoURL = "https://example.invalid/latest"

        store.checkNow(settings: settings)

        try await waitUntilStoreState { store.state == .current }
        XCTAssertEqual(runner.calls.map(\.operation), ["update-check"])
        XCTAssertEqual(runner.calls.first?.params["url"], .string("https://example.invalid/version.json"))
        XCTAssertEqual(runner.calls.first?.params["release_url"], .string("https://example.invalid/latest"))
        XCTAssertEqual(store.payload?.source, "network")
        XCTAssertNil(store.promptedRelease)
        XCTAssertNil(store.manualCheckOutcome, "automatic checks do not raise the up-to-date alert")
    }

    func testPublishesWhenBackendFinishesAfterUpdateCheckResult() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: updateCheckPayload(shouldBlock: false))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        store.markUIReady()
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
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: updateCheckPayload(shouldBlock: false, source: "unavailable"))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        store.markUIReady()

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.state == .unavailable }
        XCTAssertEqual(store.payload?.summary, "Version metadata is unavailable.")
        XCTAssertEqual(store.payload?.localizedSummary, "Version metadata is unavailable.")
    }

    func testCheckNowMarksOptionalUpdateAvailableAndPrompts() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "update-check",
                    ok: true,
                    payload: updateCheckPayload(
                        shouldBlock: false,
                        updateAvailable: true,
                        localVersionCode: 30001,
                        currentVersion: 30002
                    )
                )
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        store.markUIReady()

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.state == .updateAvailable }
        XCTAssertEqual(store.payload?.localizedSummary, "Update available.")
        let prompt = try XCTUnwrap(store.promptedRelease)
        XCTAssertEqual(prompt.versionCode, 30002)
        XCTAssertEqual(prompt.title, "v3.0.1")
        XCTAssertEqual(prompt.tag, "v3.0.1")
        XCTAssertEqual(prompt.notes, "## Changes\n- one")
        XCTAssertEqual(prompt.htmlURL, URL(string: "https://example.invalid/rel"))
        XCTAssertEqual(prompt.asset?.sha256, "ab")
        XCTAssertFalse(prompt.isRequired)
    }

    func testPromptFallsBackToVersionMetadataWithoutRelease() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "update-check",
                    ok: true,
                    payload: updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002, includeRelease: false)
                )
            ])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default)

        try await waitUntilStoreState { store.promptedRelease != nil }
        let prompt = try XCTUnwrap(store.promptedRelease)
        XCTAssertEqual(prompt.title, "v3.0.1")
        XCTAssertEqual(prompt.notes, "")
        XCTAssertEqual(prompt.htmlURL, URL(string: "https://example.invalid/download"))
        XCTAssertNil(prompt.asset)
    }

    func testRemindLaterSuppressesPromptForSession() async throws {
        let payload = updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)]),
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default)
        try await waitUntilStoreState { store.promptedRelease != nil }
        store.remindLater()
        XCTAssertNil(store.promptedRelease)

        try await waitUntilStoreState { !store.isChecking }
        store.checkNow(settings: .default)
        try await waitUntilStoreState { runner.calls.count == 2 && store.state == .updateAvailable && !store.isChecking }
        XCTAssertNil(store.promptedRelease)
    }

    func testSkippedVersionDoesNotPromptAutomaticallyButManualDoes() async throws {
        let payload = updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)]),
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()
        var settings = AppSettings.default
        settings.skippedUpdateVersionCode = 30002

        store.checkNow(settings: settings)
        try await waitUntilStoreState { store.state == .updateAvailable && !store.isChecking }
        XCTAssertNil(store.promptedRelease)

        store.checkNow(settings: settings, manual: true)
        try await waitUntilStoreState { store.promptedRelease != nil }
        XCTAssertNil(store.manualCheckOutcome)
        XCTAssertEqual(store.skipVersion(), 30002)
        XCTAssertNil(store.promptedRelease)
    }

    func testRequiredUpdateIgnoresSkipAndCannotBeSkipped() async throws {
        let payload = updateCheckPayload(shouldBlock: true, updateAvailable: true, currentVersion: 30002)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()
        var settings = AppSettings.default
        settings.skippedUpdateVersionCode = 30002

        store.checkNow(settings: settings)

        try await waitUntilStoreState { store.promptedRelease != nil }
        XCTAssertEqual(store.promptedRelease?.isRequired, true)
        XCTAssertNil(store.skipVersion())
        XCTAssertNotNil(store.promptedRelease)
    }

    func testManualCheckReportsUpToDate() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: updateCheckPayload(shouldBlock: false, localVersionCode: 30001, currentVersion: 30001))
            ])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default, manual: true)

        try await waitUntilStoreState { store.manualCheckOutcome != nil }
        XCTAssertEqual(store.manualCheckOutcome, .upToDate(localVersionCode: 30001))
        XCTAssertNil(store.promptedRelease)
    }

    func testManualCheckReportsBackendError() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent.error(operation: "update-check", code: "validation_failed", message: "release_url must be an HTTP/HTTPS URL")
            ])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default, manual: true)

        try await waitUntilStoreState { store.manualCheckOutcome != nil }
        XCTAssertEqual(store.state, .failed)
        guard case .failed(let message)? = store.manualCheckOutcome else {
            return XCTFail("expected failed outcome, got \(String(describing: store.manualCheckOutcome))")
        }
        XCTAssertTrue(message.contains("release_url"))
    }

    func testManualCheckBlocksConcurrentUpdateChecks() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = AppUpdateStore(coordinator: coordinator)
        store.markUIReady()

        store.checkNow(settings: .default)
        try await waitUntilStoreState { runner.calls.count == 1 && store.isChecking }
        store.checkNow(settings: .default, manual: true)

        XCTAssertEqual(store.state, .failed)
        XCTAssertEqual(store.error?.code, "operation_rejected")
        XCTAssertEqual(runner.calls.count, 1)
        runner.finishAll()
    }

    func testAutomaticCollisionIsDroppedSilently() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [], pauseBeforeEvents: true)
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default)
        try await waitUntilStoreState { runner.calls.count == 1 && store.isChecking }
        store.checkNow(settings: .default)

        XCTAssertEqual(store.state, .checking)
        XCTAssertNil(store.error)
        XCTAssertEqual(runner.calls.count, 1)
        runner.finishAll()
    }

    func testAutomaticChecksUseIntervalAndSkipWhenBusy() async throws {
        var scheduled: [(interval: TimeInterval, tick: @MainActor () -> Void)] = []
        var cancelCount = 0
        let scheduler: UpdateCheckScheduler = { interval, tick in
            scheduled.append((interval, tick))
            return AnyCancellable { cancelCount += 1 }
        }
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [], pauseBeforeEvents: true),
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true, payload: updateCheckPayload(shouldBlock: false, localVersionCode: 30001, currentVersion: 30001))
            ])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)), scheduler: scheduler)
        store.markUIReady()
        var settings = AppSettings.default
        settings.updateCheckIntervalHours = 6

        store.startAutomaticChecks(settings: settings)
        XCTAssertEqual(scheduled.count, 1)
        XCTAssertEqual(scheduled[0].interval, 6 * 3600)

        store.checkNow(settings: settings)
        try await waitUntilStoreState { runner.calls.count == 1 && store.isChecking }
        scheduled[0].tick()
        XCTAssertEqual(runner.calls.count, 1, "a tick during a running check is dropped")
        XCTAssertNotEqual(store.state, .failed)

        runner.finishAll()
        try await waitUntilStoreState { !store.isChecking }
        scheduled[0].tick()
        try await waitUntilStoreState { runner.calls.count == 2 }

        settings.updateCheckIntervalHours = 12
        store.startAutomaticChecks(settings: settings)
        XCTAssertEqual(cancelCount, 1, "restarting replaces the previous timer")
        XCTAssertEqual(scheduled.last?.interval, 12 * 3600)
        store.stopAutomaticChecks()
        XCTAssertEqual(cancelCount, 2)
        runner.finishAll()
    }

    func testInstallForwardsToInstallerAndRemindLaterResetsIt() async throws {
        let payload = updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)])
        ])
        let temp = try TemporaryDirectory()
        let installer = AppUpdateInstaller(
            environment: InstallEnvironment(
                bundleURL: temp.url.appendingPathComponent("TimeCapsuleSMB.app"),
                updatesDirectory: temp.url.appendingPathComponent("updates"),
                bundleIdentifier: "com.timecapsulesmb.TimeCapsuleSMB",
                expectedTeamID: nil,
                runtimeMode: { .developmentCheckout },
                hasBlockingActivity: { false }
            ),
            downloader: NoopDownloader(),
            processRunner: NoopRunner(),
            relaunch: { _ in }
        )
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)), installer: installer)
        store.markUIReady()

        XCTAssertNil(store.installUnavailableReason(), "no prompt means nothing to report")
        store.checkNow(settings: .default)
        try await waitUntilStoreState { store.promptedRelease != nil }
        XCTAssertEqual(store.installUnavailableReason(), "In-app updates are unavailable when running from a source checkout.")

        await store.install()
        guard case .failed(.unsupported) = store.installState else {
            return XCTFail("unexpected install state \(store.installState)")
        }

        store.remindLater()
        XCTAssertEqual(store.installState, .idle)
    }

    func testStoreWithoutInstallerReportsInstallUnavailable() async throws {
        let payload = updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002)
        let runner = StoreTestRunner(responses: [
            .init(events: [BackendEvent(type: "result", operation: "update-check", ok: true, payload: payload)])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))
        store.markUIReady()

        store.checkNow(settings: .default)
        try await waitUntilStoreState { store.promptedRelease != nil }

        XCTAssertNotNil(store.installUnavailableReason())
        await store.install()
        XCTAssertEqual(store.installState, .idle)
    }

    private struct NoopDownloader: UpdateDownloading {
        func download(_ url: URL, to destination: URL, expectedSize: Int?, progress: @escaping @Sendable (Double) -> Void) async throws {}
    }

    private struct NoopRunner: ProcessRunning {
        func run(_ executable: String, _ arguments: [String]) async throws -> ProcessOutput {
            ProcessOutput(exitCode: 0, stdout: "", stderr: "")
        }
    }

    func testPromptIsHeldUntilUIIsReady() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "update-check", ok: true,
                             payload: updateCheckPayload(shouldBlock: false, updateAvailable: true, currentVersion: 30002))
            ])
        ])
        let store = AppUpdateStore(coordinator: OperationCoordinator(backend: BackendClient(runner: runner)))

        store.checkNow(settings: .default)
        try await waitUntilStoreState { store.state == .updateAvailable && !store.isChecking }
        XCTAssertNil(store.promptedRelease, "prompt must wait for the window")

        store.markUIReady()

        XCTAssertEqual(store.promptedRelease?.versionCode, 30002)
        store.markUIReady()
        XCTAssertEqual(store.promptedRelease?.versionCode, 30002, "repeat calls are harmless")
    }

    private func updateCheckPayload(
        shouldBlock: Bool,
        updateAvailable: Bool = false,
        source: String = "network",
        localVersionCode: Int = 30001,
        currentVersion: Int = 30001,
        includeRelease: Bool = true
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
        let release: JSONValue = includeRelease
            ? .object([
                "tag": .string("v3.0.1"),
                "name": .string("v3.0.1"),
                "published_at": .string("2026-10-01T00:00:00Z"),
                "notes": .string("## Changes\n- one"),
                "html_url": .string("https://example.invalid/rel"),
                "prerelease": .bool(false),
                "asset": .object([
                    "name": .string("TimeCapsuleSMB.app.zip"),
                    "size": .number(10),
                    "download_url": .string("https://example.invalid/app.zip"),
                    "sha256": .string("ab")
                ])
            ])
            : .null
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
            "latest_tag": .string("v3.0.1"),
            "source": .string(source),
            "summary": .string(summary),
            "release": release
        ])
    }
}
