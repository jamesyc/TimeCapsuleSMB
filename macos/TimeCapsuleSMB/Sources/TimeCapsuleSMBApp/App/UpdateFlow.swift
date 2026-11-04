import AppKit
import Combine
import Foundation
import os

/// What the conditions read from the app. A test process is never the active
/// app and has no key window, so its tests hand over real windows this way.
@MainActor
protocol UpdateWindowHost {
    var isActive: Bool { get }
    var keyWindow: NSWindow? { get }
    var modalWindow: NSWindow? { get }
    var windows: [NSWindow] { get }
}

extension NSApplication: UpdateWindowHost {}

/// Whether Sparkle's update window may open now. It takes the keyboard and
/// makes Return its install button, so it waits for typing, a confirmation and
/// a device operation, and it never pulls the app forward.
struct UpdateWindowConditions: Equatable {
    var appIsActive: Bool
    var hasBlockingWork: Bool
    var isTyping: Bool
    var hasModal: Bool

    /// No time limit: a held window loses nothing, because quitting still
    /// installs and a window with nothing ready returns at the next launch.
    var mayOpen: Bool {
        appIsActive && !hasModal && !hasBlockingWork && !isTyping
    }

    @MainActor
    static var current: UpdateWindowConditions {
        read(from: NSApplication.shared, hasBlockingWork: AppCloseGuard.shared.hasBlockingActivity)
    }

    @MainActor
    static func read(from host: UpdateWindowHost, hasBlockingWork: Bool) -> UpdateWindowConditions {
        UpdateWindowConditions(
            appIsActive: host.isActive,
            hasBlockingWork: hasBlockingWork,
            isTyping: isTyping(in: host.keyWindow),
            hasModal: host.modalWindow != nil || host.windows.contains(where: hasSheet)
        )
    }

    /// A focused text control types through the window's field editor, an
    /// `NSText`, which is what holds first responder.
    @MainActor
    static func isTyping(in window: NSWindow?) -> Bool {
        window?.firstResponder is NSText
    }

    /// SwiftUI's sheets, alerts and confirmation dialogs all attach here.
    @MainActor
    static func hasSheet(_ window: NSWindow) -> Bool {
        window.attachedSheet != nil
    }
}

/// Bundle versions compare as numbers, so 100000 is above 99999.
enum UpdateVersion {
    static func isNewer(_ version: String, than other: String) -> Bool {
        version.compare(other, options: .numeric) == .orderedDescending
    }
}

/// The last real failure, with what a reader needs to judge how old it is.
struct UpdateFailure: Equatable {
    var message: String
    var date: Date
    var appVersion: String
}

/// What a launch must remember about a silent update that did not reach an
/// installer. Sparkle cannot resume a download and keeps a finished one only in
/// memory, so the next launch asks first and shows Sparkle's progress window.
enum UpdateMemory: Equatable {
    case none
    /// A quit or a failure stopped it. The next launch asks, once.
    case interrupted(version: String)
    /// It downloaded but needs authorization, so a silent download can never
    /// finish. Every launch asks until the app reaches this version.
    case needsAuthorization(version: String)

    var diagnosticsDescription: String {
        switch self {
        case .none: "none"
        case .interrupted(let version): "interrupted \(version)"
        case .needsAuthorization(let version): "needs authorization \(version)"
        }
    }
}

/// `UpdateMemory` and the last failure, which both outlive the launch.
struct UpdateMemoryStore {
    private static let memoryKey = "TimeCapsuleSMBUpdateMemory"
    private static let failureKey = "TimeCapsuleSMBLastUpdateFailure"
    private static let interrupted = "interrupted"
    private static let needsAuthorization = "needsAuthorization"

    let defaults: UserDefaults

    var memory: UpdateMemory {
        get {
            guard let saved = defaults.dictionary(forKey: Self.memoryKey) as? [String: String],
                  let version = saved["version"] else {
                return .none
            }
            return saved["reason"] == Self.needsAuthorization
                ? .needsAuthorization(version: version)
                : .interrupted(version: version)
        }
        nonmutating set {
            switch newValue {
            case .none:
                defaults.removeObject(forKey: Self.memoryKey)
            case .interrupted(let version):
                defaults.set(["version": version, "reason": Self.interrupted], forKey: Self.memoryKey)
            case .needsAuthorization(let version):
                defaults.set(["version": version, "reason": Self.needsAuthorization], forKey: Self.memoryKey)
            }
        }
    }

