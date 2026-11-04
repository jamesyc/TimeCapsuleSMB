import Combine
import XCTest
@testable import TimeCapsuleSMBApp

private let closed = UpdateWindowConditions(appIsActive: false, hasBlockingWork: false, isTyping: false, hasModal: false)
private let free = UpdateWindowConditions(appIsActive: true, hasBlockingWork: false, isTyping: false, hasModal: false)
private let start = Date(timeIntervalSince1970: 1_000_000)
private let updateVersion = "30803"
private let promptWindow = UpdateWindow(stage: .notDownloaded, version: updateVersion)
private let downloadedWindow = UpdateWindow(stage: .downloaded, version: updateVersion)
private let installerWindow = UpdateWindow(stage: .installing, version: updateVersion)

private let events: [(name: String, event: UpdateFlow.Event)] = [
    ("started", .downloadStarted(version: updateVersion)),
    ("prepared", .installerPrepared(version: updateVersion)),
    ("cycle", .cycleFinished(.finished)),
    ("cycleNoUpdate", .cycleFinished(.noUpdateFound)),
    ("cycleFailed", .cycleFinished(.failed("broken"))),
    ("waitingPrompt", .windowWaiting(promptWindow, closed)),
    ("waitingDownloaded", .windowWaiting(downloadedWindow, closed)),
    ("waitingInstaller", .windowWaiting(installerWindow, closed)),
    ("waitingFree", .windowWaiting(installerWindow, free)),
    ("shown", .windowShown(installerWindow)),
    ("confirmed", .windowConfirmed),
    ("kept", .installerKept(version: updateVersion)),
    ("skipped", .updateSkipped),
    ("relaunch", .relaunchRequested),
    ("recheckClosed", .recheck(closed)),
    ("recheckFree", .recheck(free)),
    ("ended", .sessionEnded),
]

/// The legal route to each phase, so every phase is also proved reachable.
private let routes: [(phase: String, events: [String])] = [
    ("idle", []),
    ("downloading", ["started"]),
    ("installerReady", ["started", "prepared"]),
    ("reprobing", ["started", "prepared", "cycle"]),
    ("windowHeld", ["waitingInstaller"]),
    ("windowOpen", ["shown"]),
    ("installerWaiting", ["shown", "kept"]),
]

