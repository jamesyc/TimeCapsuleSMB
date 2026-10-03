import Combine
import Foundation

enum AddDeviceFlowState: String, CaseIterable, Equatable {
    case idle
    case discovering
    case discoveryEmpty
    case discoveryReady
    case manualEntry
    case passwordEntry
    case checkingLocalNetwork
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
        case .checkingLocalNetwork:
            return L10n.string("add_device.state.checking_local_network")
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
    @Published private(set) var reconnectProfileID: DeviceProfile.ID?
    @Published var selectedDeviceID: DiscoveredDevice.ID?
    @Published private(set) var savedProfile: DeviceProfile?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let coordinator: OperationCoordinator
    let registry: DeviceRegistryStore
    let passwordStore: PasswordStore
    let profilePersistence: DeviceProfilePersistenceService
    let discovery: DeviceDiscoveryStore
    let setupWorkflow: DeviceSetupWorkflow

    private let urlOpener: URLOpening
    private var defaultDeviceSettings: DeviceProfileSettings = AppSettings.default.defaultDeviceSettings
    private var appliedDefaultDeviceSettings: DeviceProfileSettings = AppSettings.default.defaultDeviceSettings
    private var appliedDefaultBonjourTimeout = AppSettings.default.defaultBonjourTimeoutSeconds
    private var cancellables: Set<AnyCancellable> = []

    init(
        coordinator: OperationCoordinator,
        registry: DeviceRegistryStore,
        passwordStore: PasswordStore,
        profilePersistence: DeviceProfilePersistenceService? = nil,
        discovery: DeviceDiscoveryStore? = nil,
        setupWorkflow: DeviceSetupWorkflow? = nil,
        localNetworkPreflightChecker: LocalNetworkPreflightChecking? = nil,
        urlOpener: URLOpening = WorkspaceURLOpener()
    ) {
        self.coordinator = coordinator
        self.registry = registry
        self.passwordStore = passwordStore
        self.urlOpener = urlOpener
        let resolvedPersistence = profilePersistence ?? DeviceProfilePersistenceService(
            registry: registry,
            passwordStore: passwordStore
        )
        self.profilePersistence = resolvedPersistence
        self.discovery = discovery ?? DeviceDiscoveryStore(
            coordinator: coordinator,
            registry: registry
        )
        self.setupWorkflow = setupWorkflow ?? DeviceSetupWorkflow(
            coordinator: coordinator,
            profilePersistence: resolvedPersistence,
            localNetworkPreflightChecker: localNetworkPreflightChecker
        )
        observeDiscovery()
        observeSetupWorkflow()
    }

    var devices: [DiscoveredDevice] {
        entryMode == .discover ? discovery.devices : []
    }

    var isRunning: Bool {
        discovery.state == .discovering || discovery.state == .checkingLocalNetwork || setupWorkflow.isRunning
    }

    var canCancel: Bool {
        setupWorkflow.canCancel || discovery.state == .discovering || discovery.state == .checkingLocalNetwork
    }

    var selectedDevice: DiscoveredDevice? {
        guard let selectedDeviceID else {
            return nil
        }
        return discovery.devices.first { $0.id == selectedDeviceID }
    }

    var suggestedProfile: DeviceProfile? {
        guard let device = selectedDevice, reconnectProfileID == nil,
              let suggestion = registry.suggestedProfile(for: device) else { return nil }
        // A name shared by several current appliances cannot suggest one profile.
        let peers = devices.filter { suggestion.network.sharesName(with: $0.observedIdentity) }
        return peers.count == 1 ? suggestion : nil
    }

    var reconnectMessage: String? {
        guard savedProfile == nil, let profile = registry.profile(id: reconnectProfileID) else { return nil }
        return L10n.format("discovery.reconnect_selected", profile.title)
    }

    func reconnectSuggestedProfile() {
        guard let profile = suggestedProfile else { return }
        reconnectProfileID = profile.id
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
        ValueParsers.discoveryTimeout(bonjourTimeout)
    }

    var canConfigure: Bool {
        !isRunning
            && !password.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && currentTarget()?.isEmpty == false
            && !(entryMode == .discover && selectedDevice?.isUnsupportedModel == true)
    }