    var lastFailure: UpdateFailure? {
        get {
            guard let saved = defaults.dictionary(forKey: Self.failureKey),
                  let message = saved["message"] as? String,
                  let date = saved["date"] as? Date,
                  let appVersion = saved["appVersion"] as? String else {
                return nil
            }
            return UpdateFailure(message: message, date: date, appVersion: appVersion)
        }
        nonmutating set {
            if let newValue {
                defaults.set(
                    ["message": newValue.message, "date": newValue.date, "appVersion": newValue.appVersion] as [String: Any],
                    forKey: Self.failureKey
                )
            } else {
                defaults.removeObject(forKey: Self.failureKey)
            }
        }
    }

    /// Declining authorization deletes the download, so an app left open would
    /// otherwise fetch it again at its next daily check.
    func remember(_ newMemory: UpdateMemory) {
        memory = newMemory
        if case .needsAuthorization = newMemory {
            askBeforeDownloading()
        }
    }

    /// A failure belongs to the app that had it, so a newer app starts clean.
    func forgetFailureFromOlderVersion(installedVersion: String) {
        if let failure = lastFailure, UpdateVersion.isNewer(installedVersion, than: failure.appVersion) {
            lastFailure = nil
        }
    }

    /// A version the app already reached, by any route, has nothing left to
    /// ask about.
    func askBeforeDownloadingThisLaunch(installedVersion: String) {
        let asks: Bool
        switch memory {
        case .none:
            return
        case .interrupted(let version):
            asks = UpdateVersion.isNewer(version, than: installedVersion)
            memory = .none
        case .needsAuthorization(let version):
            asks = UpdateVersion.isNewer(version, than: installedVersion)
            if !asks {
                memory = .none
            }
        }
        if asks {
            askBeforeDownloading()
        }
    }

    /// The argument domain overrides the setting for this process without
    /// changing what the user chose. Sparkle observes the key, so this also
    /// reaches an updater that already exists.
    private func askBeforeDownloading() {
        var arguments = defaults.volatileDomain(forName: UserDefaults.argumentDomain)
        arguments["SUAutomaticallyUpdate"] = false
        defaults.setVolatileDomain(arguments, forName: UserDefaults.argumentDomain)
    }
}

/// How far a window's update has come, as Sparkle reports it. Each stage has
/// its own buttons and its own answer to what quitting does.
enum WindowStage: Equatable {
    /// Install Update, Remind Me Later, Skip This Version.
    case notDownloaded
    /// The same buttons. It downloaded but no installer runs, so quitting
    /// installs nothing. Reached when the app folder needs authorization.
    case downloaded
    /// Install and Relaunch, Install on Quit, Skip This Version. An installer
    /// is running, and quitting installs.
    case installing
}

struct UpdateWindow: Equatable {
    var stage: WindowStage
    var version: String
}