/// Every phase against every event: event, next phase, outcome, actions. The
/// window phases here show an installer; other stages have their own tests.
private let table: [String: String] = [
    "idle": """
    started downloading expected rememberDownload
    prepared installerReady corrected forgetMemory clearFailure
    cycle idle expected
    cycleNoUpdate idle expected
    cycleFailed idle expected recordFailure
    waitingPrompt windowHeld expected startRecheck
    waitingDownloaded windowHeld expected startRecheck
    waitingInstaller windowHeld expected startRecheck
    waitingFree windowOpen expected openWindow
    shown windowOpen expected
    confirmed idle stale
    kept installerWaiting corrected
    skipped idle stale
    relaunch idle stale
    recheckClosed idle stale
    recheckFree idle stale
    ended idle expected
    """,
    "downloading": """
    started downloading corrected rememberDownload
    prepared installerReady expected forgetMemory clearFailure
    cycle idle expected
    cycleNoUpdate idle expected
    cycleFailed idle expected recordFailure
    waitingPrompt windowHeld expected startRecheck
    waitingDownloaded windowHeld expected startRecheck rememberNeedsAuthorization
    waitingInstaller windowHeld expected startRecheck forgetMemory
    waitingFree windowOpen expected forgetMemory openWindow
    shown windowOpen corrected forgetMemory
    confirmed downloading stale
    kept installerWaiting corrected forgetMemory
    skipped idle corrected
    relaunch downloading stale
    recheckClosed downloading stale
    recheckFree downloading stale
    ended downloading stale
    """,
    "installerReady": """
    started downloading corrected rememberDownload
    prepared installerReady stale
    cycle reprobing expected checkAgain
    cycleNoUpdate reprobing expected checkAgain
    cycleFailed reprobing expected checkAgain recordFailure
    waitingPrompt windowHeld expected startRecheck
    waitingDownloaded windowHeld expected startRecheck
    waitingInstaller windowHeld expected startRecheck
    waitingFree windowOpen expected openWindow
    shown windowOpen corrected
    confirmed installerReady stale
    kept installerWaiting corrected
    skipped idle corrected
    relaunch installerReady stale
    recheckClosed installerReady stale
    recheckFree installerReady stale
    ended installerReady stale
    """,
    "reprobing": """
    started downloading expected rememberDownload
    prepared installerReady corrected forgetMemory clearFailure
    cycle installerWaiting expected
    cycleNoUpdate idle corrected
    cycleFailed installerWaiting expected recordFailure
    waitingPrompt windowHeld expected startRecheck
    waitingDownloaded windowHeld expected startRecheck
    waitingInstaller windowHeld expected startRecheck
    waitingFree windowOpen expected openWindow
    shown windowOpen corrected
    confirmed reprobing stale
    kept installerWaiting corrected
    skipped idle corrected
    relaunch reprobing stale
    recheckClosed reprobing stale
    recheckFree reprobing stale
    ended reprobing stale
    """,
    "windowHeld": """
    started windowHeld corrected
    prepared windowHeld corrected
    cycle installerWaiting corrected stopRecheck
    cycleNoUpdate installerWaiting corrected stopRecheck
    cycleFailed installerWaiting corrected stopRecheck recordFailure
    waitingPrompt windowHeld corrected
    waitingDownloaded windowHeld corrected
    waitingInstaller windowHeld stale
    waitingFree windowHeld stale
    shown windowOpen expected stopRecheck
    confirmed windowOpen expected stopRecheck
    kept installerWaiting corrected stopRecheck
    skipped idle corrected stopRecheck
    relaunch installerWaiting expected stopRecheck
    recheckClosed windowHeld expected
    recheckFree windowOpen expected stopRecheck openWindow
    ended installerWaiting expected stopRecheck
    """,
    "windowOpen": """
    started windowOpen expected
    prepared windowOpen corrected
    cycle installerWaiting corrected
    cycleNoUpdate installerWaiting corrected
    cycleFailed installerWaiting corrected recordFailure
    waitingPrompt windowHeld corrected startRecheck
    waitingDownloaded windowHeld corrected startRecheck
    waitingInstaller windowHeld corrected startRecheck
    waitingFree windowOpen corrected openWindow
    shown windowOpen stale
    confirmed windowOpen stale
    kept installerWaiting expected
    skipped idle expected
    relaunch installerWaiting expected
    recheckClosed windowOpen stale
    recheckFree windowOpen stale
    ended installerWaiting expected
    """,
    "installerWaiting": """
    started downloading expected rememberDownload
    prepared installerReady corrected forgetMemory clearFailure
    cycle installerWaiting stale
    cycleNoUpdate idle corrected
    cycleFailed installerWaiting stale recordFailure
    waitingPrompt windowHeld expected startRecheck
    waitingDownloaded windowHeld expected startRecheck
    waitingInstaller windowHeld expected startRecheck
    waitingFree windowOpen expected openWindow
    shown windowOpen expected
    confirmed installerWaiting stale
    kept installerWaiting stale
    skipped idle corrected
    relaunch installerWaiting stale
    recheckClosed installerWaiting stale
    recheckFree installerWaiting stale
    ended installerWaiting stale
    """,
]

private func event(named name: String) -> UpdateFlow.Event {
    events.first { $0.name == name }!.event
}

private func reachedFlow(_ route: [String]) -> UpdateFlow {
    var flow = UpdateFlow(now: start)
    for name in route {
        _ = flow.handle(event(named: name), at: start)
    }
    return flow
}

private func actionName(_ action: UpdateFlow.Action) -> String {
    switch action {
    case .checkAgain: "checkAgain"
    case .openWindow: "openWindow"
    case .startRecheck: "startRecheck"
    case .stopRecheck: "stopRecheck"
    case .remember(.none): "forgetMemory"
    case .remember(.interrupted): "rememberDownload"
    case .remember(.needsAuthorization): "rememberNeedsAuthorization"
    case .recordFailure: "recordFailure"
    case .clearFailure: "clearFailure"
    }
}

