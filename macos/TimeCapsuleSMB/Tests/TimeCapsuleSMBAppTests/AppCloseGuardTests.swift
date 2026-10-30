import AppKit
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppCloseGuardTests: XCTestCase {
    func testCloseGuardAllowsWindowCloseWithoutPromptWhenNoOperationIsActive() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { false }
        guardController.presenter = presenter
        let window = NSWindow()

        XCTAssertTrue(guardController.shouldCloseWindow(window))
        XCTAssertTrue(presenter.requests.isEmpty)
    }

    func testCloseGuardRequiresSharedConfirmationWhenOperationIsActive() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { true }
        guardController.presenter = presenter
        let window = NSWindow()

        XCTAssertFalse(guardController.shouldCloseWindow(window))
        XCTAssertEqual(presenter.requests, [.windowClose])
        XCTAssertEqual(presenter.prompts, [.activeOperation])
        XCTAssertEqual(presenter.windows, [window])

        let delegate = AppCloseGuardApplicationDelegate()
        delegate.closeGuard = guardController

        XCTAssertEqual(delegate.applicationShouldTerminate(.shared), .terminateLater)
        XCTAssertEqual(presenter.requests, [.windowClose, .appQuit])
        XCTAssertEqual(presenter.prompts, [.activeOperation, .activeOperation])
    }

    func testConfirmedWindowCloseClosesWindowDirectly() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { true }
        guardController.presenter = presenter
        let window = RecordingWindow()

        XCTAssertFalse(guardController.shouldCloseWindow(window))

        presenter.completions.first?(true)

        XCTAssertEqual(window.closeCount, 1)
    }

    func testApplicationDelegateRoutesCommandQuitThroughCloseGuard() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { true }
        guardController.presenter = presenter
        let delegate = AppCloseGuardApplicationDelegate()
        delegate.closeGuard = guardController

        XCTAssertEqual(delegate.applicationShouldTerminate(.shared), .terminateLater)
        XCTAssertEqual(presenter.requests, [.appQuit])
        XCTAssertEqual(presenter.prompts, [.activeOperation])
    }

    func testApplicationDelegateAllowsCommandQuitWithoutPromptWhenNoOperationIsActive() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { false }
        guardController.presenter = presenter
        let delegate = AppCloseGuardApplicationDelegate()
        delegate.closeGuard = guardController

        XCTAssertEqual(delegate.applicationShouldTerminate(.shared), .terminateNow)
        XCTAssertTrue(presenter.requests.isEmpty)
    }

    func testQuittingOrClosingDuringOnlyTheSSHStatusCheckNeedsNoPrompt() async throws {
        let result = BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
        // Separate keys: finishing a key opens its gate for good.
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh", profileID: "device-one"): [.init(events: [result], pauseBeforeEvents: true)],
            .init("set-ssh"): [.init(events: [result], pauseBeforeEvents: true)]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { coordinator.hasBlockingWork }
        guardController.presenter = presenter
        let delegate = AppCloseGuardApplicationDelegate()
        delegate.closeGuard = guardController

        DeviceSSHAccessStore(coordinator: coordinator).refresh(profile: profile)
        XCTAssertTrue(coordinator.lane(for: .deviceWorkflow(profile.id, .sshStatus)).isBusy)
        XCTAssertEqual(delegate.applicationShouldTerminate(.shared), .terminateNow)
        XCTAssertTrue(guardController.shouldCloseWindow(NSWindow()))
        XCTAssertTrue(presenter.requests.isEmpty)
        // A gate exists only once its call is recorded; finishing earlier would be lost.
        try await waitUntilStoreState { runner.calls.count == 1 }
        runner.finish(.init("set-ssh", profileID: "device-one"))
        try await waitUntilStoreState { !coordinator.lane(for: .deviceWorkflow(profile.id, .sshStatus)).isBusy }

        _ = coordinator.run(
            operation: "set-ssh",
            params: OperationParams.SetSSH.enable(noWait: false),
            context: profile.runtimeContext,
            activeDeviceID: profile.id,
            password: "pw",
            laneKey: .deviceWorkflow(profile.id, .sshAccess)
        )
        XCTAssertEqual(delegate.applicationShouldTerminate(.shared), .terminateLater)
        XCTAssertFalse(guardController.shouldCloseWindow(NSWindow()))
        XCTAssertEqual(presenter.requests, [.appQuit, .windowClose])
        try await waitUntilStoreState { runner.calls.count == 2 }
        runner.finish(.init("set-ssh"))
        try await waitUntilStoreState { !coordinator.isDeviceBusy(profile.id) }
    }

    func testAttachedWindowDelegateForwardsUninterceptedCallbacks() {
        let guardController = AppCloseGuard()
        let downstream = RecordingWindowDelegate()
        let window = NSWindow()
        window.delegate = downstream

        guardController.attach(to: window)
        let notification = Notification(name: NSWindow.didResizeNotification, object: window)
        XCTAssertTrue(window.delegate?.responds(to: #selector(NSWindowDelegate.windowDidResize(_:))) ?? false)

        window.delegate?.windowDidResize?(notification)

        XCTAssertEqual(downstream.resizeCount, 1)
    }

    func testAttachedWindowDelegateUsesConfiguredCloseGuardAndRewrapsReplacementDelegate() {
        let guardController = AppCloseGuard()
        let presenter = RecordingCloseGuardPresenter()
        guardController.configure { true }
        guardController.presenter = presenter
        let downstream = RecordingWindowDelegate()
        let window = NSWindow()

        guardController.attach(to: window)
        window.delegate = downstream
        guardController.attach(to: window)

        XCTAssertFalse(window.delegate === downstream)
        XCTAssertFalse(window.delegate?.windowShouldClose?(window) ?? true)
        XCTAssertEqual(presenter.requests, [.windowClose])
        XCTAssertEqual(downstream.shouldCloseCount, 1)
    }
}

private final class RecordingWindow: NSWindow {
    private(set) var closeCount = 0

    override func close() {
        closeCount += 1
    }
}

private final class RecordingWindowDelegate: NSObject, NSWindowDelegate {
    private(set) var resizeCount = 0
    private(set) var shouldCloseCount = 0

    func windowDidResize(_ notification: Notification) {
        resizeCount += 1
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        shouldCloseCount += 1
        return true
    }
}

@MainActor
private final class RecordingCloseGuardPresenter: AppCloseGuardPresenting {
    private(set) var prompts: [AppCloseGuardPrompt] = []
    private(set) var requests: [AppCloseGuardRequest] = []
    private(set) var windows: [NSWindow?] = []
    private(set) var completions: [@MainActor (Bool) -> Void] = []

    func confirmClose(
        _ prompt: AppCloseGuardPrompt,
        for request: AppCloseGuardRequest,
        modalFor window: NSWindow?,
        completion: @escaping @MainActor (Bool) -> Void
    ) {
        prompts.append(prompt)
        requests.append(request)
        windows.append(window)
        completions.append(completion)
    }
}
