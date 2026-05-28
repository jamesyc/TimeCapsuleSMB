import Combine
import Foundation

enum DeviceDiscoveryState: String, CaseIterable, Equatable {
    case idle
    case waitingForReadiness
    case discovering
    case empty
    case ready
    case paused
    case readinessBlocked
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("discovery_monitor.state.idle")
        case .waitingForReadiness:
            return L10n.string("discovery_monitor.state.waiting_for_readiness")
        case .discovering:
            return L10n.string("discovery_monitor.state.discovering")
        case .empty:
            return L10n.string("discovery_monitor.state.empty")
        case .ready:
            return L10n.string("discovery_monitor.state.ready")
        case .paused:
            return L10n.string("discovery_monitor.state.paused")
        case .readinessBlocked:
            return L10n.string("discovery_monitor.state.readiness_blocked")
        case .failed:
            return L10n.string("discovery_monitor.state.failed")
        }
    }
}

@MainActor
final class DeviceDiscoveryStore: ObservableObject {
    @Published private(set) var state: DeviceDiscoveryState = .idle
    @Published private(set) var devices: [DiscoveredDevice] = []
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let coordinator: OperationCoordinator
    let readinessStore: AppReadinessStore?
    let registry: DeviceRegistryStore
    private let lane: OperationLane

    private var timeout: Double
    private var isMonitoring = false
    private var pendingRefresh = false
    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    init(
        coordinator: OperationCoordinator,
        readinessStore: AppReadinessStore? = nil,
        registry: DeviceRegistryStore,
        timeout: Double = AppSettings.default.defaultBonjourTimeoutSeconds
    ) {
        self.coordinator = coordinator
        self.readinessStore = readinessStore
        self.registry = registry
        self.timeout = timeout
        self.lane = coordinator.appLane

        readinessStore?.$state
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.handleReadinessChange()
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .sink { [weak self] isRunning in
                guard !isRunning else { return }
                Task { @MainActor in
                    self?.resumePendingRefreshIfNeeded()
                }
            }
            .store(in: &cancellables)
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        registry.$profiles
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var unsavedDevices: [DiscoveredDevice] {
        devices.filter { matchingProfile(for: $0) == nil }
    }

    var savedDevices: [DiscoveredDevice] {
        devices.filter { matchingProfile(for: $0) != nil }
    }

    func startMonitoring() {
        guard !isMonitoring else {
            return
        }
        isMonitoring = true
        handleReadinessChange()
    }

    func refresh() {
        guard isMonitoring else {
            isMonitoring = true
            return handleReadinessChange()
        }
        runDiscoverWhenPossible()
    }

    func refresh(timeout: Double) {
        self.timeout = timeout
        refresh()
    }

    func applyAppSettings(_ settings: AppSettings) {
        timeout = settings.defaultBonjourTimeoutSeconds
    }

    func matchingProfile(for device: DiscoveredDevice) -> DeviceProfile? {
        registry.matchingProfile(for: device)
    }

    func lastSeenText(for profile: DeviceProfile) -> String? {
        guard state == .ready || state == .empty else {
            return nil
        }
        let wasSeen = devices.contains { device in
            matchingProfile(for: device)?.id == profile.id
        }
        return wasSeen ? L10n.string("discovery_monitor.last_seen.now") : nil
    }

    private func handleReadinessChange() {
        guard isMonitoring else {
            return
        }
        guard let readinessStore else {
            if devices.isEmpty && state != .discovering {
                runDiscoverWhenPossible()
            }
            return
        }
        switch readinessStore.state.kind {
        case .ready, .degraded:
            if devices.isEmpty && state != .discovering {
                runDiscoverWhenPossible()
            }
        case .blocked:
            state = .readinessBlocked
            pendingRefresh = false
        default:
            state = .waitingForReadiness
            pendingRefresh = false
        }
    }

    private func runDiscoverWhenPossible() {
        if let readinessStore {
            switch readinessStore.state.kind {
            case .ready, .degraded:
                break
            case .blocked:
                state = .readinessBlocked
                pendingRefresh = false
                return
            default:
                state = .waitingForReadiness
                pendingRefresh = false
                return
            }
        }

        guard !lane.isBusy else {
            if operationObserver.activeOperation == nil {
                pendingRefresh = true
                state = .paused
            }
            return
        }

        lane.clear()
        operationObserver.clear()
        error = nil
        currentStage = nil
        switch coordinator.run(
            operation: "discover",
            params: OperationParams.discover(timeout: timeout),
            context: nil,
            activeDeviceID: nil,
            laneKey: .app
        ) {
        case .started(let operation):
            operationObserver.start(operation)
            state = .discovering
            process(lane.backend.events)
        case .rejected(let message):
            operationObserver.clear()
            error = BackendErrorViewModel(
                operation: "discover",
                code: "operation_rejected",
                message: message
            )
            state = .failed
        }
    }

    private func resumePendingRefreshIfNeeded() {
        guard pendingRefresh else {
            return
        }
        pendingRefresh = false
        runDiscoverWhenPossible()
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "discover" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }
        if event.type == "error" {
            error = BackendErrorViewModel(event: event)
            operationObserver.finish()
            state = .failed
            return
        }
        guard event.type == "result" else {
            return
        }
        guard event.ok == true else {
            error = BackendErrorViewModel(
                operation: "discover",
                code: "operation_failed",
                message: event.payloadSummaryText ?? event.summary
            )
            operationObserver.finish()
            state = .failed
            return
        }
        applyDiscoverResult(event)
    }

    private func applyDiscoverResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(DiscoverPayload.self)
            devices = payload.devices.enumerated().map { index, device in
                DiscoveredDevice(payload: device, index: index)
            }
            error = nil
            operationObserver.finish()
            state = devices.isEmpty ? .empty : .ready
        } catch {
            self.error = BackendErrorViewModel(
                operation: "discover",
                code: "contract_decode_failed",
                message: error.localizedDescription
            )
            operationObserver.finish()
            state = .failed
        }
    }
}
