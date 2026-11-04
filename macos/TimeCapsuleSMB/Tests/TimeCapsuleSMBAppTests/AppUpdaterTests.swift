import Combine
import Sparkle
import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

private final class FakeUpdaterDriver: AppUpdaterDriver {
    let canCheck = CurrentValueSubject<Bool, Never>(false)
    var automaticallyChecksForUpdates = true
    var onStart: () -> Void = {}
    private(set) var startCount = 0
    private(set) var checkCount = 0

    var canCheckForUpdatesPublisher: AnyPublisher<Bool, Never> {
        canCheck.eraseToAnyPublisher()
    }

    func startUpdater() {
        startCount += 1
        onStart()
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
    private let suiteName = "AppUpdaterTests-\(UUID().uuidString)"
    private var defaults: UserDefaults!
    // The argument domain is shared by every UserDefaults in the process.
    private var launchArguments: [String: Any] = [:]

    override func setUp() {
        super.setUp()
        defaults = UserDefaults(suiteName: suiteName)
        launchArguments = defaults.volatileDomain(forName: UserDefaults.argumentDomain)
    }

    override func tearDown() {
        defaults.setVolatileDomain(launchArguments, forName: UserDefaults.argumentDomain)
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        super.tearDown()
    }

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

    private func configuredBundle(in temp: TemporaryDirectory) throws -> Bundle {
        let bundleURL = temp.url.appendingPathComponent("Configured.app")
        let contents = bundleURL.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let plist = try PropertyListSerialization.data(fromPropertyList: configured, format: .xml, options: 0)
        try plist.write(to: contents.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: bundleURL))
    }

    func testAConfiguredBundleStartsSparkleExactlyOnce() throws {
        let temp = try TemporaryDirectory()
        let bundle = try configuredBundle(in: temp)
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver, defaults: defaults)

        updater.start(ifConfiguredIn: bundle)
        updater.start(ifConfiguredIn: bundle)