/// The app's side of an update, as one state machine. Sparkle owns the real
/// session and the installer process; every callback from it is an event here,
/// and nothing else starts a check, opens the window, runs the recheck timer or
/// writes `UpdateMemory`.
///
///     idle ─► downloading ─► installerReady ─► reprobing
///       │          │                │               │
///       │ prompt   │ needs          │ already       │
///       │          │ authorization  │ reprobed      │
///       │          ▼                ▼               ▼
///       └────► windowHeld ◄──── installerWaiting ◄─ (no window)
///                  │                ▲
///                  ▼                │ Install on Quit, relaunch
///              windowOpen ──────────┘
///
/// `step` has no default case, so a new phase or event does not compile until
/// each pair has an answer. Sparkle is the truth. A window, cycle or install
/// event the model did not predict corrects the phase; a download event it did
/// not predict says nothing about the phase, so the phase stays.
///
/// What holds for every sequence of events (tested on random sequences):
/// - The recheck timer runs exactly while the phase is `windowHeld`.
/// - A version gets one follow-up check per launch. Sparkle's probe gives up
///   after 7 seconds and then picks the silent download path. If that ever
///   repeats a download, the flow stops at `installerWaiting` after it, so the
///   cost is at most one extra download; an installer frozen for 70 seconds
///   started none. Critical updates never ask for a follow-up check, because
///   Sparkle checks again by itself.
/// - The window opens only when Sparkle leaves it to the flow or on a recheck,
///   and the phase is `windowOpen` from then on.
/// - After `cycleFinished` the phase is `idle`, `reprobing` or
///   `installerWaiting`: no session can leave another phase behind.
/// - `installerWaiting` is reachable only once an installer was reported ready.
///   It ends if a check finds no update, because Sparkle resumes a live installer
///   without looking for one.
/// - The flow clears `UpdateMemory` only when an installer is ready. The launch
///   check clears it once the app reaches that version.
struct UpdateFlow: Equatable {
    enum Phase: Equatable {
        case idle
        /// A silent download, then the installer unpacking and validating it.
        /// A download the user asked for stays `windowOpen`.
        case downloading(version: String)
        /// The installer is ready and this update cycle has not ended yet.
        case installerReady(version: String)
        /// The follow-up check that finds the installer and builds the window.
        case reprobing(version: String)
        /// Sparkle built a window and left opening it to us.
        case windowHeld(UpdateWindow)
        case windowOpen(UpdateWindow)
        /// The installer runs when the app quits. Includes a relaunch the
        /// close guard or a dialog cancelled, when Sparkle closes its window.
        case installerWaiting(version: String)

        var isHeld: Bool {
            if case .windowHeld = self {
                return true
            }
            return false
        }

        var name: String {
            switch self {
            case .idle: "idle"
            case .downloading: "downloading"
            case .installerReady: "installerReady"
            case .reprobing: "reprobing"
            case .windowHeld: "windowHeld"
            case .windowOpen: "windowOpen"
            case .installerWaiting: "installerWaiting"
            }
        }

        var detail: String {
            switch self {
            case .idle:
                name
            case .downloading(let version), .installerReady(let version), .reprobing(let version),
                 .installerWaiting(let version):
                "\(name) \(version)"
            case .windowHeld(let window), .windowOpen(let window):
                "\(name) \(window.stage) \(window.version)"
            }
        }
    }

    enum CycleEnd: Equatable {
        case finished
        case noUpdateFound
        case failed(String)
    }

    enum Event: Equatable {
        case downloadStarted(version: String)
        case installerPrepared(version: String)
        case cycleFinished(CycleEnd)
        case windowWaiting(UpdateWindow, UpdateWindowConditions)
        /// Sparkle showed a window itself, for Check for Updates...
        case windowShown(UpdateWindow)
        /// A window got the user's attention, as when Check for Updates...
        /// opens a held one.
        case windowConfirmed
        /// Install on Quit.
        case installerKept(version: String)
        case updateSkipped
        /// Sparkle is about to quit the app. A dialog or Keep Open can cancel
        /// that, and the installer then waits for the next quit.
        case relaunchRequested
        case recheck(UpdateWindowConditions)
        case sessionEnded
    }

    enum Action: Equatable {
        case checkAgain
        case openWindow
        case startRecheck
        case stopRecheck
        case remember(UpdateMemory)
        case recordFailure(String)
        case clearFailure
    }

    enum Outcome: Equatable {
        /// The event belongs to this phase.
        case expected
        /// A late or repeated event that changes nothing.
        case stale
        /// Sparkle did something this model did not predict. The phase follows
        /// when the event says where it went, and stays when it does not.
        case corrected
    }

    struct Transition: Equatable {
        var from: Phase
        var to: Phase
        var event: Event
        var outcome: Outcome
        var actions: [Action]
    }

    private struct Step {
        var phase: Phase
        var outcome: Outcome
        var actions: [Action] = []
    }

