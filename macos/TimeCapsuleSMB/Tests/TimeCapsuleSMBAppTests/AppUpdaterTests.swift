import Combine
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

private final class FakeUpdaterDriver: AppUpdaterDriver {
    let canCheck = CurrentValueSubject<Bool, Never>(false)
    var automaticallyChecksForUpdates = true
    private(set) var startCount = 0
    private(set) var checkCount = 0

    var canCheckForUpdatesPublisher: AnyPublisher<Bool, Never> {
        canCheck.eraseToAnyPublisher()
    }

    func startUpdater() {
        startCount += 1
    }

    func checkForUpdates() {
        checkCount += 1
    }
}

@MainActor
final class AppUpdaterTests: XCTestCase {
    private let configured: [String: Any] = [
        "SUFeedURL": "https://example.invalid/appcast.xml",
        "SUPublicEDKey": "9nF9KWJxNjm2RCdTSKFTbgXv8XrLrrhgwTiDy84Xqro=",
    ]

    func testSparkleIsConfiguredOnlyWithAFeedAndAPublicKey() {
        XCTAssertTrue(AppUpdater.isConfigured(configured))
        let unconfigured: [String: [String: Any]?] = [
            "no Info.plist": nil,
            "empty Info.plist": [:],
            "no feed": configured.filter { $0.key != "SUFeedURL" },
            "no public key": configured.filter { $0.key != "SUPublicEDKey" },
            "blank feed": configured.merging(["SUFeedURL": "  "]) { $1 },
            "blank public key": configured.merging(["SUPublicEDKey": ""]) { $1 },
            "public key that is not a string": configured.merging(["SUPublicEDKey": Data([1, 2])]) { $1 },
        ]
        for (name, info) in unconfigured {
            XCTAssertFalse(AppUpdater.isConfigured(info), name)
        }
    }

    func testAConfiguredBundleStartsSparkleExactlyOnce() throws {
        let temp = try TemporaryDirectory()
        let bundleURL = temp.url.appendingPathComponent("Configured.app")
        let contents = bundleURL.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let plist = try PropertyListSerialization.data(fromPropertyList: configured, format: .xml, options: 0)
        try plist.write(to: contents.appendingPathComponent("Info.plist"))
        let bundle = try XCTUnwrap(Bundle(url: bundleURL))
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)

        updater.start(ifConfiguredIn: bundle)
        updater.start(ifConfiguredIn: bundle)

