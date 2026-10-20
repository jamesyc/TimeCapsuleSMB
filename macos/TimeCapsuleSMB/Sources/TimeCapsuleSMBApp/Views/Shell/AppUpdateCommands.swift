import SwiftUI

struct AppUpdateAction {
    let isChecking: Bool
    let check: () -> Void
}

private struct AppUpdateActionKey: FocusedValueKey {
    typealias Value = AppUpdateAction
}

extension FocusedValues {
    var appUpdateAction: AppUpdateAction? {
        get { self[AppUpdateActionKey.self] }
        set { self[AppUpdateActionKey.self] = newValue }
    }
}

public struct AppUpdateCommands: Commands {
    @FocusedValue(\.appUpdateAction) private var action

    public init() {}

    public var body: some Commands {
        CommandGroup(after: .appInfo) {
            Button(L10n.string("app_update.check_menu")) {
                action?.check()
            }
            .disabled(action == nil || action?.isChecking == true)
        }
    }
}