/// Repeatable random numbers, so a failing sequence fails the same way again.
private struct SplitMix: RandomNumberGenerator {
    var state: UInt64

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var value = state
        value = (value ^ (value >> 30)) &* 0xBF58_476D_1CE4_E5B9
        value = (value ^ (value >> 27)) &* 0x94D0_49BB_1331_11EB
        return value ^ (value >> 31)
    }
}

final class UpdateFlowTests: XCTestCase {
    func testEveryPhaseAndEventDoWhatTheTableSays() throws {
        XCTAssertEqual(Set(table.keys), Set(routes.map(\.phase)))
        for route in routes {
            XCTAssertEqual(reachedFlow(route.events).phase.name, route.phase, "route to \(route.phase)")
            let rows = try XCTUnwrap(table[route.phase]).split(separator: "\n").map { $0.split(separator: " ").map(String.init) }
            XCTAssertEqual(rows.map { $0[0] }, events.map(\.name), route.phase)

            for row in rows {
                var flow = reachedFlow(route.events)
                let transition = flow.handle(event(named: row[0]), at: start)
                let pair = "\(route.phase) + \(row[0])"
                XCTAssertEqual(transition.to.name, row[1], pair)
                XCTAssertEqual(String(describing: transition.outcome), row[2], pair)
                XCTAssertEqual(transition.actions.map(actionName), Array(row[3...]), pair)
                for action in transition.actions {
                    switch action {
                    case .remember(.interrupted(let version)), .remember(.needsAuthorization(let version)):
                        XCTAssertEqual(version, updateVersion, pair)
                    default:
                        break
                    }
                }
            }
        }
    }

    func testEveryDownloadRemembersTheVersionItStarted() {
        for route in routes where !route.phase.hasPrefix("window") {
            var flow = reachedFlow(route.events)

            let transition = flow.handle(.downloadStarted(version: "30900"), at: start)

            XCTAssertEqual(transition.to, .downloading(version: "30900"), route.phase)
            XCTAssertEqual(transition.actions, [.remember(.interrupted(version: "30900"))], route.phase)
        }
    }

    func testAPreparedVersionIsTheOneThatWaits() {
        for route in routes where !route.phase.hasPrefix("window") {
            var flow = reachedFlow(route.events)

            XCTAssertEqual(flow.handle(.installerPrepared(version: "30900"), at: start).to.name, "installerReady", route.phase)
            if route.phase != "installerReady" {
                XCTAssertEqual(flow.phase, .installerReady(version: "30900"), route.phase)
            }
        }
    }

