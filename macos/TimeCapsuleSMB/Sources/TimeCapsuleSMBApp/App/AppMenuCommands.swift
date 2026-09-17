import Foundation

/// Entry points for application-menu commands defined in the executable target.
public enum AppMenuCommands {
    public static var checkForUpdatesTitle: String {
        L10n.string("menu.check_for_updates")
    }

    /// Asks the running `AppStore` to perform a manual update check.
    public static func requestCheckForUpdates() {
        NotificationCenter.default.post(name: .tcapsuleCheckForUpdates, object: nil)
    }
}
