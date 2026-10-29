import AppKit
import Darwin
import SwiftUI
import TimeCapsuleSMBApp

@main
struct TimeCapsuleSMBExecutable: App {
    @NSApplicationDelegateAdaptor(AppCloseGuardApplicationDelegate.self) private var appCloseGuardDelegate
    // A plain stored property, not a StateObject: init() starts it, and a
    // StateObject read before SwiftUI installs it is a throwaway copy that
    // would leave the installed updater unstarted. The App struct is made once.
    private let updater = AppUpdater()

    init() {
        if CommandLine.arguments.contains("--validate-resources") {
            if let error = AppLaunchResourceValidation.validate() {
                fputs("\(error)\n", stderr)
                exit(70)
            }
            print("ok")
            exit(0)
        }

        updater.start(ifConfiguredIn: .main)
        NSApplication.shared.setActivationPolicy(.regular)
        DispatchQueue.main.async {
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView(updater: updater)
        }
        .commands {
            CommandGroup(after: .appInfo) {
                CheckForUpdatesCommand(updater: updater)
            }
        }
    }
}