    func testAKeptInstallerWaitsForTheVersionTheWindowShowed() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowShown(UpdateWindow(stage: .installing, version: "30900")), at: start)

        XCTAssertEqual(flow.handle(.relaunchRequested, at: start).to, .installerWaiting(version: "30900"))
    }

    func testACheckThatFindsNoUpdateEndsAWaitingInstaller() {
        var flow = reachedFlow(["shown", "kept"])

        let transition = flow.handle(.cycleFinished(.noUpdateFound), at: start)

        XCTAssertEqual(transition.to, .idle)
        XCTAssertEqual(transition.outcome, .corrected)
    }

    func testOtherCycleEndsLeaveAWaitingInstaller() {
        for end in [UpdateFlow.CycleEnd.finished, .failed("offline")] {
            var flow = reachedFlow(["shown", "kept"])

            XCTAssertEqual(flow.handle(.cycleFinished(end), at: start).to, .installerWaiting(version: updateVersion))
        }
    }

    func testAFollowUpCheckThatFindsNoUpdateEndsAtIdle() {
        var flow = reachedFlow(["started", "prepared", "cycle"])

        let transition = flow.handle(.cycleFinished(.noUpdateFound), at: start)

        XCTAssertEqual(transition.to, .idle)
        XCTAssertEqual(transition.outcome, .corrected)
    }

    func testOtherFollowUpCycleEndsLeaveTheInstallerWaiting() {
        for end in [UpdateFlow.CycleEnd.finished, .failed("offline")] {
            var flow = reachedFlow(["started", "prepared", "cycle"])

            XCTAssertEqual(flow.handle(.cycleFinished(end), at: start).to, .installerWaiting(version: updateVersion))
        }
    }

    func testEverySilentStepKeepsTheVersion() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.downloadStarted(version: "30900"), at: start)
        XCTAssertEqual(flow.phase, .downloading(version: "30900"))
        _ = flow.handle(.installerPrepared(version: "30900"), at: start)
        XCTAssertEqual(flow.phase, .installerReady(version: "30900"))
        _ = flow.handle(.cycleFinished(.finished), at: start)
        XCTAssertEqual(flow.phase, .reprobing(version: "30900"))
    }

    func testAFollowUpCheckThatDownloadsAgainIsNotRepeated() {
        var flow = reachedFlow(["started", "prepared", "cycle"])
        var followUps = 0

        for _ in 0..<2 {
            for name in ["started", "prepared", "cycle"] {
                followUps += flow.handle(event(named: name), at: start).actions.filter { $0 == .checkAgain }.count
            }
        }

        XCTAssertEqual(followUps, 0)
        XCTAssertEqual(flow.phase, .installerWaiting(version: updateVersion))
    }

    func testANewerVersionGetsItsOwnFollowUpCheck() {
        var flow = reachedFlow(["started", "prepared", "cycle", "started", "prepared", "cycle"])
        for next in [UpdateFlow.Event.downloadStarted(version: "30900"), .installerPrepared(version: "30900")] {
            _ = flow.handle(next, at: start)
        }

        let transition = flow.handle(.cycleFinished(.finished), at: start)

        XCTAssertEqual(transition.to, .reprobing(version: "30900"))
        XCTAssertEqual(transition.actions, [.checkAgain])
    }

    func testACriticalUpdateNeedsNoFollowUpCheck() {
        var flow = reachedFlow(["started", "prepared"])

        // Sparkle checks again by itself, without ending the cycle first.
        let transition = flow.handle(.windowWaiting(installerWindow, free), at: start)

        XCTAssertEqual(transition.outcome, .expected)
        XCTAssertFalse(transition.actions.contains(.checkAgain))
        XCTAssertEqual(flow.phase, .windowOpen(installerWindow))
    }

    func testADownloadTheUserAskedForLeavesNoMemory() {
        var flow = reachedFlow([])
        _ = flow.handle(.windowShown(promptWindow), at: start)

        let transition = flow.handle(.downloadStarted(version: updateVersion), at: start)

        XCTAssertEqual(transition.actions, [])
        XCTAssertEqual(flow.phase, .windowOpen(promptWindow))
    }

    func testASilentDownloadThatNeedsAuthorizationIsRemembered() {
        var flow = reachedFlow(["started"])

        let transition = flow.handle(.windowWaiting(downloadedWindow, closed), at: start)

        XCTAssertTrue(transition.actions.contains(.remember(.needsAuthorization(version: updateVersion))))
    }

    func testAWindowWithNoInstallerEndsIdle() {
        for window in [promptWindow, downloadedWindow] {
            var held = UpdateFlow(now: start)
            _ = held.handle(.windowWaiting(window, closed), at: start)
            XCTAssertEqual(held.handle(.sessionEnded, at: start).to, .idle)

            var open = UpdateFlow(now: start)
            _ = open.handle(.windowShown(window), at: start)
            XCTAssertEqual(open.handle(.sessionEnded, at: start).to, .idle)
        }
    }

    func testAWindowWithAnInstallerEndsWaiting() {
        var flow = reachedFlow(["shown"])

        XCTAssertEqual(flow.handle(.sessionEnded, at: start).to, .installerWaiting(version: updateVersion))
    }

    func testSkippingCancelsTheInstallerBeforeTheSessionEnds() {
        var flow = reachedFlow(["shown"])

        _ = flow.handle(.updateSkipped, at: start)
        let transition = flow.handle(.sessionEnded, at: start)

        XCTAssertEqual(transition.to, .idle)
    }

    func testACancelledRelaunchFromAPromptWaitsForTheVersionItDownloaded() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowShown(promptWindow), at: start)

        let transition = flow.handle(.relaunchRequested, at: start)

        XCTAssertEqual(transition.to, .installerWaiting(version: updateVersion))
    }

    func testAWindowOpensOnce() {
        var flow = reachedFlow(["waitingInstaller"])
        var opens = 0

        for _ in 0..<10 {
            opens += flow.handle(.recheck(free), at: start).actions.filter { $0 == .openWindow }.count
        }

        XCTAssertEqual(opens, 1)
        XCTAssertEqual(flow.phase, .windowOpen(installerWindow))
    }

    func testTheNextWindowOpensAfterOneThatNeverReachedTheUser() {
        var flow = reachedFlow(["waitingInstaller"])
        _ = flow.handle(.recheck(free), at: start)

        let transition = flow.handle(.windowWaiting(installerWindow, free), at: start)

        XCTAssertEqual(transition.outcome, .corrected)
        XCTAssertEqual(transition.actions, [.openWindow])
    }

    func testTheNextWindowCanBeHeldAfterOneThatNeverReachedTheUser() {
        var flow = reachedFlow(["waitingInstaller"])
        _ = flow.handle(.recheck(free), at: start)

        let transition = flow.handle(.windowWaiting(installerWindow, closed), at: start)

        XCTAssertEqual(transition.actions, [.startRecheck])
        XCTAssertEqual(flow.phase, .windowHeld(installerWindow))
    }

    func testADifferentWindowReplacesTheHeldOne() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowWaiting(promptWindow, closed), at: start)

        let transition = flow.handle(.windowWaiting(installerWindow, closed), at: start)

        XCTAssertEqual(transition.outcome, .corrected)
        XCTAssertEqual(transition.actions, [])
        XCTAssertEqual(flow.phase, .windowHeld(installerWindow))
        XCTAssertEqual(flow.handle(.sessionEnded, at: start).to, .installerWaiting(version: updateVersion))
    }

    func testADifferentShownWindowReplacesTheOpenOne() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowShown(promptWindow), at: start)

        let transition = flow.handle(.windowShown(installerWindow), at: start)

        XCTAssertEqual(transition.outcome, .corrected)
        XCTAssertEqual(flow.phase, .windowOpen(installerWindow))
        XCTAssertEqual(flow.handle(.sessionEnded, at: start).to, .installerWaiting(version: updateVersion))
    }

    func testAttentionOpensTheWindowThatWasHeld() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowWaiting(promptWindow, closed), at: start)

        XCTAssertEqual(flow.handle(.windowConfirmed, at: start).to, .windowOpen(promptWindow))
    }

    func testEachConditionHoldsTheWindow() {
        var behindOtherApps = free
        behindOtherApps.appIsActive = false
        var busy = free
        busy.hasBlockingWork = true
        var typing = free
        typing.isTyping = true
        var confirming = free
        confirming.hasModal = true

        XCTAssertTrue(free.mayOpen)
        for conditions in [behindOtherApps, busy, typing, confirming] {
            XCTAssertFalse(conditions.mayOpen, "\(conditions)")
        }
    }

    func testTheStatusAgesAfterTheLastEvent() {
        var flow = UpdateFlow(now: start)
        _ = flow.handle(.windowWaiting(installerWindow, closed), at: start)
        let status = flow.status

        XCTAssertEqual(status.summary(at: start), "windowHeld installing \(updateVersion) for 0 min")
        XCTAssertEqual(status.summary(at: start.addingTimeInterval(720)), "windowHeld installing \(updateVersion) for 12 min")
    }

    func testTheStatusCountsEveryKindOfDrift() {
        var flow = reachedFlow(["waitingInstaller"])
        _ = flow.handle(.downloadStarted(version: updateVersion), at: start)
        _ = flow.handle(.installerKept(version: updateVersion), at: start)

        XCTAssertEqual(flow.status.summary(at: start), "installerWaiting \(updateVersion) for 0 min, 2 drift")
    }

    /// Whatever order Sparkle's callbacks arrive in, the rules stay true.
    func testRandomEventOrdersKeepEveryInvariant() {
        var generator = SplitMix(state: 0x414)
        let otherVersion = "30900"
        var pool = events.map(\.event)
        pool += [
            .downloadStarted(version: otherVersion),
            .installerPrepared(version: otherVersion),
            .windowWaiting(promptWindow, free),
            .windowShown(promptWindow),
            .windowShown(downloadedWindow),
        ]

        for sequence in 0..<2_000 {
            var flow = UpdateFlow(now: start)
            var timerRunning = false
            var checkedAgain: Set<String> = []
            var provenInstallers: Set<String> = []
            var now = start

            for step in 0..<60 {
                now += TimeInterval.random(in: 0...120, using: &generator)
                let next = pool.randomElement(using: &generator)!
                let transition = flow.handle(next, at: now)
                let place = "sequence \(sequence) step \(step): \(transition.from.name) + \(next)"

                switch next {
                case .installerPrepared(let version), .installerKept(let version):
                    provenInstallers.insert(version)
                case .windowWaiting(let window, _), .windowShown(let window):
                    if window.stage == .installing {
                        provenInstallers.insert(window.version)
                    }
                case .relaunchRequested:
                    // Sparkle relaunches only with an installer running.
                    if case .installerWaiting(let version) = transition.to {
                        provenInstallers.insert(version)
                    }
                default:
                    break
                }

                for action in transition.actions {
                    switch action {
                    case .startRecheck:
                        XCTAssertFalse(timerRunning, place)
                        timerRunning = true
                    case .stopRecheck:
                        XCTAssertTrue(timerRunning, place)
                        timerRunning = false
                    case .checkAgain:
                        guard case .installerReady(let version) = transition.from else {
                            return XCTFail("check outside installerReady: \(place)")
                        }
                        XCTAssertTrue(checkedAgain.insert(version).inserted, place)
                    case .openWindow:
                        switch next {
                        case .windowWaiting, .recheck:
                            break
                        default:
                            XCTFail("window opened by another event: \(place)")
                        }
                        XCTAssertEqual(transition.to.name, "windowOpen", place)
                    case .remember(.none):
                        let installerIsReady: Bool
                        switch next {
                        case .installerPrepared, .installerKept:
                            installerIsReady = true
                        case .windowWaiting(let window, _), .windowShown(let window):
                            installerIsReady = window.stage == .installing
                        default:
                            installerIsReady = false
                        }
                        XCTAssertTrue(installerIsReady, place)
                    case .remember(.interrupted):
                        guard case .downloading = transition.to else {
                            return XCTFail("interrupted outside downloading: \(place)")
                        }
                    case .remember(.needsAuthorization), .recordFailure, .clearFailure:
                        break
                    }
                }

                XCTAssertEqual(timerRunning, flow.phase.isHeld, place)
                if case .installerWaiting(let version) = flow.phase {
                    XCTAssertTrue(provenInstallers.contains(version), place)
                }
                if case .cycleFinished = next {
                    XCTAssertTrue(["idle", "reprobing", "installerWaiting"].contains(flow.phase.name), place)
                }
                if next == .cycleFinished(.noUpdateFound), ["reprobing", "installerWaiting"].contains(transition.from.name) {
                    XCTAssertEqual(flow.phase, .idle, place)
                }
                if transition.outcome == .stale {
                    XCTAssertEqual(transition.from, transition.to, place)
                    XCTAssertEqual(transition.actions.filter { actionName($0) != "recordFailure" }, [], place)
                }
            }
        }
    }
}

