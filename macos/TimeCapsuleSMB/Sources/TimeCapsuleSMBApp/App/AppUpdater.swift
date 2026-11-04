import Combine
import Foundation
import os
import Sparkle
import SwiftUI

/// What the app needs from Sparkle. The app's own decisions (when to start,
/// what the menu shows, what the toggle writes) are tested against a fake.
protocol AppUpdaterDriver: AnyObject {
    var canCheckForUpdatesPublisher: AnyPublisher<Bool, Never> { get }
    var automaticallyChecksForUpdates: Bool { get set }
    func startUpdater()
    func checkForUpdates()
}

/// Sparkle's callback arguments as `UpdateFlow` events. These are the only
/// decisions on the Sparkle side of the flow, kept apart from the driver so a
/// test can make them without a live updater.
enum SparkleEvents {
    static func window(stage: SPUUserUpdateStage, version: String) -> UpdateWindow {
        switch stage {
        case .notDownloaded:
            UpdateWindow(stage: .notDownloaded, version: version)
        case .downloaded:
            UpdateWindow(stage: .downloaded, version: version)
        case .installing:
            UpdateWindow(stage: .installing, version: version)
        @unknown default:
            UpdateWindow(stage: .notDownloaded, version: version)
        }
    }

    /// `handled` is Sparkle's own answer: true when it showed the window for a
    /// user-initiated check, false when it left a scheduled one to the flow.
    static func windowEvent(handled: Bool, window: UpdateWindow, conditions: UpdateWindowConditions) -> UpdateFlow.Event {
        handled ? .windowShown(window) : .windowWaiting(window, conditions)
    }

    /// Install and Relaunch reports itself as `relaunchRequested`, and a
    /// dismiss at any other stage is Remind Me Later, which ends the session.
    static func choiceEvent(_ choice: SPUUserUpdateChoice, stage: SPUUserUpdateStage, version: String) -> UpdateFlow.Event? {
        switch choice {
        case .skip:
            .updateSkipped
        case .dismiss where stage == .installing:
            .installerKept(version: version)
        default:
            nil
        }
    }

    /// Declining authorization, or being sent to ask for it, is not a failure.
    static func cycleEvent(error: Error?) -> UpdateFlow.Event {
        guard let error else {
            return .cycleFinished(.finished)
        }
        let code = (error as NSError).code
        if code == Int(SUError.noUpdateError.rawValue) {
            return .cycleFinished(.noUpdateFound)
        }
        let normalEnds = [SUError.installationCanceledError, .installationAuthorizeLaterError]
        if normalEnds.contains(where: { Int($0.rawValue) == code }) {
            return .cycleFinished(.finished)
        }
        return .cycleFinished(.failed(error.localizedDescription))
    }
}

/// Forwards Sparkle's callbacks to `UpdateFlow`. All of them arrive on the main
/// thread, and every decision is the flow's or `SparkleEvents`'.
final class SparkleUpdaterDriver: NSObject, AppUpdaterDriver, SPUUpdaterDelegate, SPUStandardUserDriverDelegate {
    private static let updateWindowRecheckSeconds: TimeInterval = 5
    private static let log = Logger(subsystem: "com.timecapsulesmb.TimeCapsuleSMB", category: "updates")

    private let store: UpdateMemoryStore
    private let installedVersion: String

    private lazy var controller = SPUStandardUpdaterController(
        startingUpdater: false,
        updaterDelegate: self,
        userDriverDelegate: self
    )

    private lazy var machine = UpdateFlowMachine(effects: .init(
        checkAgain: { [weak self] in self?.controller.updater.checkForUpdatesInBackground() },
        openWindow: { [weak self] in self?.controller.updater.checkForUpdates() },
        sessionInProgress: { [weak self] in self?.controller.updater.sessionInProgress ?? false },
        startRecheck: { action in
            Timer.publish(every: Self.updateWindowRecheckSeconds, tolerance: 1, on: .main, in: .common)
                .autoconnect()
                .sink { _ in action() }
        },
        remember: { [store] in store.remember($0) },
        recordFailure: { [store, installedVersion] message in
            store.lastFailure = message.map { UpdateFailure(message: $0, date: Date(), appVersion: installedVersion) }
        },
        conditions: { MainActor.assumeIsolated { UpdateWindowConditions.current } },
        now: Date.init,
        report: { transition, status in
            Self.log(transition)
            MainActor.assumeIsolated { UpdateFlowBoard.status = status }
        }
    ))

    init(
        defaults: UserDefaults = .standard,
        installedVersion: String = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? ""
    ) {
        store = UpdateMemoryStore(defaults: defaults)
        self.installedVersion = installedVersion
        store.forgetFailureFromOlderVersion(installedVersion: installedVersion)
        store.askBeforeDownloadingThisLaunch(installedVersion: installedVersion)
        super.init()
    }

    /// Notices stay in the system log, so a user's Mac can be read afterwards.
    private static func log(_ transition: UpdateFlow.Transition) {
        let from = transition.from.detail
        let to = transition.to.detail
        let event = String(describing: transition.event)
        switch transition.outcome {
        case .corrected:
            log.fault("\(String(describing: transition.outcome), privacy: .public) \(event, privacy: .public): \(from, privacy: .public) -> \(to, privacy: .public)")
        case .expected where transition.from.name != transition.to.name:
            log.notice("\(event, privacy: .public): \(from, privacy: .public) -> \(to, privacy: .public)")
        case .expected, .stale:
            break
        }
    }

