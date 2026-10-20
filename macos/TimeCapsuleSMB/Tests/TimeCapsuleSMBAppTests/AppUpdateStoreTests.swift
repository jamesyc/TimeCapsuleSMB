import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppUpdateStoreTests: XCTestCase {
    func testLaunchPreferenceIsRespectedEvenWhenHelperIsUnavailable() async throws {
        for enabled in [false, true] {
            let temp = try TemporaryDirectory()
            let settingsStore = AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json"))
            var settings = AppSettings.default
            settings.checkForUpdatesOnLaunch = enabled
            settings.versionCheckURL = "https://example.invalid/helper-compatibility.json"
            try await settingsStore.save(settings)
            let checker = StubReleaseChecker()
            let updates = AppUpdateStore(checker: checker, installedVersion: "3.6.0")
            let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
            let readiness = AppReadinessStore(
                backend: coordinator.backend,
                runtimeResolver: MissingUpdateTestRuntime(),
                helperPathProvider: { "" }
            )
            let app = AppStore(
                appReadinessStore: readiness,
                appSettingsStore: settingsStore,
                deviceRegistry: DeviceRegistryStore(applicationSupportURL: temp.url),
                operationCoordinator: coordinator,
                passwordStore: InMemoryPasswordStore(),
                appUpdateStore: updates
            )

            await app.start()

            XCTAssertEqual(readiness.state.kind, .blocked)
            if enabled {
                try await waitUntilStoreState { updates.state == .updateAvailable }
                XCTAssertEqual(checker.calls, 1)
                XCTAssertNotNil(updates.notice)
            } else {
                XCTAssertEqual(updates.state, .idle)
                XCTAssertEqual(checker.calls, 0)
                updates.checkNow()
                try await waitUntilStoreState { updates.state == .updateAvailable }
                XCTAssertEqual(checker.calls, 1)
            }
        }
    }

    func testNewReleasePromptsEvenDuringAutomaticCheck() async throws {
        let checker = StubReleaseChecker()
        let store = AppUpdateStore(checker: checker, installedVersion: "3.6.0")

        store.checkNow(automatically: true)

        XCTAssertTrue(store.isChecking)
        try await waitUntilStoreState { !store.isChecking }
        XCTAssertEqual(store.state, .updateAvailable)
        let result = try XCTUnwrap(store.payload)
        XCTAssertEqual(result.release.downloadURL, checker.release.downloadURL)
        XCTAssertEqual(store.notice, .available(result))
        XCTAssertTrue(store.notice?.message.contains("v3.7.0") == true)
        XCTAssertTrue(store.notice?.message.contains("3.6.0") == true)
    }

    func testManualCheckConfirmsCurrentOrNewerInstalledVersion() async throws {
        for installed in ["3.7.0", "3.8.0", "4.0.0"] {
            let store = AppUpdateStore(checker: StubReleaseChecker(), installedVersion: installed)

            store.checkNow()

            try await waitUntilStoreState { !store.isChecking }
            XCTAssertEqual(store.state, .current)
            XCTAssertEqual(store.payload?.updateAvailable, false)
            XCTAssertEqual(store.notice, .current)
            XCTAssertNil(store.error)
        }
    }

    func testAutomaticCurrentCheckDoesNotInterruptUser() async throws {
        let store = AppUpdateStore(checker: StubReleaseChecker(), installedVersion: "3.7.0")

        store.checkNow(automatically: true)

        try await waitUntilStoreState { !store.isChecking }
        XCTAssertEqual(store.state, .current)
        XCTAssertNil(store.notice)
    }

    func testFailedChecksAreVisibleButOnlyManualChecksShowAlert() async throws {
        for automatic in [false, true] {
            let checker = StubReleaseChecker()
            checker.failure = URLError(.notConnectedToInternet)
            let store = AppUpdateStore(checker: checker, installedVersion: "3.6.0")

            store.checkNow(automatically: automatic)

            try await waitUntilStoreState { !store.isChecking }
            XCTAssertEqual(store.state, .failed)
            XCTAssertNil(store.payload)
            XCTAssertEqual(store.error?.code, "release_check_failed")
            if automatic {
                XCTAssertNil(store.notice)
            } else {
                XCTAssertEqual(store.notice, .failed(URLError(.notConnectedToInternet).localizedDescription))
            }
        }
    }

    func testMissingOrInvalidInstalledVersionDoesNotClaimToBeCurrent() async throws {
        for installed: String? in [nil, "development", "3.x.0"] {
            let checker = StubReleaseChecker()
            let store = AppUpdateStore(checker: checker, installedVersion: installed)

            store.checkNow()

            try await waitUntilStoreState { !store.isChecking }
            XCTAssertEqual(store.state, .failed)
            XCTAssertNotNil(store.notice)
            XCTAssertEqual(checker.calls, 0)
        }
    }

    func testConcurrentChecksAreCoalescedAndRetryClearsOldResult() async throws {
        let checker = StubReleaseChecker()
        checker.paused = true
        let store = AppUpdateStore(checker: checker, installedVersion: "3.6.0")
        store.checkNow()
        try await waitUntilStoreState { checker.calls == 1 }

        store.checkNow()

        XCTAssertEqual(store.state, .checking)
        XCTAssertEqual(checker.calls, 1)
        checker.resume()
        try await waitUntilStoreState { store.state == .updateAvailable }
        checker.failure = URLError(.timedOut)
        store.checkNow()
        XCTAssertNil(store.payload)
        XCTAssertNil(store.notice)
        try await waitUntilStoreState { store.state == .failed }
        checker.failure = nil
        store.checkNow()
        XCTAssertNil(store.error)
        try await waitUntilStoreState { store.state == .updateAvailable }
        XCTAssertEqual(checker.calls, 3)
    }

    func testFailedDownloadOpeningPreservesLinkAndShowsError() async throws {
        let store = AppUpdateStore(checker: StubReleaseChecker(), installedVersion: "3.6.0")
        store.checkNow()
        try await waitUntilStoreState { store.state == .updateAvailable }

        store.downloadOpeningFailed()

        XCTAssertNotNil(store.payload?.release.downloadURL)
        XCTAssertEqual(store.error?.code, "open_download_failed")
        XCTAssertEqual(store.notice, .failed(AppUpdateError.openDownloadFailed.localizedDescription))
    }

    func testUpdateCopyResolvesInEverySupportedLanguage() throws {
        let previous = L10n.currentLanguage
        defer { L10n.apply(language: previous) }
        let result = AppUpdateResult(installedVersion: "3.6.0", release: StubReleaseChecker().release, updateAvailable: true)
        let keys = [
            "app_update.check_menu", "app_update.download", "app_update.later",
            "app_update.alert.available_title", "app_update.alert.available_message",
            "app_update.alert.current_message", "app_update.feed_description",
            "app_update.error.installed_version", "app_update.error.invalid_release",
            "app_update.error.missing_package", "app_update.error.http_status", "app_update.error.open_download"
        ]
        for language in AppLanguage.allCases where language != .system {
            L10n.apply(language: language)
            let strings = L10n.strings(language: language)
            for key in keys {
                XCTAssertNotNil(strings[key], "\(language): \(key)")
            }
            let notice = AppUpdateNotice.available(result)
            XCTAssertTrue(notice.message.contains("3.6.0"))
            XCTAssertTrue(notice.message.contains("v3.7.0"))
            XCTAssertFalse(notice.message.contains("%@"))
            XCTAssertFalse(AppUpdateError.httpStatus(403).localizedDescription.contains("%"))
        }
    }
}

private struct MissingUpdateTestRuntime: AppRuntimeResolving {
    func resolve(helperPath: String?) throws -> HelperResolution {
        throw CocoaError(.fileNoSuchFile)
    }

    func runtimeIssues(for resolution: HelperResolution) -> [BundleRuntimeIssue] { [] }
}

@MainActor
private final class StubReleaseChecker: AppReleaseChecking {
    let release = GitHubAppRelease(
        tag: "v3.7.0",
        downloadURL: URL(string: "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.7.0/TimeCapsuleSMB.app.zip")!
    )
    var failure: Error?
    var calls = 0
    var paused = false
    private var continuation: CheckedContinuation<Void, Never>?

    func latestRelease() async throws -> GitHubAppRelease {
        calls += 1
        if paused {
            await withCheckedContinuation { continuation = $0 }
        }
        if let failure { throw failure }
        return release
    }

    func resume() {
        paused = false
        continuation?.resume()
        continuation = nil
    }
}