/// Runs the machine against recorded effects and a recheck the test fires.
private final class MachineFixture {
    var conditions = closed
    var sessionInProgress = true
    var onOpenWindow: () -> Void = {}
    private(set) var effects: [String] = []
    private(set) var memory = UpdateMemory.none
    private(set) var failure: String?
    private(set) var reports: [UpdateFlow.Transition] = []
    private(set) var status: UpdateFlowStatus?
    private(set) var runningRechecks = 0
    private var fireRecheck: (() -> Void)?

    private(set) lazy var machine = UpdateFlowMachine(effects: .init(
        checkAgain: { [unowned self] in effects.append("checkAgain") },
        openWindow: { [unowned self] in
            effects.append("openWindow")
            onOpenWindow()
        },
        sessionInProgress: { [unowned self] in sessionInProgress },
        startRecheck: { [unowned self] action in
            runningRechecks += 1
            fireRecheck = action
            return AnyCancellable { [weak self] in
                self?.runningRechecks -= 1
                self?.fireRecheck = nil
            }
        },
        remember: { [unowned self] in
            effects.append("remember")
            memory = $0
        },
        recordFailure: { [unowned self] in
            effects.append("recordFailure")
            failure = $0
        },
        conditions: { [unowned self] in conditions },
        now: { start },
        report: { [unowned self] in
            reports.append($0)
            status = $1
        }
    ))