    var canCheckForUpdatesPublisher: AnyPublisher<Bool, Never> {
        controller.updater.publisher(for: \.canCheckForUpdates).eraseToAnyPublisher()
    }

    var automaticallyChecksForUpdates: Bool {
        get { controller.updater.automaticallyChecksForUpdates }
        set { controller.updater.automaticallyChecksForUpdates = newValue }
    }

    func startUpdater() {
        controller.startUpdater()
    }

    func checkForUpdates() {
        controller.checkForUpdates(nil)
    }

    func updater(_ updater: SPUUpdater, willDownloadUpdate item: SUAppcastItem, with request: NSMutableURLRequest) {
        machine.send(.downloadStarted(version: item.versionString))
    }

    /// Returning false leaves the install to Sparkle, which runs it when the
    /// app quits.
    func updater(
        _ updater: SPUUpdater,
        willInstallUpdateOnQuit item: SUAppcastItem,
        immediateInstallationBlock immediateInstallHandler: @escaping () -> Void
    ) -> Bool {
        machine.send(.installerPrepared(version: item.versionString))
        return false
    }

    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        machine.send(SparkleEvents.cycleEvent(error: error))
    }

    func updater(
        _ updater: SPUUpdater,
        userDidMake choice: SPUUserUpdateChoice,
        forUpdate updateItem: SUAppcastItem,
        state: SPUUserUpdateState
    ) {
        if let event = SparkleEvents.choiceEvent(choice, stage: state.stage, version: updateItem.versionString) {
            machine.send(event)
        }
    }

    func updaterWillRelaunchApplication(_ updater: SPUUpdater) {
        machine.send(.relaunchRequested)
    }

    var supportsGentleScheduledUpdateReminders: Bool {
        true
    }

    /// Every scheduled window waits for the flow. Sparkle's own rule opens one
    /// near launch, when the system has been idle, or when the app next becomes
    /// active, whatever the user is doing.
    func standardUserDriverShouldHandleShowingScheduledUpdate(
        _ update: SUAppcastItem,
        andInImmediateFocus immediateFocus: Bool
    ) -> Bool {
        false
    }

    func standardUserDriverWillHandleShowingUpdate(
        _ handleShowingUpdate: Bool,
        forUpdate update: SUAppcastItem,
        state: SPUUserUpdateState
    ) {
        machine.send(SparkleEvents.windowEvent(
            handled: handleShowingUpdate,
            window: SparkleEvents.window(stage: state.stage, version: update.versionString),
            conditions: machine.conditions
        ))
    }

    /// How Check for Updates... opening a held window reaches the flow.
    func standardUserDriverDidReceiveUserAttention(forUpdate update: SUAppcastItem) {
        machine.send(.windowConfirmed)
    }

    func standardUserDriverWillFinishUpdateSession() {
        machine.send(.sessionEnded)
    }
}

/// The app's Sparkle updater. The executable owns the one instance and
/// starts it once at launch.
@MainActor
public final class AppUpdater: ObservableObject {
    @Published public private(set) var canCheckForUpdates = false

    private let driver: AppUpdaterDriver
    private let defaults: UserDefaults
    private var started = false

    public convenience init() {
        self.init(driver: SparkleUpdaterDriver())
    }

    init(driver: AppUpdaterDriver, defaults: UserDefaults = .standard) {
        self.driver = driver
        self.defaults = defaults
        driver.canCheckForUpdatesPublisher.assign(to: &$canCheckForUpdates)
    }

    /// Sparkle refuses to start, and shows an alert, without a feed or a
    /// public key: a test run, an Xcode run of the package, or any build
    /// that did not come from packaging must never start it.
    static func isConfigured(_ info: [String: Any]?) -> Bool {
        ["SUFeedURL", "SUPublicEDKey"].allSatisfy { key in
            guard let value = info?[key] as? String else {
                return false
            }
            return !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    /// Starts Sparkle the first time it is called with a configured bundle.
    /// Sparkle's updater may be started only once.
    public func start(ifConfiguredIn bundle: Bundle) {
        guard !started, Self.isConfigured(bundle.infoDictionary) else {
            return
        }
        started = true
        // Sparkle has no check at launch, only one a day counted from the last
        // check: without a last check it runs one as soon as it starts. Left
        // alone when checks are off, since no new check would replace it.
        if driver.automaticallyChecksForUpdates {
            defaults.removeObject(forKey: "SULastCheckTime")
        }
        driver.startUpdater()
    }

    func checkForUpdates() {
        driver.checkForUpdates()
    }

    /// What the Settings toggle binds to.
    var automaticallyChecksForUpdates: Bool {
        get { driver.automaticallyChecksForUpdates }
        set {
            objectWillChange.send()
            driver.automaticallyChecksForUpdates = newValue
        }
    }
}

/// The app menu's Check for Updates item. It observes the updater and the
/// in-app language itself: a Scene's `commands` do not refresh for a change
/// the App struct cannot see, which is why Sparkle's own sample uses a view here.
public struct CheckForUpdatesCommand: View {
    @ObservedObject private var updater: AppUpdater
    @ObservedObject private var languageChanges = AppLanguageChanges.shared

    public init(updater: AppUpdater) {
        self.updater = updater
    }

    public var body: some View {
        Button(L10n.string("app_menu.check_for_updates")) {
            updater.checkForUpdates()
        }
        .disabled(!updater.canCheckForUpdates)
    }
}
