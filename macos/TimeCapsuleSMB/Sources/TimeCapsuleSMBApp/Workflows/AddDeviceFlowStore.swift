import Combine
import Foundation

enum AddDeviceFlowState: String, CaseIterable, Equatable {
    case idle
    case discovering
    case discoveryEmpty
    case discoveryReady
    case manualEntry
    case passwordEntry
    case configuring
    case savingProfile
    case saved
    case authFailed
    case unsupported
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("add_device.state.idle")
        case .discovering:
            return L10n.string("add_device.state.discovering")
        case .discoveryEmpty:
            return L10n.string("add_device.state.discovery_empty")
        case .discoveryReady:
            return L10n.string("add_device.state.discovery_ready")
        case .manualEntry:
            return L10n.string("add_device.state.manual_entry")
        case .passwordEntry:
            return L10n.string("add_device.state.password_entry")
        case .configuring:
            return L10n.string("add_device.state.configuring")
        case .savingProfile:
            return L10n.string("add_device.state.saving_profile")
        case .saved:
            return L10n.string("add_device.state.saved")
        case .authFailed:
            return L10n.string("add_device.state.auth_failed")
        case .unsupported:
            return L10n.string("add_device.state.unsupported")
        case .failed:
            return L10n.string("add_device.state.failed")
        }
    }
}

enum AddDeviceEntryMode: String, CaseIterable, Equatable, Identifiable {
    case discover
    case manual

    var id: String { rawValue }

    var title: String {
        switch self {
        case .discover:
            return L10n.string("add_device.entry.discover")
        case .manual:
            return L10n.string("add_device.entry.manual")
        }
    }
}

@MainActor
final class AddDeviceFlowStore: ObservableObject {
    @Published private(set) var entryMode: AddDeviceEntryMode = .discover
    @Published var manualHost = ""
    @Published var bonjourTimeout = "6"
    @Published var password = ""
    @Published var debugLogging = false
    @Published private(set) var state: AddDeviceFlowState = .idle
    @Published private(set) var devices: [DiscoveredDevice] = []
    @Published var selectedDeviceID: DiscoveredDevice.ID?
    @Published private(set) var savedProfile: DeviceProfile?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let coordinator: OperationCoordinator
    let registry: DeviceRegistryStore
    let passwordStore: PasswordStore
    let profileSaver: ConfiguredDeviceProfileSaving
    private let appLane: OperationLane

    private var pendingProfileID: DeviceProfile.ID?
    private var pendingDiscoveredDevice: DiscoveredDevice?
    private var activeOperation: ActiveOperation?
    private var activeLaneKey: OperationLaneKey?
    private var lastProcessedEventCounts: [OperationLaneKey: Int] = [:]
    private var cancellables: Set<AnyCancellable> = []
    private var observedLaneKeys: Set<OperationLaneKey> = []

    init(
        coordinator: OperationCoordinator,
        registry: DeviceRegistryStore,
        passwordStore: PasswordStore,
        profileSaver: ConfiguredDeviceProfileSaving? = nil
    ) {
        self.coordinator = coordinator
        self.registry = registry
        self.passwordStore = passwordStore
        self.profileSaver = profileSaver ?? ConfiguredDeviceProfileSaver(registry: registry, passwordStore: passwordStore)
        self.appLane = coordinator.appLane
        observe(lane: appLane)
    }