    func recheck() {
        fireRecheck?()
    }

    func wait(window: UpdateWindow = installerWindow) {
        machine.send(.windowWaiting(window, machine.conditions))
    }
}

final class UpdateFlowMachineTests: XCTestCase {
    func testASilentUpdateRunsEachEffectInOrder() {
        let fixture = MachineFixture()
        fixture.conditions = free

        fixture.machine.send(.downloadStarted(version: updateVersion))
        fixture.machine.send(.installerPrepared(version: updateVersion))
        fixture.machine.send(.cycleFinished(.finished))
        fixture.wait()

        XCTAssertEqual(fixture.effects, ["remember", "remember", "recordFailure", "checkAgain", "openWindow"])
        XCTAssertEqual(fixture.memory, .none)
        XCTAssertEqual(fixture.machine.flow.phase, .windowOpen(installerWindow))
        XCTAssertEqual(fixture.runningRechecks, 0)
    }

    func testAnInterruptedDownloadStaysRemembered() {
        let fixture = MachineFixture()

        fixture.machine.send(.downloadStarted(version: updateVersion))

        XCTAssertEqual(fixture.memory, .interrupted(version: updateVersion))
    }

    func testAFailedCycleIsRecordedAndAReadyInstallerClearsIt() {
        let fixture = MachineFixture()

        fixture.machine.send(.cycleFinished(.failed("broken")))
        XCTAssertEqual(fixture.failure, "broken")

        fixture.machine.send(.installerPrepared(version: updateVersion))
        XCTAssertNil(fixture.failure)
    }

