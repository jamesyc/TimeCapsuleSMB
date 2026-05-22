import AppKit
import ObjectiveC
import SwiftUI

enum AppCloseGuardRequest: Equatable {
    case windowClose
    case appQuit
}

struct AppCloseGuardPrompt: Equatable {
    let title: String
    let message: String
    let cancelTitle: String
    let confirmTitle: String

    static var activeOperation: AppCloseGuardPrompt {
        AppCloseGuardPrompt(
            title: L10n.string("close_guard.title"),
            message: L10n.string("close_guard.message"),
            cancelTitle: L10n.string("close_guard.keep_open"),
            confirmTitle: L10n.string("close_guard.close_anyway")
        )
    }
}

private struct AppCloseGuardPolicy {
    var hasBlockingActivity: () -> Bool = { false }

    var requiresConfirmation: Bool {
        hasBlockingActivity()
    }
}

@MainActor
protocol AppCloseGuardPresenting: AnyObject {
    func confirmClose(
        _ prompt: AppCloseGuardPrompt,
        for request: AppCloseGuardRequest,
        modalFor window: NSWindow?,
        completion: @escaping @MainActor (Bool) -> Void
    )
}

@MainActor
private final class AppCloseGuardAlertPresenter: AppCloseGuardPresenting {
    func confirmClose(
        _ prompt: AppCloseGuardPrompt,
        for _: AppCloseGuardRequest,
        modalFor window: NSWindow?,
        completion: @escaping @MainActor (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = prompt.title
        alert.informativeText = prompt.message
        alert.addButton(withTitle: prompt.cancelTitle)
        alert.addButton(withTitle: prompt.confirmTitle)

        if let window, window.isVisible {
            alert.beginSheetModal(for: window) { response in
                Task { @MainActor in
                    completion(response == .alertSecondButtonReturn)
                }
            }
            return
        }

        DispatchQueue.main.async {
            let response = alert.runModal()
            Task { @MainActor in
                completion(response == .alertSecondButtonReturn)
            }
        }
    }
}

@MainActor
public final class AppCloseGuard: NSObject {
    public static let shared = AppCloseGuard()

    var presenter: AppCloseGuardPresenting = AppCloseGuardAlertPresenter()

    private var policy = AppCloseGuardPolicy()
    private var authorizedWindowCloses: Set<ObjectIdentifier> = []

    public func configure(hasBlockingActivity: @escaping () -> Bool) {
        policy = AppCloseGuardPolicy(hasBlockingActivity: hasBlockingActivity)
    }

    func shouldCloseWindow(_ window: NSWindow) -> Bool {
        guard policy.requiresConfirmation else {
            return true
        }
        presenter.confirmClose(
            AppCloseGuardPrompt.activeOperation,
            for: .windowClose,
            modalFor: window
        ) { [weak self, weak window] confirmed in
            guard confirmed, let window else {
                return
            }
            self?.authorizeNextClose(of: window)
            window.performClose(nil)
        }
        return false
    }

    func shouldTerminateApplication(_ application: NSApplication) -> NSApplication.TerminateReply {
        guard policy.requiresConfirmation else {
            return .terminateNow
        }
        presenter.confirmClose(
            AppCloseGuardPrompt.activeOperation,
            for: .appQuit,
            modalFor: application.keyWindow ?? application.mainWindow
        ) { confirmed in
            application.reply(toApplicationShouldTerminate: confirmed)
        }
        return .terminateLater
    }

    func attach(to window: NSWindow) {
        if objc_getAssociatedObject(window, &windowCloseGuardDelegateKey) is GuardedWindowDelegate {
            return
        }
        let delegate = GuardedWindowDelegate(downstream: window.delegate)
        objc_setAssociatedObject(window, &windowCloseGuardDelegateKey, delegate, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        window.delegate = delegate
    }

    func consumeAuthorizedClose(of window: NSWindow) -> Bool {
        authorizedWindowCloses.remove(ObjectIdentifier(window)) != nil
    }

    private func authorizeNextClose(of window: NSWindow) {
        authorizedWindowCloses.insert(ObjectIdentifier(window))
    }
}

@MainActor
public final class AppCloseGuardApplicationDelegate: NSObject, NSApplicationDelegate {
    var closeGuard: AppCloseGuard = .shared

    public override init() {
        super.init()
    }

    public func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        closeGuard.shouldTerminateApplication(sender)
    }
}

private var windowCloseGuardDelegateKey: UInt8 = 0

private final class GuardedWindowDelegate: NSObject, NSWindowDelegate {
    private weak var downstream: NSWindowDelegate?

    init(downstream: NSWindowDelegate?) {
        self.downstream = downstream
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        let alreadyConfirmed = AppCloseGuard.shared.consumeAuthorizedClose(of: sender)
        if let downstreamAllows = downstream?.windowShouldClose?(sender), !downstreamAllows {
            return false
        }
        if alreadyConfirmed {
            return true
        }
        return AppCloseGuard.shared.shouldCloseWindow(sender)
    }

    func windowWillClose(_ notification: Notification) {
        downstream?.windowWillClose?(notification)
    }
}

struct WindowCloseGuardInstaller: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        GuardedWindowAnchorView()
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        guard let window = nsView.window else {
            return
        }
        AppCloseGuard.shared.attach(to: window)
    }

    private final class GuardedWindowAnchorView: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window else {
                return
            }
            AppCloseGuard.shared.attach(to: window)
        }
    }
}