    func setEntryMode(_ mode: AddDeviceEntryMode) {
        guard entryMode != mode else {
            return
        }
        switch mode {
        case .discover:
            entryMode = .discover
            reconnectProfileID = nil
            selectedDeviceID = nil
            manualHost = ""
            savedProfile = nil
            error = nil
            currentStage = nil
            syncDiscoveryState()
        case .manual:
            startManualEntry()
        }
    }

    func startManualEntry() {
        entryMode = .manual
        state = .manualEntry
        reconnectProfileID = nil
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
        guard coordinator.appLane.isBusy == false else {
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }
        entryMode = .discover
        reconnectProfileID = nil
        selectedDeviceID = nil
        manualHost = ""
        savedProfile = nil
        error = nil
        currentStage = nil
        state = .discovering
        discovery.refresh(timeout: timeout)
    }

    func runConfigure() {
        let trimmedPassword = password.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedPassword.isEmpty else {
            state = .passwordEntry
            failLocally(L10n.string("add_device.error.password_required"))
            return
        }
        guard let target = currentTarget(), !target.isEmpty else {
            failLocally(L10n.string("add_device.error.choose_target"))
            return
        }
        if target.discoveredDevice?.isUnsupportedModel == true {
            rejectUnsupportedModel()
            return
        }

        if let device = target.discoveredDevice, case .conflict = registry.profileMatch(for: device) {
            failLocally(L10n.string("discovery.identity_conflict"))
            return
        }
        let existing = registry.profile(id: reconnectProfileID) ?? target.matchingProfile(in: registry)
        let profileID = existing?.id ?? UUID().uuidString.lowercased()
        let configureSettings = existing?.settings ?? defaultDeviceSettings
        error = nil
        currentStage = nil
        savedProfile = nil
        setupWorkflow.start(
            target: target,
            password: password,
            existingProfile: existing,
            preferredID: profileID,
            settings: configureSettings,
            newProfileSettings: defaultDeviceSettings
        )
        applySetupState(setupWorkflow.state)
    }

    func select(_ device: DiscoveredDevice) {
        entryMode = .discover
        reconnectProfileID = nil
        selectedDeviceID = device.id
        manualHost = device.connectionTarget
        if device.isUnsupportedModel {
            rejectUnsupportedModel()
            return
        }
        if let existing = registry.matchingProfile(for: device) {
            savedProfile = existing
            state = .saved
            error = nil
            return
        }
        if error?.code == WorkflowLocalError.unsupportedDevice.code {
            error = nil
        }
        state = .passwordEntry
    }

    func reset() {
        if discovery.state == .checkingLocalNetwork { discovery.cancel() }
        setupWorkflow.reset()
        reconnectProfileID = nil
        selectedDeviceID = nil
        entryMode = .discover
        manualHost = ""
        password = ""
        savedProfile = nil
        error = nil
        currentStage = nil
        syncDiscoveryState()
    }

    func cancel() {
        if setupWorkflow.canCancel {
            setupWorkflow.cancel()
        } else if discovery.state == .discovering || discovery.state == .checkingLocalNetwork {
            discovery.cancel()
        }
    }