    func testAHeldWindowOpensOnTheFirstRecheckThatAllowsIt() {
        let fixture = MachineFixture()
        fixture.wait()
        XCTAssertEqual(fixture.effects, [])

        fixture.recheck()
        XCTAssertEqual(fixture.effects, [])

        fixture.conditions = free
        fixture.recheck()
        XCTAssertEqual(fixture.effects, ["openWindow"])
    }

    func testOpeningTheWindowStopsTheRecheck() {
        let fixture = MachineFixture()
        fixture.wait()
        fixture.conditions = free

        fixture.recheck()
        fixture.recheck()

        XCTAssertEqual(fixture.effects, ["openWindow"])
        XCTAssertEqual(fixture.runningRechecks, 0)
    }

    func testAWindowConfirmedDuringItsOwnOpenLeavesNoTimerRunning() {
        let fixture = MachineFixture()
        fixture.conditions = free
        fixture.onOpenWindow = { [unowned fixture] in fixture.machine.send(.windowConfirmed) }

        fixture.wait()

        XCTAssertEqual(fixture.machine.flow.phase, .windowOpen(installerWindow))
        XCTAssertEqual(fixture.runningRechecks, 0)
    }

    func testNoSessionMeansNoWindowCall() {
        let fixture = MachineFixture()
        fixture.conditions = free
        fixture.sessionInProgress = false

        fixture.wait()

        XCTAssertEqual(fixture.effects, [])
    }

    func testTheSessionEndingStopsTheRecheck() {
        let fixture = MachineFixture()
        fixture.wait(window: promptWindow)

        fixture.machine.send(.sessionEnded)

        XCTAssertEqual(fixture.runningRechecks, 0)
        XCTAssertEqual(fixture.machine.flow.phase, .idle)
    }

    func testEveryTransitionIsReportedWithTheNewStatus() {
        let fixture = MachineFixture()

        fixture.machine.send(.downloadStarted(version: updateVersion))
        fixture.machine.send(.cycleFinished(.failed("broken")))

        XCTAssertEqual(fixture.reports.map(\.to.name), ["downloading", "idle"])
        XCTAssertEqual(fixture.status?.phase, "idle")
    }
}