    private(set) var phase = Phase.idle
    private(set) var phaseStart: Date
    private(set) var driftCount = 0
    private var versionsCheckedAgain: Set<String> = []

    init(now: Date) {
        phaseStart = now
    }

    var status: UpdateFlowStatus {
        UpdateFlowStatus(phase: phase.detail, since: phaseStart, driftCount: driftCount)
    }

    mutating func handle(_ event: Event, at now: Date) -> Transition {
        let from = phase
        var step = step(for: event)
        if step.actions.contains(.checkAgain), case .installerReady(let version) = from {
            versionsCheckedAgain.insert(version)
        }
        // First, so a window that opens at once and reports back stops a
        // timer that is already running.
        if step.phase.isHeld != from.isHeld {
            step.actions.insert(step.phase.isHeld ? .startRecheck : .stopRecheck, at: 0)
        }
        if case .installerPrepared = event, case .installerReady = step.phase, step.outcome != .stale {
            step.actions.append(.clearFailure)
        }
        if case .cycleFinished(.failed(let failure)) = event {
            step.actions.append(.recordFailure(failure))
        }
        if step.phase.name != from.name {
            phaseStart = now
        }
        if step.outcome == .corrected {
            driftCount += 1
        }
        phase = step.phase
        return Transition(from: from, to: step.phase, event: event, outcome: step.outcome, actions: step.actions)
    }

