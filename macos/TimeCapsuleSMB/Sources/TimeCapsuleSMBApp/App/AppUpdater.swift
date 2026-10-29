import Combine
import Foundation
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

private final class SparkleUpdaterDriver: AppUpdaterDriver {
    private let controller = SPUStandardUpdaterController(
        startingUpdater: false,
        updaterDelegate: nil,
        userDriverDelegate: nil
    )

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
}

/// The app's Sparkle updater. The executable owns the one instance and
/// starts it once at launch.
@MainActor
public final class AppUpdater: ObservableObject {
    @Published public private(set) var canCheckForUpdates = false

    private let driver: AppUpdaterDriver
    private var started = false

    public convenience init() {
        self.init(driver: SparkleUpdaterDriver())
    }

    init(driver: AppUpdaterDriver) {
        self.driver = driver
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