    private func observe(lane: OperationLane) {
        guard observedLaneKeys.insert(lane.key).inserted else {
            return
        }
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events, laneKey: lane.key)
                }
            }
            .store(in: &cancellables)
    }

    var isRunning: Bool {
        switch activeLaneKey {
        case .some(let key):
            return coordinator.lane(for: key).backend.isRunning
        case .none:
            return false
        }
    }

    var canCancel: Bool {
        guard let activeLaneKey else {
            return false
        }
        return coordinator.lane(for: activeLaneKey).backend.canCancel
    }

    var selectedDevice: DiscoveredDevice? {
        guard let selectedDeviceID else {
            return nil
        }
        return devices.first { $0.id == selectedDeviceID }
    }

    var hostFieldText: String {
        switch entryMode {
        case .discover:
            return selectedDevice?.host ?? ""
        case .manual:
            return manualHost
        }
    }

    var isHostFieldEditable: Bool {
        entryMode == .manual
    }

    var bonjourTimeoutValue: Double? {
        nonNegativeDouble(bonjourTimeout)
    }

    var canConfigure: Bool {
        let hasTarget: Bool
        switch entryMode {
        case .discover:
            hasTarget = selectedDevice != nil
        case .manual:
            hasTarget = !manualHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        return !isRunning
            && !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && hasTarget
    }

    func setEntryMode(_ mode: AddDeviceEntryMode) {
        guard entryMode != mode else {
            return
        }
        switch mode {
        case .discover:
            entryMode = .discover
            selectedDeviceID = nil
            manualHost = ""
            savedProfile = nil
            error = nil
            currentStage = nil
            state = devices.isEmpty ? .idle : .discoveryReady
        case .manual:
            startManualEntry()
        }
    }

    func startManualEntry() {
        entryMode = .manual
        state = .manualEntry
        devices = []
        selectedDeviceID = nil
        savedProfile = nil
        error = nil
        currentStage = nil
    }

    func promptForPassword() {
        guard hasSelectedTarget else {
            failLocally(L10n.string("add_device.error.choose_target"))
            return
        }
        state = .passwordEntry
        error = nil
    }

    func runDiscover() {
        guard let timeout = bonjourTimeoutValue else {
            failLocally(L10n.string("add_device.error.invalid_bonjour_timeout"))
            return
        }
        guard !appLane.isBusy else {
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }
        resetRunState(clearDevices: true)
        entryMode = .discover
        manualHost = ""
        switch coordinator.run(
            operation: "discover",
            params: OperationParams.discover(timeout: timeout),
            context: nil,
            activeDeviceID: nil,
            laneKey: .app
        ) {
        case .started(let operation):
            activeOperation = operation
            activeLaneKey = .app
            state = .discovering
        case .rejected(let message):
            rejectRun(message)
        }
    }

    func runConfigure() {
        let trimmedPassword = password.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedPassword.isEmpty else {
            state = .passwordEntry
            failLocally(L10n.string("add_device.error.password_required"))
            return
        }
        let selectedDevice = entryMode == .discover ? selectedDevice : nil
        let trimmedHost = manualHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard selectedDevice != nil || (entryMode == .manual && !trimmedHost.isEmpty) else {
            failLocally(L10n.string("add_device.error.choose_target"))
            return
        }

        let targetHost = selectedDevice?.host ?? trimmedHost
        let existing = registry.matchingProfile(host: targetHost, bonjourFullname: selectedDevice?.fullname)
        let profileID = existing?.id ?? UUID().uuidString.lowercased()
        pendingProfileID = profileID
        pendingDiscoveredDevice = selectedDevice

        let context = DeviceRuntimeContext(
            profileID: profileID,
            configURL: DeviceProfile.configURL(for: profileID, applicationSupportURL: registry.applicationSupportURL)
        )

        let laneKey = OperationLaneKey.device(profileID)
        let lane = coordinator.lane(for: laneKey)
        observe(lane: lane)

        guard !lane.isBusy else {
            pendingProfileID = nil
            pendingDiscoveredDevice = nil
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }
        resetRunState(clearDevices: false)
        lastProcessedEventCounts[laneKey] = 0
        switch coordinator.run(
            operation: "configure",
            params: OperationParams.configure(
                host: targetHost,
                selectedRecord: selectedDevice?.rawRecord,
                password: password,
                debugLogging: debugLogging
            ),
            context: context,
            activeDeviceID: profileID,
            laneKey: laneKey
        ) {
        case .started(let operation):
            activeOperation = operation
            activeLaneKey = laneKey
            state = .configuring
        case .rejected(let message):
            pendingProfileID = nil
            pendingDiscoveredDevice = nil
            rejectRun(message)
        }
    }

    func select(_ device: DiscoveredDevice) {
        entryMode = .discover
        selectedDeviceID = device.id
        manualHost = device.host
        if let existing = registry.matchingProfile(host: device.host, bonjourFullname: device.fullname) {
            savedProfile = existing
            state = .saved
            error = nil
            return
        }
        state = .passwordEntry
    }

    func stageDiscoveredDevice(_ device: DiscoveredDevice) {
        stageDiscoveredDevices([device], selected: device)
    }

    func stageDiscoveredDevices(_ discoveredDevices: [DiscoveredDevice], selected device: DiscoveredDevice) {
        if !appLane.isBusy {
            appLane.clear()
        }
        entryMode = .discover
        var stagedDevices = discoveredDevices
        if !stagedDevices.contains(where: { $0.id == device.id }) {
            stagedDevices.append(device)
        }
        devices = stagedDevices
        password = ""
        savedProfile = nil
        error = nil
        currentStage = nil
        pendingProfileID = nil
        pendingDiscoveredDevice = nil
        activeOperation = nil
        activeLaneKey = nil
        lastProcessedEventCounts[.app] = 0
        select(device)
    }

    func reset() {
        if !appLane.isBusy {
            appLane.clear()
        }
        if let activeLaneKey, activeLaneKey != .app {
            let lane = coordinator.lane(for: activeLaneKey)
            if !lane.isBusy {
                lane.clear()
            }
        }
        devices = []
        selectedDeviceID = nil
        entryMode = .discover
        manualHost = ""
        password = ""
        savedProfile = nil
        error = nil
        currentStage = nil
        pendingProfileID = nil
        pendingDiscoveredDevice = nil
        activeOperation = nil
        activeLaneKey = nil
        lastProcessedEventCounts = [:]
        state = .idle
    }

    func cancel() {
        guard let activeLaneKey else {
            return
        }
        coordinator.cancel(laneKey: activeLaneKey)
    }

    private func resetRunState(clearDevices: Bool) {
        let laneKey = activeLaneKey ?? (state == .discovering ? .app : nil)
        if let laneKey {
            let lane = coordinator.lane(for: laneKey)
            if !lane.isBusy {
                lane.clear()
            }
            lastProcessedEventCounts[laneKey] = 0
        } else if !appLane.isBusy {
            appLane.clear()
            lastProcessedEventCounts[.app] = 0
        }
        error = nil
        currentStage = nil
        savedProfile = nil
        activeOperation = nil
        activeLaneKey = nil
        if clearDevices {
            devices = []
            selectedDeviceID = nil
            if entryMode == .discover {
                manualHost = ""
            }
        }
    }

    private func process(_ events: [BackendEvent], laneKey: OperationLaneKey) {
        var lastProcessedEventCount = lastProcessedEventCounts[laneKey, default: 0]
        if events.count < lastProcessedEventCount {
            lastProcessedEventCount = 0
        }
        guard events.count > lastProcessedEventCount else {
            return
        }
        for event in events.dropFirst(lastProcessedEventCount) {
            handle(event)
        }
        lastProcessedEventCounts[laneKey] = events.count
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "discover" || event.operation == "configure" else {
            return
        }
        guard activeOperation?.operation == event.operation else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }
        if event.type == "error" {
            applyError(event)
            return
        }
        guard event.type == "result" else {
            return
        }
        if event.ok == false {
            failFromResult(event)
            return
        }
        switch event.operation {
        case "discover":
            applyDiscoverResult(event)
        case "configure":
            applyConfigureResult(event)
        default:
            break
        }
    }

    private func applyDiscoverResult(_ event: BackendEvent) {
        do {
            let payload = try event.decodePayload(DiscoverPayload.self)
            devices = payload.devices.enumerated().map { index, device in
                DiscoveredDevice(payload: device, index: index)
            }
            selectedDeviceID = devices.count == 1 ? devices[0].id : nil
            manualHost = devices.count == 1 ? devices[0].host : ""
            state = devices.isEmpty ? .discoveryEmpty : .discoveryReady
            error = nil
            activeOperation = nil
            activeLaneKey = nil
        } catch {
            failContract(error)
        }
    }

    private func applyConfigureResult(_ event: BackendEvent) {
        let configured: ConfiguredDeviceState
        do {
            configured = ConfiguredDeviceState(payload: try event.decodePayload(ConfigurePayload.self))
        } catch {
            failContract(error)
            return
        }

        state = .savingProfile
        let profileID = pendingProfileID ?? UUID().uuidString.lowercased()
        let pendingDiscoveredDevice = pendingDiscoveredDevice
        let password = password
        Task { @MainActor in
            do {
                savedProfile = try await profileSaver.saveConfiguredDevice(
                    configuredDevice: configured,
                    discoveredDevice: pendingDiscoveredDevice,
                    password: password,
                    preferredID: profileID
                )
                error = nil
                state = .saved
                activeOperation = nil
                activeLaneKey = nil
            } catch {
                failProfileSave(error)
            }
        }
    }

    private func applyError(_ event: BackendEvent) {
        error = BackendErrorViewModel(event: event)
        switch event.code {
        case "auth_failed":
            state = .authFailed
        case "unsupported_device":
            state = .unsupported
        default:
            state = .failed
        }
        activeOperation = nil
        activeLaneKey = nil
    }

    private func failFromResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.payloadSummaryText ?? event.summary
        )
        state = .failed
        activeOperation = nil
        activeLaneKey = nil
    }

    private func failContract(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        state = .failed
        activeOperation = nil
        activeLaneKey = nil
    }

    private func failProfileSave(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "profile_save_failed",
            message: error.localizedDescription
        )
        state = .failed
        activeOperation = nil
        activeLaneKey = nil
    }

    private func failLocally(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        state = .failed
        activeOperation = nil
        activeLaneKey = nil
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .failed
        activeOperation = nil
        activeLaneKey = nil
    }

    private func nonNegativeDouble(_ text: String) -> Double? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let value = Double(trimmed), value.isFinite, value >= 0 else {
            return nil
        }
        return value
    }

    private var hasSelectedTarget: Bool {
        switch entryMode {
        case .discover:
            return selectedDevice != nil
        case .manual:
            return !manualHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
}