    private func step(for event: Event) -> Step {
        let stale = Step(phase: phase, outcome: .stale)
        let drift = Step(phase: phase, outcome: .corrected)

        switch phase {
        case .idle:
            switch event {
            case .downloadStarted(let version):
                return Step(phase: .downloading(version: version), outcome: .expected, actions: [.remember(.interrupted(version: version))])
            case .installerPrepared(let version):
                return Step(phase: .installerReady(version: version), outcome: .corrected, actions: [.remember(.none)])
            case .cycleFinished, .sessionEnded:
                return Step(phase: phase, outcome: .expected)
            case .windowWaiting(let window, let conditions):
                return hold(window, conditions, outcome: .expected)
            case .windowShown(let window):
                return Step(phase: .windowOpen(window), outcome: .expected)
            case .installerKept(let version):
                return Step(phase: .installerWaiting(version: version), outcome: .corrected)
            case .windowConfirmed, .updateSkipped, .relaunchRequested, .recheck:
                return stale
            }

        case .downloading:
            switch event {
            case .downloadStarted(let other):
                return Step(phase: .downloading(version: other), outcome: .corrected, actions: [.remember(.interrupted(version: other))])
            case .installerPrepared(let version):
                return Step(phase: .installerReady(version: version), outcome: .expected, actions: [.remember(.none)])
            case .cycleFinished:
                return Step(phase: .idle, outcome: .expected)
            case .windowWaiting(let window, let conditions):
                return hold(window, conditions, outcome: .expected, before: silentDownloadEnded(window))
            case .windowShown(let window):
                return Step(phase: .windowOpen(window), outcome: .corrected, actions: silentDownloadEnded(window))
            case .installerKept(let version):
                return Step(phase: .installerWaiting(version: version), outcome: .corrected, actions: [.remember(.none)])
            case .updateSkipped:
                return Step(phase: .idle, outcome: .corrected)
            case .windowConfirmed, .relaunchRequested, .recheck, .sessionEnded:
                return stale
            }

        case .installerReady(let version):
            switch event {
            case .downloadStarted(let other):
                return Step(phase: .downloading(version: other), outcome: .corrected, actions: [.remember(.interrupted(version: other))])
            case .cycleFinished:
                if versionsCheckedAgain.contains(version) {
                    return Step(phase: .installerWaiting(version: version), outcome: .expected)
                }
                return Step(phase: .reprobing(version: version), outcome: .expected, actions: [.checkAgain])
            case .windowWaiting(let window, let conditions):
                // A critical update: Sparkle checks again without ending the cycle.
                return hold(window, conditions, outcome: .expected)
            case .windowShown(let window):
                return Step(phase: .windowOpen(window), outcome: .corrected)
            case .installerKept(let kept):
                return Step(phase: .installerWaiting(version: kept), outcome: .corrected)
            case .updateSkipped:
                return Step(phase: .idle, outcome: .corrected)
            case .installerPrepared, .windowConfirmed, .relaunchRequested, .recheck, .sessionEnded:
                return stale
            }

        case .reprobing(let version):
            switch event {
            case .downloadStarted(let other):
                // A probe that finds no installer takes Sparkle's download path.
                return Step(phase: .downloading(version: other), outcome: .expected, actions: [.remember(.interrupted(version: other))])
            case .installerPrepared(let prepared):
                return Step(phase: .installerReady(version: prepared), outcome: .corrected, actions: [.remember(.none)])
            case .cycleFinished(.noUpdateFound):
                // A live installer would have been resumed, not looked past.
                return Step(phase: .idle, outcome: .corrected)
            case .cycleFinished:
                // The check built no window. The installer was ready moments
                // ago, and a failed check does not mean it went away.
                return Step(phase: .installerWaiting(version: version), outcome: .expected)
            case .windowWaiting(let window, let conditions):
                return hold(window, conditions, outcome: .expected)
            case .windowShown(let window):
                return Step(phase: .windowOpen(window), outcome: .corrected)
            case .installerKept(let kept):
                return Step(phase: .installerWaiting(version: kept), outcome: .corrected)
            case .updateSkipped:
                return Step(phase: .idle, outcome: .corrected)
            case .windowConfirmed, .relaunchRequested, .recheck, .sessionEnded:
                return stale
            }

        case .windowHeld(let window):
            switch event {
            case .downloadStarted, .installerPrepared:
                return drift
            case .cycleFinished:
                return Step(phase: phaseAfter(window), outcome: .corrected)
            case .windowWaiting(let other, let conditions):
                if other == window {
                    return stale
                }
                return hold(other, conditions, outcome: .corrected)
            case .windowShown(let shown):
                return Step(phase: .windowOpen(shown), outcome: .expected)
            case .windowConfirmed:
                return Step(phase: .windowOpen(window), outcome: .expected)
            case .installerKept(let version):
                return Step(phase: .installerWaiting(version: version), outcome: .corrected)
            case .updateSkipped:
                return Step(phase: .idle, outcome: .corrected)
            case .relaunchRequested:
                return Step(phase: .installerWaiting(version: window.version), outcome: .expected)
            case .recheck(let conditions):
                return hold(window, conditions, outcome: .expected)
            case .sessionEnded:
                return Step(phase: phaseAfter(window), outcome: .expected)
            }

        case .windowOpen(let window):
            switch event {
            case .downloadStarted:
                // The user's own download, with Sparkle's progress window.
                return Step(phase: phase, outcome: .expected)
            case .installerPrepared:
                return drift
            case .cycleFinished:
                return Step(phase: phaseAfter(window), outcome: .corrected)
            case .windowWaiting(let other, let conditions):
                // Sparkle built another window, so this one closed or never opened.
                return hold(other, conditions, outcome: .corrected)
            case .windowShown(let other):
                return Step(phase: .windowOpen(other), outcome: other == window ? .stale : .corrected)
            case .windowConfirmed, .recheck:
                return stale
            case .installerKept(let version):
                return Step(phase: .installerWaiting(version: version), outcome: .expected)
            case .updateSkipped:
                return Step(phase: .idle, outcome: .expected)
            case .relaunchRequested:
                return Step(phase: .installerWaiting(version: window.version), outcome: .expected)
            case .sessionEnded:
                return Step(phase: phaseAfter(window), outcome: .expected)
            }

        case .installerWaiting:
            switch event {
            case .downloadStarted(let other):
                // The installer died, so Sparkle starts over.
                return Step(phase: .downloading(version: other), outcome: .expected, actions: [.remember(.interrupted(version: other))])
            case .installerPrepared(let prepared):
                return Step(phase: .installerReady(version: prepared), outcome: .corrected, actions: [.remember(.none)])
            case .windowWaiting(let window, let conditions):
                return hold(window, conditions, outcome: .expected)
            case .windowShown(let window):
                return Step(phase: .windowOpen(window), outcome: .expected)
            case .updateSkipped:
                return Step(phase: .idle, outcome: .corrected)
            case .cycleFinished(.noUpdateFound):
                // A live installer would have been resumed, not looked past.
                return Step(phase: .idle, outcome: .corrected)
            case .cycleFinished, .windowConfirmed, .installerKept, .relaunchRequested, .recheck, .sessionEnded:
                return stale
            }
        }
    }