    func handleRecoveryAction(_ action: RecoveryAction) {
        switch action.kind {
        case .retry:
            if error?.operation == "discover" { runDiscover() } else { runConfigure() }
        case .openSystemSettings:
            if let url = LocalNetworkRecovery.settingsURL {
                urlOpener.open(url)
            }
        case .replacePassword:
            state = .passwordEntry
        default:
            break
        }
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

    /// Stops before the password prompt: configure would only turn on SSH and
    /// reboot this AirPort to find out that it cannot run TimeCapsuleSMB.
    private func rejectUnsupportedModel() {
        savedProfile = nil
        password = ""
        currentStage = nil
        error = BackendErrorViewModel(operation: "add-device", localError: .unsupportedDevice)
        state = .unsupported
    }

    private func currentTarget() -> AddDeviceTarget? {
        switch entryMode {
        case .discover:
            guard let selectedDevice else {
                return nil
            }
            return .discovered(selectedDevice)
        case .manual:
            let target = ManualDeviceTarget(host: manualHost)
            return target.host.isEmpty ? nil : .manual(target)
        }
    }

    private func observeDiscovery() {
        discovery.$state
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.syncDiscoveryState()
                }
            }
            .store(in: &cancellables)
        discovery.$devices
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.syncDiscoveryState()
                }
            }
            .store(in: &cancellables)
        discovery.$currentStage
            .sink { [weak self] stage in
                Task { @MainActor in
                    guard let self, self.entryMode == .discover, self.state == .discovering else {
                        return
                    }
                    self.currentStage = stage
                }
            }
            .store(in: &cancellables)
        discovery.$error
            .sink { [weak self] discoveryError in
                Task { @MainActor in
                    guard let self, self.entryMode == .discover, self.discovery.state == .failed else {
                        return
                    }
                    self.error = discoveryError
                }
            }
            .store(in: &cancellables)
    }

    private func observeSetupWorkflow() {
        setupWorkflow.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        setupWorkflow.$state
            .sink { [weak self] workflowState in
                Task { @MainActor in
                    self?.applySetupState(workflowState)
                }
            }
            .store(in: &cancellables)
        setupWorkflow.$currentStage
            .sink { [weak self] stage in
                Task { @MainActor in
                    guard let self, self.isSetupState else {
                        return
                    }
                    self.currentStage = stage
                }
            }
            .store(in: &cancellables)
        setupWorkflow.$error
            .sink { [weak self] error in
                Task { @MainActor in
                    guard let self, self.isSetupState else {
                        return
                    }
                    self.error = error
                }
            }
            .store(in: &cancellables)
        setupWorkflow.$savedProfile
            .sink { [weak self] profile in
                Task { @MainActor in
                    self?.savedProfile = profile
                }
            }
            .store(in: &cancellables)
    }

    private var isSetupState: Bool {
        switch state {
        case .checkingLocalNetwork:
            return setupWorkflow.state == .checkingLocalNetwork
        case .configuring, .awaitingConfirmation, .savingProfile, .saved, .authFailed, .unsupported, .failed:
            return true
        default:
            return false
        }
    }

    private func syncDiscoveryState() {
        guard entryMode == .discover, !isSetupState else {
            return
        }
        switch discovery.state {
        case .idle, .waitingForReadiness, .paused, .readinessBlocked:
            state = discovery.devices.isEmpty ? .idle : .discoveryReady
        case .checkingLocalNetwork:
            state = .checkingLocalNetwork
            currentStage = nil
            error = nil
        case .discovering:
            state = .discovering
            currentStage = discovery.currentStage
            error = nil
        case .empty:
            reconnectProfileID = nil
            selectedDeviceID = nil
            manualHost = ""
            state = .discoveryEmpty
            currentStage = nil
            error = nil
        case .ready:
            if let selectedDeviceID,
               !discovery.devices.contains(where: { $0.id == selectedDeviceID }) {
                self.reconnectProfileID = nil
                self.selectedDeviceID = nil
            }
            // An unsupported model is not preselected: its row shows why, and
            // clicking it explains instead of leaving Save silently disabled.
            if selectedDeviceID == nil, discovery.devices.count == 1, !discovery.devices[0].isUnsupportedModel {
                selectedDeviceID = discovery.devices[0].id
                manualHost = discovery.devices[0].connectionTarget
            }
            state = discovery.devices.isEmpty ? .discoveryEmpty : .discoveryReady
            currentStage = nil
            error = nil
        case .failed:
            error = discovery.error
            currentStage = nil
            state = .failed
        }
    }

    private func applySetupState(_ workflowState: DeviceSetupWorkflowState) {
        switch workflowState {
        case .idle:
            if state == .awaitingConfirmation {
                state = .passwordEntry
            } else if state == .checkingLocalNetwork && discovery.state != .checkingLocalNetwork {
                state = .passwordEntry
            }
        case .checkingLocalNetwork:
            error = nil
            currentStage = nil
            state = .checkingLocalNetwork
        case .configuring:
            error = nil
            currentStage = setupWorkflow.currentStage
            state = .configuring
        case .awaitingConfirmation:
            error = nil
            state = .awaitingConfirmation
        case .savingProfile:
            state = .savingProfile
        case .saved:
            savedProfile = setupWorkflow.savedProfile
            error = nil
            currentStage = nil
            state = .saved
        case .authFailed:
            error = setupWorkflow.error
            currentStage = nil
            state = .authFailed
        case .unsupported:
            error = setupWorkflow.error
            currentStage = nil
            state = .unsupported
        case .failed:
            error = setupWorkflow.error
            currentStage = nil
            state = .failed
        }
    }

    private func failLocally(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        state = .failed
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .failed
    }

    private static func timeoutText(_ value: Double) -> String {
        guard value.rounded() == value else {
            return String(value)
        }
        return String(Int(value))
    }
}