        XCTAssertEqual(driver.startCount, 1)
    }

    func testSparkleStartsWithoutALastCheckSoItChecksAtLaunch() throws {
        let temp = try TemporaryDirectory()
        let bundle = try configuredBundle(in: temp)
        defaults.set(Date(), forKey: "SULastCheckTime")
        let driver = FakeUpdaterDriver()
        var lastCheckAtStart: Date? = .distantPast
        driver.onStart = { [defaults] in
            lastCheckAtStart = defaults?.object(forKey: "SULastCheckTime") as? Date
        }
        let updater = AppUpdater(driver: driver, defaults: defaults)

        updater.start(ifConfiguredIn: bundle)

        XCTAssertEqual(driver.startCount, 1)
        XCTAssertNil(lastCheckAtStart)
    }

    func testStartingKeepsTheLastCheckWhenAutomaticChecksAreOff() throws {
        let temp = try TemporaryDirectory()
        let bundle = try configuredBundle(in: temp)
        let lastCheck = Date(timeIntervalSince1970: 1_000_000)
        defaults.set(lastCheck, forKey: "SULastCheckTime")
        let driver = FakeUpdaterDriver()
        driver.automaticallyChecksForUpdates = false
        let updater = AppUpdater(driver: driver, defaults: defaults)

        updater.start(ifConfiguredIn: bundle)

        XCTAssertEqual(driver.startCount, 1)
        XCTAssertEqual(defaults.object(forKey: "SULastCheckTime") as? Date, lastCheck)
    }

    func testABundleWithoutSparkleKeysNeverStartsSparkle() {
        let lastCheck = Date(timeIntervalSince1970: 1_000_000)
        defaults.set(lastCheck, forKey: "SULastCheckTime")
        let driver = FakeUpdaterDriver()
        let updater = AppUpdater(driver: driver, defaults: defaults)

        // The test bundle's Info.plist has no feed or key, like a swift test or Xcode run.
        updater.start(ifConfiguredIn: Bundle(for: Self.self))

        XCTAssertEqual(driver.startCount, 0)
        XCTAssertEqual(defaults.object(forKey: "SULastCheckTime") as? Date, lastCheck)
    }

    private var store: UpdateMemoryStore {
        UpdateMemoryStore(defaults: defaults)
    }

    private var settingAsRead: Bool? {
        defaults.object(forKey: "SUAutomaticallyUpdate") as? Bool
    }

    private func resetLaunchArguments() {
        defaults.setVolatileDomain(launchArguments, forName: UserDefaults.argumentDomain)
    }

    func testAnInterruptedDownloadAsksOnceOnTheNextLaunchAndKeepsTheUsersChoice() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        defaults.setVolatileDomain(
            launchArguments.merging(["KeptArgument": "kept"]) { $1 },
            forName: UserDefaults.argumentDomain
        )
        store.memory = .interrupted(version: "30900")

        store.askBeforeDownloadingThisLaunch(installedVersion: "30803")

        XCTAssertEqual(settingAsRead, false)
        XCTAssertEqual(defaults.persistentDomain(forName: suiteName)?["SUAutomaticallyUpdate"] as? Bool, true)
        XCTAssertEqual(defaults.string(forKey: "KeptArgument"), "kept")
        XCTAssertEqual(store.memory, .none)

        // The launch after that downloads silently again.
        resetLaunchArguments()
        store.askBeforeDownloadingThisLaunch(installedVersion: "30803")
        XCTAssertEqual(settingAsRead, true)
    }

    func testNeedingAuthorizationAsksOnEveryLaunchUntilTheAppReachesThatVersion() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        store.memory = .needsAuthorization(version: "30900")

        for _ in 0..<3 {
            resetLaunchArguments()
            store.askBeforeDownloadingThisLaunch(installedVersion: "30803")

            XCTAssertEqual(settingAsRead, false)
            XCTAssertEqual(store.memory, .needsAuthorization(version: "30900"))
        }

        resetLaunchArguments()
        store.askBeforeDownloadingThisLaunch(installedVersion: "30900")

        XCTAssertEqual(settingAsRead, true)
        XCTAssertEqual(store.memory, .none)
    }

    func testAVersionTheAppAlreadyReachedDoesNotAsk() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")

        for installed in ["30900", "30901"] {
            for memory in [UpdateMemory.interrupted(version: "30900"), .needsAuthorization(version: "30900")] {
                store.memory = memory
                store.askBeforeDownloadingThisLaunch(installedVersion: installed)

                XCTAssertEqual(settingAsRead, true, installed)
                XCTAssertEqual(store.memory, .none, installed)
            }
        }
    }

    func testVersionsCompareAsNumbers() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        store.memory = .interrupted(version: "100000")

        store.askBeforeDownloadingThisLaunch(installedVersion: "99999")

        XCTAssertEqual(settingAsRead, false)
    }

    func testNothingRememberedLeavesTheSettingAlone() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")

        store.askBeforeDownloadingThisLaunch(installedVersion: "30803")

        XCTAssertEqual(settingAsRead, true)
    }

    func testTheMemorySurvivesAReadFromDefaults() {
        for memory in [UpdateMemory.none, .interrupted(version: "30900"), .needsAuthorization(version: "30900")] {
            store.memory = memory

            XCTAssertEqual(UpdateMemoryStore(defaults: defaults).memory, memory)
        }
    }

    func testAFailureIsKeptUntilCleared() {
        let failure = UpdateFailure(message: "The signature is invalid.", date: Date(timeIntervalSince1970: 1_000_000), appVersion: "30803")
        store.lastFailure = failure
        XCTAssertEqual(UpdateMemoryStore(defaults: defaults).lastFailure, failure)

        store.lastFailure = nil
        XCTAssertNil(store.lastFailure)
    }

    func testAFailureSurvivesLaunchesOfTheSameVersion() {
        let failure = UpdateFailure(message: "Offline", date: Date(timeIntervalSince1970: 1_000_000), appVersion: "30803")
        store.lastFailure = failure

        store.forgetFailureFromOlderVersion(installedVersion: "30803")

        XCTAssertEqual(store.lastFailure, failure)
    }

    func testANewerAppForgetsAFailureFromAnOlderOne() {
        store.lastFailure = UpdateFailure(message: "Offline", date: Date(timeIntervalSince1970: 1_000_000), appVersion: "30803")

        store.forgetFailureFromOlderVersion(installedVersion: "30900")

        XCTAssertNil(store.lastFailure)
    }

    func testCreatingTheDriverForgetsAFailureFromAnOlderApp() {
        store.lastFailure = UpdateFailure(message: "Offline", date: Date(timeIntervalSince1970: 1_000_000), appVersion: "30803")

        _ = SparkleUpdaterDriver(defaults: defaults, installedVersion: "30900")

        XCTAssertNil(store.lastFailure)
    }

    func testNeedingAuthorizationAsksForTheRestOfThisLaunch() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")

        store.remember(.needsAuthorization(version: "30900"))

        XCTAssertEqual(settingAsRead, false)
        XCTAssertEqual(defaults.persistentDomain(forName: suiteName)?["SUAutomaticallyUpdate"] as? Bool, true)
    }

    func testOtherMemoryLeavesThisLaunchSilent() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")

        store.remember(.interrupted(version: "30900"))
        store.remember(.none)

        XCTAssertEqual(settingAsRead, true)
    }

    func testCreatingTheDriverAppliesTheMemory() {
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        store.memory = .interrupted(version: "30900")

        _ = SparkleUpdaterDriver(defaults: defaults, installedVersion: "30803")

        XCTAssertEqual(settingAsRead, false)
    }

    func testScheduledWindowsAlwaysWaitForTheFlow() throws {
        let driver = SparkleUpdaterDriver(defaults: defaults)

        for focus in [true, false] {
            XCTAssertFalse(driver.standardUserDriverShouldHandleShowingScheduledUpdate(SUAppcastItem.empty(), andInImmediateFocus: focus))
        }
    }

    func testSparkleStagesMapToWindowStages() {
        XCTAssertEqual(SparkleEvents.window(stage: .notDownloaded, version: "1").stage, .notDownloaded)
        XCTAssertEqual(SparkleEvents.window(stage: .downloaded, version: "1").stage, .downloaded)
        XCTAssertEqual(SparkleEvents.window(stage: .installing, version: "1").stage, .installing)
        XCTAssertEqual(SparkleEvents.window(stage: .installing, version: "30900").version, "30900")
    }

    func testAWindowSparkleShowedIsNotOneItLeftToTheFlow() {
        let window = UpdateWindow(stage: .installing, version: "30900")
        let conditions = UpdateWindowConditions(appIsActive: true, hasBlockingWork: false, isTyping: false, hasModal: false)

        XCTAssertEqual(SparkleEvents.windowEvent(handled: true, window: window, conditions: conditions), .windowShown(window))
        XCTAssertEqual(SparkleEvents.windowEvent(handled: false, window: window, conditions: conditions), .windowWaiting(window, conditions))
    }

    func testOnlyInstallOnQuitKeepsTheInstaller() {
        XCTAssertEqual(SparkleEvents.choiceEvent(.dismiss, stage: .installing, version: "30900"), .installerKept(version: "30900"))
        XCTAssertNil(SparkleEvents.choiceEvent(.dismiss, stage: .notDownloaded, version: "30900"))
        XCTAssertNil(SparkleEvents.choiceEvent(.dismiss, stage: .downloaded, version: "30900"))
    }

    func testSkippingIsReportedAtEveryStage() {
        for stage in [SPUUserUpdateStage.notDownloaded, .downloaded, .installing] {
            XCTAssertEqual(SparkleEvents.choiceEvent(.skip, stage: stage, version: "30900"), .updateSkipped)
        }
    }

    func testInstallIsLeftToTheRelaunchEvent() {
        for stage in [SPUUserUpdateStage.notDownloaded, .downloaded, .installing] {
            XCTAssertNil(SparkleEvents.choiceEvent(.install, stage: stage, version: "30900"))
        }
    }

    func testSparkleErrorsMapToHowTheCycleEnded() {
        func error(_ code: SUError) -> NSError {
            NSError(domain: SUSparkleErrorDomain, code: Int(code.rawValue), userInfo: [NSLocalizedDescriptionKey: "text"])
        }

        XCTAssertEqual(SparkleEvents.cycleEvent(error: nil), .cycleFinished(.finished))
        XCTAssertEqual(SparkleEvents.cycleEvent(error: error(.noUpdateError)), .cycleFinished(.noUpdateFound))
        for normalEnd in [SUError.installationCanceledError, .installationAuthorizeLaterError] {
            XCTAssertEqual(SparkleEvents.cycleEvent(error: error(normalEnd)), .cycleFinished(.finished))
        }
        XCTAssertEqual(SparkleEvents.cycleEvent(error: error(.downloadError)), .cycleFinished(.failed("text")))
    }

    func testSparkleFindsTheDelegateMethodsTheUpdateFlowNeeds() {
        // All are optional in Sparkle's protocols: a signature that stops
        // matching would still build, and the step would silently not run.
        let driver = SparkleUpdaterDriver(defaults: defaults)
        let selectors = [
            #selector(SPUUpdaterDelegate.updater(_:willInstallUpdateOnQuit:immediateInstallationBlock:)),
            #selector(SPUUpdaterDelegate.updater(_:didFinishUpdateCycleFor:error:)),
            #selector(SPUUpdaterDelegate.updater(_:willDownloadUpdate:with:)),
            #selector(SPUUpdaterDelegate.updater(_:userDidMake:forUpdate:state:)),
            #selector(SPUUpdaterDelegate.updaterWillRelaunchApplication(_:)),
            #selector(getter: SPUStandardUserDriverDelegate.supportsGentleScheduledUpdateReminders),
            #selector(SPUStandardUserDriverDelegate.standardUserDriverShouldHandleShowingScheduledUpdate(_:andInImmediateFocus:)),
            #selector(SPUStandardUserDriverDelegate.standardUserDriverWillHandleShowingUpdate(_:forUpdate:state:)),
            #selector(SPUStandardUserDriverDelegate.standardUserDriverDidReceiveUserAttention(forUpdate:)),
            #selector(SPUStandardUserDriverDelegate.standardUserDriverWillFinishUpdateSession),
        ]

        for selector in selectors {
            XCTAssertTrue(driver.responds(to: selector), "\(selector)")
        }
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

/// What the update window's conditions read from real windows and SwiftUI
/// controls. Each window is on screen, off to the side, or SwiftUI presents nothing.
@MainActor
final class UpdateWindowConditionsTests: XCTestCase {
    private var windows: [NSWindow] = []

    override func tearDown() {
        for window in windows {
            if let sheet = window.attachedSheet {
                window.endSheet(sheet)
            }
            window.close()
        }
        windows = []
        super.tearDown()
    }

    private func show<V: View>(_ view: V) -> NSWindow {
        let window = NSWindow(
            contentRect: CGRect(x: -3000, y: -3000, width: 500, height: 300),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: view)
        window.orderFrontRegardless()
        windows.append(window)
        return window
    }

    private func textField(in view: NSView) -> NSTextField? {
        if let field = view as? NSTextField, field.isEditable {
            return field
        }
        return view.subviews.lazy.compactMap { self.textField(in: $0) }.first
    }

    private func assertFocusCountsAsTyping<V: View>(_ control: V, file: StaticString = #filePath, line: UInt = #line) async throws {
        let window = show(control)
        XCTAssertFalse(UpdateWindowConditions.isTyping(in: window), file: file, line: line)

        try await waitUntilStoreState { self.textField(in: window.contentView!) != nil }
        let field = try XCTUnwrap(textField(in: window.contentView!), file: file, line: line)
        XCTAssertTrue(window.makeFirstResponder(field), file: file, line: line)

        XCTAssertTrue(UpdateWindowConditions.isTyping(in: window), file: file, line: line)
    }

    func testATextFieldWithFocusCountsAsTyping() async throws {
        try await assertFocusCountsAsTyping(TextField("Host", text: .constant("")))
    }

    func testAPasswordFieldWithFocusCountsAsTyping() async throws {
        try await assertFocusCountsAsTyping(SecureField("Password", text: .constant("")))
    }

    func testLeavingTheFieldStopsCountingAsTyping() async throws {
        try await assertFocusCountsAsTyping(TextField("Host", text: .constant("")))
        let window = try XCTUnwrap(windows.first)

        XCTAssertTrue(window.makeFirstResponder(nil))

        XCTAssertFalse(UpdateWindowConditions.isTyping(in: window))
    }

    func testNoKeyWindowIsNotTyping() {
        XCTAssertFalse(UpdateWindowConditions.isTyping(in: nil))
    }

    private struct FakeHost: UpdateWindowHost {
        var isActive = true
        var keyWindow: NSWindow?
        var modalWindow: NSWindow?
        var windows: [NSWindow] = []
    }

    private func conditions(from host: FakeHost, hasBlockingWork: Bool = false) -> UpdateWindowConditions {
        UpdateWindowConditions.read(from: host, hasBlockingWork: hasBlockingWork)
    }

    func testAnEmptyAppHoldsNothingBack() {
        XCTAssertEqual(
            conditions(from: FakeHost()),
            UpdateWindowConditions(appIsActive: true, hasBlockingWork: false, isTyping: false, hasModal: false)
        )
    }

    func testTypingIsReadFromTheKeyWindow() async throws {
        try await assertFocusCountsAsTyping(TextField("Host", text: .constant("")))
        let focused = try XCTUnwrap(windows.first)
        let plain = show(Text("Devices"))

        XCTAssertTrue(conditions(from: FakeHost(keyWindow: focused, windows: [focused, plain])).isTyping)
        XCTAssertFalse(conditions(from: FakeHost(keyWindow: plain, windows: [focused, plain])).isTyping)
    }

    func testASheetOnAnyWindowCounts() async throws {
        let plain = show(Text("Devices"))
        let confirming = show(Text("Devices").sheet(isPresented: .constant(true)) { Text("Diagnostics") })
        try await waitUntilStoreState { UpdateWindowConditions.hasSheet(confirming) }

        XCTAssertTrue(conditions(from: FakeHost(keyWindow: plain, windows: [plain, confirming])).hasModal)
        XCTAssertFalse(conditions(from: FakeHost(keyWindow: plain, windows: [plain])).hasModal)
    }

    func testAModalWindowCounts() {
        let modal = show(Text("Devices"))

        XCTAssertTrue(conditions(from: FakeHost(modalWindow: modal)).hasModal)
    }

    func testTheHostsOtherFactsCarryThrough() {
        XCTAssertFalse(conditions(from: FakeHost(isActive: false)).appIsActive)
        XCTAssertTrue(conditions(from: FakeHost(), hasBlockingWork: true).hasBlockingWork)
    }

    func testTheRealAppsConditionsSeeItsSheetsAndCloseGuard() async throws {
        let confirming = show(Text("Devices").alert("Forget device?", isPresented: .constant(true)) {
            Button("Cancel", role: .cancel) {}
        })
        try await waitUntilStoreState { UpdateWindowConditions.hasSheet(confirming) }
        AppCloseGuard.shared.configure { true }
        defer { AppCloseGuard.shared.configure { false } }

        let conditions = UpdateWindowConditions.current

        XCTAssertTrue(conditions.hasModal)
        XCTAssertTrue(conditions.hasBlockingWork)
    }

    func testAPlainWindowHasNoSheet() {
        XCTAssertFalse(UpdateWindowConditions.hasSheet(show(Text("Devices"))))
    }

    func testASwiftUISheetHoldsTheWindowBack() async throws {
        let window = show(Text("Devices").sheet(isPresented: .constant(true)) { Text("Diagnostics") })

        try await waitUntilStoreState { UpdateWindowConditions.hasSheet(window) }
    }

    func testASwiftUIAlertHoldsTheWindowBack() async throws {
        let window = show(Text("Devices").alert("Forget device?", isPresented: .constant(true)) {
            Button("Cancel", role: .cancel) {}
        })

        try await waitUntilStoreState { UpdateWindowConditions.hasSheet(window) }
    }

    func testASwiftUIConfirmationDialogHoldsTheWindowBack() async throws {
        let window = show(Text("Devices").confirmationDialog("Forget device?", isPresented: .constant(true)) {
            Button("Forget", role: .destructive) {}
        })

        try await waitUntilStoreState { UpdateWindowConditions.hasSheet(window) }
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
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": true]).automaticChecks)

        defaults.set(true, forKey: "SUEnableAutomaticChecks")
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": false]).automaticChecks)
    }

    func testThePackagedDefaultAppliesUntilTheUserChooses() {
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": true]).automaticChecks)
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: [:]).automaticChecks)
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil).automaticChecks)
    }

    func testAutomaticInstallsFollowTheUpdateWindowsCheckbox() {
        let packaged: [String: Any] = ["SUEnableAutomaticChecks": true, "SUAutomaticallyUpdate": true]
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": true]).automaticInstalls)
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: packaged).automaticInstalls)

        defaults.set(false, forKey: "SUAutomaticallyUpdate")
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: packaged).automaticInstalls)

        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": true]).automaticInstalls)
    }

    func testAutomaticInstallsAreOffWhileAutomaticChecksAreOff() {
        // Sparkle ignores the checkbox then, whatever it holds.
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        defaults.set(false, forKey: "SUEnableAutomaticChecks")

        let diagnostics = UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: ["SUEnableAutomaticChecks": true])

        XCTAssertFalse(diagnostics.automaticChecks)
        XCTAssertFalse(diagnostics.automaticInstalls)
    }

    func testReadingCollectsTheFlowMemorySkipAndFailure() {
        defaults.set("30803", forKey: "SUSkippedVersion")
        let store = UpdateMemoryStore(defaults: defaults)
        store.memory = .needsAuthorization(version: "30900")
        let failure = UpdateFailure(message: "The signature is invalid.", date: Date(timeIntervalSince1970: 1_000_000), appVersion: "30803")
        store.lastFailure = failure

        let diagnostics = UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil, flow: "idle for 3 min", installedVersion: "30802")

        XCTAssertEqual(diagnostics.flow, "idle for 3 min")
        XCTAssertEqual(diagnostics.skippedVersion, "30803")
        XCTAssertEqual(diagnostics.memory, .needsAuthorization(version: "30900"))
        XCTAssertEqual(diagnostics.lastFailure, failure)
    }

    func testASkippedVersionTheAppPassedIsNotReported() {
        defaults.set("30803", forKey: "SUSkippedVersion")

        for installed in ["30803", "30900"] {
            XCTAssertNil(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil, installedVersion: installed).skippedVersion)
        }
    }

    func testAnAskingLaunchStillReportsTheUsersChoice() {
        let packaged: [String: Any] = ["SUEnableAutomaticChecks": true, "SUAutomaticallyUpdate": true]
        let launchArguments = defaults.volatileDomain(forName: UserDefaults.argumentDomain)
        defer { defaults.setVolatileDomain(launchArguments, forName: UserDefaults.argumentDomain) }
        defaults.set(true, forKey: "SUAutomaticallyUpdate")
        UpdateMemoryStore(defaults: defaults).remember(.needsAuthorization(version: "30900"))

        XCTAssertEqual(defaults.object(forKey: "SUAutomaticallyUpdate") as? Bool, false)
        XCTAssertTrue(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: packaged).automaticInstalls)

        defaults.set(false, forKey: "SUAutomaticallyUpdate")
        XCTAssertFalse(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: packaged).automaticInstalls)
    }

    func testNothingSkippedRememberedOrFailedReadsAsNothing() {
        let diagnostics = UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil)

        XCTAssertNil(diagnostics.skippedVersion)
        XCTAssertEqual(diagnostics.memory, .none)
        XCTAssertNil(diagnostics.lastFailure)
    }

    func testTheLastCheckIsSparklesSavedTime() {
        XCTAssertNil(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil).lastCheck)

        let checked = Date(timeIntervalSince1970: 1_779_790_000)
        defaults.set(checked, forKey: "SULastCheckTime")

        XCTAssertEqual(UpdateCheckDiagnostics.read(defaults: defaults, domain: suiteName, info: nil).lastCheck, checked)
    }
}