    private func hold(
        _ window: UpdateWindow,
        _ conditions: UpdateWindowConditions,
        outcome: Outcome,
        before: [Action] = []
    ) -> Step {
        guard conditions.mayOpen else {
            return Step(phase: .windowHeld(window), outcome: outcome, actions: before)
        }
        return Step(phase: .windowOpen(window), outcome: outcome, actions: before + [.openWindow])
    }

    /// A window that ends with an installer leaves it waiting. Skip cancels
    /// that install and arrives first, as `updateSkipped`.
    private func phaseAfter(_ window: UpdateWindow) -> Phase {
        window.stage == .installing ? .installerWaiting(version: window.version) : .idle
    }

    /// What a silent download leaves behind once Sparkle turns it into a window.
    private func silentDownloadEnded(_ window: UpdateWindow) -> [Action] {
        switch window.stage {
        case .installing:
            [.remember(.none)]
        case .downloaded:
            [.remember(.needsAuthorization(version: window.version))]
        case .notDownloaded:
            []
        }
    }
}

/// Where the flow is, for the diagnostics export. The age is worked out when
/// the export runs, so a phase nothing has touched for hours still reads true.
struct UpdateFlowStatus: Equatable {
    var phase: String
    var since: Date
    var driftCount: Int

    func summary(at now: Date) -> String {
        let minutes = Int(now.timeIntervalSince(since) / 60)
        let drift = driftCount == 0 ? "" : ", \(driftCount) drift"
        return "\(phase) for \(minutes) min\(drift)"
    }
}

/// Runs `UpdateFlow` against the outside world. Actions run after the phase has
/// changed, so a callback they cause sees the new phase.
final class UpdateFlowMachine {
    struct Effects {
        var checkAgain: () -> Void
        var openWindow: () -> Void
        var sessionInProgress: () -> Bool
        var startRecheck: (@escaping () -> Void) -> AnyCancellable
        var remember: (UpdateMemory) -> Void
        var recordFailure: (String?) -> Void
        var conditions: () -> UpdateWindowConditions
        var now: () -> Date
        var report: (UpdateFlow.Transition, UpdateFlowStatus) -> Void
    }

    private let effects: Effects
    private var recheck: AnyCancellable?
    private(set) var flow: UpdateFlow

    init(effects: Effects) {
        self.effects = effects
        flow = UpdateFlow(now: effects.now())
    }

    var conditions: UpdateWindowConditions {
        effects.conditions()
    }

    func send(_ event: UpdateFlow.Event) {
        let transition = flow.handle(event, at: effects.now())
        effects.report(transition, flow.status)
        for action in transition.actions {
            perform(action)
        }
    }

    private func perform(_ action: UpdateFlow.Action) {
        switch action {
        case .checkAgain:
            effects.checkAgain()
        case .openWindow:
            // Without a session, opening the window would start a manual check.
            if effects.sessionInProgress() {
                effects.openWindow()
            }
        case .startRecheck:
            recheck = effects.startRecheck { [weak self] in
                guard let self else {
                    return
                }
                send(.recheck(effects.conditions()))
            }
        case .stopRecheck:
            recheck = nil
        case .remember(let memory):
            effects.remember(memory)
        case .recordFailure(let failure):
            effects.recordFailure(failure)
        case .clearFailure:
            effects.recordFailure(nil)
        }
    }
}

/// The flow's latest status for the diagnostics export, which has no way to
/// reach the app's one updater.
@MainActor
enum UpdateFlowBoard {
    static var status: UpdateFlowStatus?

    static func summary(at now: Date) -> String {
        status?.summary(at: now) ?? "not started"
    }
}
