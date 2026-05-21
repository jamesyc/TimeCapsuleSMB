import AppKit
import SwiftUI
import TimeCapsuleSMBApp

@main
struct TimeCapsuleSMBExecutable: App {
    init() {
        NSApplication.shared.setActivationPolicy(.regular)
        DispatchQueue.main.async {
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