        XCTAssertEqual(driver.startCount, 1)
    }

    func testABundleWithoutSparkleKeysNeverStartsSparkle() {
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)

        // The test bundle's Info.plist has no feed or key, like a swift test or Xcode run.
        updater.start(ifConfiguredIn: Bundle(for: Self.self))

        XCTAssertEqual(driver.startCount, 0)
    }

    func testCanCheckFollowsSparkle() {
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)
        XCTAssertFalse(updater.canCheckForUpdates)

        driver.canCheck.send(true)
        XCTAssertTrue(updater.canCheckForUpdates)

        driver.canCheck.send(false)
        XCTAssertFalse(updater.canCheckForUpdates)
    }

    func testCheckForUpdatesAsksSparkleEachTime() {
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)

        updater.checkForUpdates()
        updater.checkForUpdates()

        XCTAssertEqual(driver.checkCount, 2)
    }

    func testTheToggleReadsAndWritesSparklesSetting() {
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)
        var changes = 0
        let subscription = updater.objectWillChange.sink { changes += 1 }

        XCTAssertTrue(updater.automaticallyChecksForUpdates)
        updater.automaticallyChecksForUpdates = false

        XCTAssertFalse(driver.automaticallyChecksForUpdates)
        XCTAssertFalse(updater.automaticallyChecksForUpdates)
        XCTAssertEqual(changes, 1)
        subscription.cancel()
    }

    func testTheMenuItemIsEnabledOnlyWhileSparkleCanCheck() {
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver)
        let title = L10n.string("app_menu.check_for_updates")
        func menuItem() -> AccessibleElement? {
            accessibleElements(CheckForUpdatesCommand(updater: updater), size: CGSize(width: 300, height: 60))
                .first { $0.role == .button && $0.text == title }
        }

        XCTAssertEqual(menuItem()?.isEnabled, false)
        driver.canCheck.send(true)
        XCTAssertEqual(menuItem()?.isEnabled, true)
    }

    func testTheMenuItemFollowsALanguageChangeInSettings() async throws {
        let previous = L10n.currentLanguage
        defer { L10n.apply(language: previous) }
        let fixture = try await AppViewFixture()
        var settings = fixture.appStore.appSettingsStore.settings
        settings.language = .english
        try await fixture.appStore.saveAppSettings(settings)
        let host = LiveHostedView(CheckForUpdatesCommand(updater: AppUpdater(driver: FakeUpdaterDriver())), size: CGSize(width: 300, height: 60))
        defer { host.close() }
        func titles() -> [String] {
            accessibleElements(in: host.contentView).filter { $0.role == .button }.map(\.text)
        }
        XCTAssertEqual(titles(), [L10n.string("app_menu.check_for_updates", language: .english)])

        settings.language = .german
        try await fixture.appStore.saveAppSettings(settings)

        let german = L10n.string("app_menu.check_for_updates", language: .german)
        try await waitUntilStoreState(timeoutNanoseconds: 2_000_000_000) { titles() == [german] }
        XCTAssertEqual(titles(), [german])
    }

    func testSettingsShowsTheAutomaticChecksToggle() async throws {
        let fixture = try await AppViewFixture()
        let view = AppSettingsView(
            appStore: fixture.appStore,
            appSettingsStore: fixture.appStore.appSettingsStore,
            editor: fixture.composition.appSettingsEditorStore,
            updater: AppUpdater(driver: FakeUpdaterDriver())
        )

        let texts = accessibleElements(view, size: CGSize(width: 900, height: 2400)).map(\.text)

        XCTAssertTrue(texts.contains(L10n.string("app_settings.automatic_updates")), "\(texts)")
    }
}

final class UpdateCheckDiagnosticsTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suiteName = ""

    override func setUp() {
        super.setUp()
        suiteName = "UpdateCheckDiagnosticsTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        super.tearDown()
    }

    func testTheUsersChoiceWinsOverThePackagedDefault() {
        defaults.set(false, forKey: "SUEnableAutomaticChecks")
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUEnableAutomaticChecks": true]).automaticChecks)

        defaults.set(true, forKey: "SUEnableAutomaticChecks")
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUEnableAutomaticChecks": false]).automaticChecks)
    }

    func testThePackagedDefaultAppliesUntilTheUserChooses() {
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUEnableAutomaticChecks": true]).automaticChecks)
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, info: [:]).automaticChecks)
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, info: nil).automaticChecks)
    }

    func testAutomaticInstallsAreTheUpdateWindowsCheckbox() {
        // The app packages no default, and Sparkle's checkbox starts unticked.
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUEnableAutomaticChecks": true]).automaticInstalls)
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUAutomaticallyUpdate": true]).automaticInstalls)

        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        let ticked = UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUEnableAutomaticChecks": false])
        XCTAssertTrue(ticked.automaticInstalls)
        XCTAssertFalse(ticked.automaticChecks)

        defaults.set(false, forKey: "SUAutomaticallyUpdate")
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, info: ["SUAutomaticallyUpdate": true]).automaticInstalls)
    }

    func testTheLastCheckIsSparklesSavedTime() {
        XCTAssertNil(UpdateCheckDiagnostics.read(defaults: defaults, info: nil).lastCheck)

        let checked = Date(timeIntervalSince1970: 1_779_790_000)
        defaults.set(checked, forKey: "SULastCheckTime")

        XCTAssertEqual(UpdateCheckDiagnostics.read(defaults: defaults, info: nil).lastCheck, checked)
    }
}
