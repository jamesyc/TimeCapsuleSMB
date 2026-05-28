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
    case awaitingConfirmation
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
        case .awaitingConfirmation:
            return L10n.string("add_device.state.awaiting_confirmation")
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
    let profilePersistence: DeviceProfilePersistenceService
    private let appLane: OperationLane

    private var pendingConfigureDraft: ConfigureProfileDraft?
    private var defaultDeviceSettings: DeviceProfileSettings = AppSettings.default.defaultDeviceSettings
    private var appliedDefaultDeviceSettings: DeviceProfileSettings = AppSettings.default.defaultDeviceSettings
    private var appliedDefaultBonjourTimeout = AppSettings.default.defaultBonjourTimeoutSeconds
    private var activeLaneKey: OperationLaneKey?
    private var operationObservers: [OperationLaneKey: BackendOperationObserver] = [:]
    private var cancellables: Set<AnyCancellable> = []
    private var observedLaneKeys: Set<OperationLaneKey> = []

    init(
        coordinator: OperationCoordinator,
        registry: DeviceRegistryStore,
        passwordStore: PasswordStore,
        profilePersistence: DeviceProfilePersistenceService? = nil
    ) {
        self.coordinator = coordinator
        self.registry = registry
        self.passwordStore = passwordStore
        self.profilePersistence = profilePersistence ?? DeviceProfilePersistenceService(registry: registry, passwordStore: passwordStore)
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
            return coordinator.lane(for: key).isBusy
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
        ValueParsers.nonNegativeDouble(bonjourTimeout)
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
            activeLaneKey = .app
            observer(for: .app).start(operation)
            state = .discovering
            process(appLane.backend.events, laneKey: .app)
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

        let targetHost = selectedDevice?.connectionTarget ?? trimmedHost
        let existing = selectedDevice.map { registry.matchingProfile(for: $0) }
            ?? registry.matchingProfile(host: targetHost, bonjourFullname: nil)
        let profileID = existing?.id ?? UUID().uuidString.lowercased()
        let configureSettings = existing?.settings ?? defaultDeviceSettings

        let laneKey = OperationLaneKey.device(profileID)
        let lane = coordinator.lane(for: laneKey)
        observe(lane: lane)

        guard !lane.isBusy else {
            clearPendingConfigureDraft()
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }
        let configureDraft: ConfigureProfileDraft
        do {
            configureDraft = try profilePersistence.prepareConfigureTarget(
                targetHost: targetHost,
                discoveredDevice: selectedDevice,
                existingProfile: existing,
                preferredID: profileID,
                settings: configureSettings
            )
        } catch {
            failProfileSave(error)
            return
        }
        resetRunState(clearDevices: false)
        pendingConfigureDraft = configureDraft
        observer(for: laneKey).clear()
        switch coordinator.run(
            operation: "configure",
            params: OperationParams.configure(
                host: targetHost,
                selectedRecord: selectedDevice?.rawRecord,
                password: password,
                debugLogging: configureSettings.debugLogging,
                internalShareUseDiskRoot: configureSettings.internalShareUseDiskRoot,
                anyProtocol: configureSettings.anyProtocol,
                ataIdleSeconds: configureSettings.ataIdleSeconds,
                ataStandby: configureSettings.ataStandby,
                includeAtaStandby: true
            ),
            context: configureDraft.context,
            activeDeviceID: profileID,
            laneKey: laneKey
        ) {
        case .started(let operation):
            activeLaneKey = laneKey
            observer(for: laneKey).start(operation)
            state = .configuring
            process(lane.backend.events, laneKey: laneKey)
        case .rejected(let message):
            clearPendingConfigureDraft()
            rejectRun(message)
        }
    }

    func select(_ device: DiscoveredDevice) {
        entryMode = .discover
        selectedDeviceID = device.id
        manualHost = device.connectionTarget
        if let existing = registry.matchingProfile(for: device) {
            savedProfile = existing
            state = .saved
            error = nil
            return
        }
        state = .passwordEntry
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
        clearPendingConfigureDraft()
        activeLaneKey = nil
        observer(for: .app).clear()
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
        clearPendingConfigureDraft()
        activeLaneKey = nil
        operationObservers = [:]
        state = .idle
    }

    func cancel() {
        guard let activeLaneKey else {
            return
        }
        coordinator.cancel(laneKey: activeLaneKey)
    }

    func applyAppSettings(_ settings: AppSettings) {
        let previousDefaultTimeout = Self.timeoutText(appliedDefaultBonjourTimeout)
        if bonjourTimeout == previousDefaultTimeout {
            bonjourTimeout = Self.timeoutText(settings.defaultBonjourTimeoutSeconds)
        }
        appliedDefaultBonjourTimeout = settings.defaultBonjourTimeoutSeconds
        defaultDeviceSettings = settings.defaultDeviceSettings
        if debugLogging == appliedDefaultDeviceSettings.debugLogging {
            debugLogging = settings.defaultDeviceSettings.debugLogging
        }
        appliedDefaultDeviceSettings = settings.defaultDeviceSettings
    }

    private func resetRunState(clearDevices: Bool) {
        let laneKey = activeLaneKey ?? (state == .discovering ? .app : nil)
        if let laneKey {
            let lane = coordinator.lane(for: laneKey)
            if !lane.isBusy {
                lane.clear()
            }
            observer(for: laneKey).clear()
        } else if !appLane.isBusy {
            appLane.clear()
            observer(for: .app).clear()
        }
        error = nil
        currentStage = nil
        savedProfile = nil
        activeLaneKey = nil
        clearPendingConfigureDraft()
        if clearDevices {
            devices = []
            selectedDeviceID = nil
            if entryMode == .discover {
                manualHost = ""
            }
        }
    }

    private func process(_ events: [BackendEvent], laneKey: OperationLaneKey) {
        observer(for: laneKey).process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "discover" || event.operation == "configure" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if event.operation == "configure", state == .awaitingConfirmation {
                state = .configuring
            }
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
            manualHost = devices.count == 1 ? devices[0].connectionTarget : ""
            state = devices.isEmpty ? .discoveryEmpty : .discoveryReady
            error = nil
            finishActiveOperation()
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
        guard let configureDraft = pendingConfigureDraft else {
            failContract(DeviceRegistryError.profileNotFound("pending"))
            return
        }
        let password = password
        let overrides = ConfiguredDeviceProfileOverrides(
            displayName: nil,
            settings: configureDraft.existingProfileID == nil ? defaultDeviceSettings : nil
        )
        Task { @MainActor in
            do {
                savedProfile = try await profilePersistence.commitConfiguredProfile(
                    configuredDevice: configured,
                    draft: configureDraft,
                    password: password,
                    overrides: overrides
                )
                error = nil
                state = .saved
                finishActiveOperation()
                clearPendingConfigureDraft()
            } catch {
                failProfileSave(error)
            }
        }
    }

    private func applyError(_ event: BackendEvent) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled()
            return
        }
        error = BackendErrorViewModel(event: event)
        switch event.code {
        case "auth_failed":
            state = .authFailed
        case "unsupported_device":
            state = .unsupported
        default:
            state = .failed
        }
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        savedProfile = nil
        finishActiveOperation()
        clearPendingConfigureDraft()
        state = .passwordEntry
    }

    private func failFromResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.payloadSummaryText ?? event.summary
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func failContract(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func failProfileSave(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "profile_save_failed",
            message: error.localizedDescription
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func failLocally(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
    }

    private func observer(for laneKey: OperationLaneKey) -> BackendOperationObserver {
        if let observer = operationObservers[laneKey] {
            return observer
        }
        let observer = BackendOperationObserver()
        operationObservers[laneKey] = observer
        return observer
    }

    private func finishActiveOperation() {
        if let activeLaneKey {
            operationObservers[activeLaneKey]?.finish()
        }
        activeLaneKey = nil
    }

    private func clearPendingConfigureDraft() {
        profilePersistence.discardConfigureDraft(pendingConfigureDraft)
        pendingConfigureDraft = nil
    }

    private static func timeoutText(_ value: Double) -> String {
        guard value.rounded() == value else {
            return String(value)
        }
        return String(Int(value))
    }
}
