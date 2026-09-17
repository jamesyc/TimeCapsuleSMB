import Combine
import Foundation

enum AppUpdateState: String, Equatable {
    case idle
    case checking
    case current
    case unavailable
    case updateAvailable
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("app_update.state.idle")
        case .checking:
            return L10n.string("app_update.state.checking")
        case .current:
            return L10n.string("app_update.state.current")
        case .unavailable:
            return L10n.string("app_update.state.unavailable")
        case .updateAvailable:
            return L10n.string("app_update.state.update_available")
        case .failed:
            return L10n.string("app_update.state.failed")
        }
    }
}

/// A newer release the app should offer to the user.
struct UpdatePrompt: Identifiable, Equatable {
    let versionCode: Int
    let title: String
    let tag: String?
    let notes: String
    let publishedDate: Date?
    let htmlURL: URL?
    let asset: ReleaseAssetPayload?
    let isRequired: Bool

    var id: Int {
        versionCode
    }

    init(
        versionCode: Int,
        title: String,
        tag: String?,
        notes: String,
        publishedDate: Date?,
        htmlURL: URL?,
        asset: ReleaseAssetPayload?,
        isRequired: Bool
    ) {
        self.versionCode = versionCode
        self.title = title
        self.tag = tag
        self.notes = notes
        self.publishedDate = publishedDate
        self.htmlURL = htmlURL
        self.asset = asset
        self.isRequired = isRequired
    }

    /// Nil when the payload does not describe a newer version.
    init?(payload: UpdateCheckPayload) {
        guard payload.updateAvailable || payload.shouldBlock, let versionCode = payload.currentVersion else {
            return nil
        }
        self.init(
            versionCode: versionCode,
            title: payload.release?.name ?? payload.latestTag ?? String(versionCode),
            tag: payload.release?.tag ?? payload.latestTag,
            notes: payload.release?.notes ?? "",
            publishedDate: payload.release?.publishedDate,
            htmlURL: URL(string: payload.release?.htmlURL ?? payload.downloadURL),
            asset: payload.release?.asset,
            isRequired: payload.shouldBlock
        )
    }
}

enum ManualCheckOutcome: Equatable {
    case upToDate(localVersionCode: Int)
    case failed(String)
}

/// Schedules a repeating tick; returns a cancellable that stops it. Injectable for tests.
typealias UpdateCheckScheduler = (_ interval: TimeInterval, _ tick: @escaping @MainActor () -> Void) -> AnyCancellable

@MainActor
final class AppUpdateStore: ObservableObject {
    @Published private(set) var state: AppUpdateState = .idle
    @Published private(set) var payload: UpdateCheckPayload?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?
    /// Non-nil while the release-notes sheet should be shown.
    @Published private(set) var promptedRelease: UpdatePrompt?
    /// Set only for manual checks that found nothing to prompt about, so the UI can confirm the check ran.
    @Published var manualCheckOutcome: ManualCheckOutcome?

    let lane: OperationLane
    /// Nil when in-app install is not wired (tests, previews).
    let installer: AppUpdateInstaller?

    nonisolated static let defaultScheduler: UpdateCheckScheduler = { interval, tick in
        Timer.publish(every: interval, on: .main, in: .common)
            .autoconnect()
            .sink { _ in
                Task { @MainActor in
                    tick()
                }
            }
    }

    private let operationObserver = BackendOperationObserver()
    private let scheduler: UpdateCheckScheduler
    private var cancellables: Set<AnyCancellable> = []
    private var timerCancellable: AnyCancellable?
    private var lastSettings: AppSettings = .default
    private var isManualCheck = false
    private var dismissedVersionCode: Int?
    /// SwiftUI drops a sheet requested before the window is key and never retries, so prompts are
    /// held here until `markUIReady()` is called.
    private var isUIReady = false
    private var pendingPrompt: UpdatePrompt?

    init(
        coordinator: OperationCoordinator,
        installer: AppUpdateInstaller? = nil,
        scheduler: @escaping UpdateCheckScheduler = AppUpdateStore.defaultScheduler
    ) {
        self.lane = coordinator.lane(for: .localPath("app-update"))
        self.installer = installer
        self.scheduler = scheduler
        installer?.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var isChecking: Bool {
        lane.backend.isRunning
    }

    /// Runs `update-check`. Automatic checks that collide with a running check are dropped silently;
    /// manual checks report the collision as an error.
    func checkNow(settings: AppSettings, manual: Bool = false) {
        lastSettings = settings
        guard !lane.isBusy else {
            if manual {
                let message = L10n.string("operation.error.already_running")
                state = .failed
                error = BackendErrorViewModel(operation: "update-check", code: "operation_rejected", message: message)
                manualCheckOutcome = .failed(message)
            }
            return
        }
        lane.clear()
        operationObserver.clear()
        state = .checking
        payload = nil
        error = nil
        currentStage = nil
        isManualCheck = manual
        manualCheckOutcome = nil

        let params = OperationParams.Readiness.updateCheck(url: settings.versionCheckURL, releaseURL: settings.releaseInfoURL)
        switch lane.run(operation: "update-check", params: params, context: nil, activeDeviceID: nil) {
        case .started(let operation):
            operationObserver.start(operation)
            process(lane.backend.events)
        case .rejected(let message):
            state = .failed
            operationObserver.clear()
            error = BackendErrorViewModel(operation: "update-check", code: "operation_rejected", message: message)
            if manual {
                manualCheckOutcome = .failed(message)
            }
        }
    }

    /// Starts (or restarts) the periodic check using `settings.updateCheckIntervalHours`.
    func startAutomaticChecks(settings: AppSettings) {
        lastSettings = settings
        timerCancellable = scheduler(TimeInterval(settings.updateCheckIntervalHours) * 3600) { [weak self] in
            guard let self, !self.lane.isBusy else {
                return
            }
            self.checkNow(settings: self.lastSettings)
        }
    }

    func stopAutomaticChecks() {
        timerCancellable = nil
    }

    /// Called once the main window can present sheets; publishes any prompt that arrived earlier.
    func markUIReady() {
        isUIReady = true
        if let pending = pendingPrompt {
            pendingPrompt = nil
            promptedRelease = pending
        }
    }

    var installState: InstallState {
        installer?.state ?? .idle
    }

    /// Nil when the prompted release can be installed in place; otherwise the reason to offer Download instead.
    func installUnavailableReason() -> String? {
        guard let prompt = promptedRelease else {
            return nil
        }
        guard let installer else {
            return L10n.string("app_update.install.unsupported.dev_checkout")
        }
        return installer.canInstall(prompt)
    }

    func install() async {
        guard let prompt = promptedRelease, let installer else {
            return
        }
        await installer.install(prompt)
    }

    /// Hides the prompt for this release until the app relaunches.
    func remindLater() {
        installer?.reset()
        dismissedVersionCode = promptedRelease?.versionCode
        promptedRelease = nil
    }

    /// Hides the prompt and returns the version code to persist as skipped. Required updates cannot be skipped.
    func skipVersion() -> Int? {
        guard let prompt = promptedRelease, !prompt.isRequired else {
            return nil
        }
        dismissedVersionCode = prompt.versionCode
        promptedRelease = nil
        return prompt.versionCode
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "update-check" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }
        if event.type == "error" {
            let viewModel = BackendErrorViewModel(event: event)
            error = viewModel
            state = .failed
            if isManualCheck {
                manualCheckOutcome = .failed(viewModel.message)
            }
            operationObserver.finish()
            return
        }
        guard event.type == "result" else {
            return
        }
        do {
            let result = try event.decodePayload(UpdateCheckPayload.self)
            payload = result
            if result.shouldBlock || result.updateAvailable {
                state = .updateAvailable
            } else if result.source == "unavailable" {
                state = .unavailable
            } else {
                state = .current
            }
            error = nil
            let nextPrompt = prompt(for: result)
            if isUIReady {
                promptedRelease = nextPrompt
            } else {
                pendingPrompt = nextPrompt
                promptedRelease = nil
            }
            if isManualCheck, nextPrompt == nil {
                manualCheckOutcome = result.source == "unavailable"
                    ? .failed(result.localizedSummary)
                    : .upToDate(localVersionCode: result.localVersionCode)
            }
            operationObserver.finish()
        } catch {
            let message = error.localizedDescription
            self.error = BackendErrorViewModel(operation: "update-check", code: "contract_decode_failed", message: message)
            state = .failed
            if isManualCheck {
                manualCheckOutcome = .failed(message)
            }
            operationObserver.finish()
        }
    }

    private func prompt(for result: UpdateCheckPayload) -> UpdatePrompt? {
        guard let prompt = UpdatePrompt(payload: result) else {
            return nil
        }
        if prompt.isRequired || isManualCheck {
            return prompt
        }
        if prompt.versionCode == lastSettings.skippedUpdateVersionCode {
            return nil
        }
        if prompt.versionCode == dismissedVersionCode {
            return nil
        }
        return prompt
    }
}
