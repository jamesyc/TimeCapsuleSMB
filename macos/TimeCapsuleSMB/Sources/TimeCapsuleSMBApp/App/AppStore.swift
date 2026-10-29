import Combine
import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published private(set) var route: AppRoute = .allDevices {
        didSet {
            if let justSavedProfileID, route != .device(justSavedProfileID) {
                self.justSavedProfileID = nil
            }
        }
    }
    /// The device Add Device just saved: its page says so until you leave it.
    @Published private(set) var justSavedProfileID: DeviceProfile.ID?

    let appReadinessStore: AppReadinessStore
    let appSettingsStore: AppSettingsStore
    let deviceRegistry: DeviceRegistryStore
    let operationCoordinator: OperationCoordinator
    let passwordStore: PasswordStore
    let profilePersistence: DeviceProfilePersistenceService
    let activityStore: ActivityStore
    let deviceDiscovery: DeviceDiscoveryStore
    let reachabilityStore: DeviceReachabilityStore
    let sshAccessStore: DeviceSSHAccessStore
    let localNetworkPreflightChecker: LocalNetworkPreflightChecking?
    /// Nil in tests, which must never write the test runner's own language.
    let perAppLanguage: PerAppLanguagePreference?

    private var cancellables: Set<AnyCancellable> = []
    /// The last finished event of each lane the learner has seen.
    private var checkedIdentityEvents: [OperationLaneKey: UUID] = [:]

    convenience init() {
        let coordinator = OperationCoordinator()
        self.init(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: AppSettingsStore(),
            deviceRegistry: DeviceRegistryStore(),
            operationCoordinator: coordinator,
            passwordStore: KeychainPasswordStore(),
            activityStore: ActivityStore(coordinator: coordinator),
            localNetworkPreflightChecker: BonjourLocalNetworkPreflightChecker(),
            perAppLanguage: Bundle.main.bundleIdentifier.map { PerAppLanguagePreference(defaults: .standard, domain: $0) }
        )
    }

    init(
        appReadinessStore: AppReadinessStore,
        appSettingsStore: AppSettingsStore? = nil,
        deviceRegistry: DeviceRegistryStore,
        operationCoordinator: OperationCoordinator,
        passwordStore: PasswordStore,
        profilePersistence: DeviceProfilePersistenceService? = nil,
        activityStore: ActivityStore? = nil,
        deviceDiscovery: DeviceDiscoveryStore? = nil,
        reachabilityStore: DeviceReachabilityStore? = nil,
        sshAccessStore: DeviceSSHAccessStore? = nil,
        localNetworkPreflightChecker: LocalNetworkPreflightChecking? = nil,
        perAppLanguage: PerAppLanguagePreference? = nil
    ) {
        self.appReadinessStore = appReadinessStore
        self.appSettingsStore = appSettingsStore ?? AppSettingsStore()
        self.deviceRegistry = deviceRegistry
        self.operationCoordinator = operationCoordinator
        self.passwordStore = passwordStore
        self.profilePersistence = profilePersistence ?? DeviceProfilePersistenceService(
            registry: deviceRegistry,
            passwordStore: passwordStore
        )
        self.activityStore = activityStore ?? ActivityStore(coordinator: operationCoordinator)
        self.deviceDiscovery = deviceDiscovery ?? DeviceDiscoveryStore(
            coordinator: operationCoordinator,
            readinessStore: appReadinessStore,
            registry: deviceRegistry,
            localNetworkPreflightChecker: localNetworkPreflightChecker
        )
        self.reachabilityStore = reachabilityStore ?? DeviceReachabilityStore(coordinator: operationCoordinator)
        self.sshAccessStore = sshAccessStore ?? DeviceSSHAccessStore(coordinator: operationCoordinator)
        self.localNetworkPreflightChecker = localNetworkPreflightChecker
        self.perAppLanguage = perAppLanguage

        deviceRegistry.$profiles
            .sink { [weak self] profiles in
                self?.syncSelection(profiles: profiles)
            }
            .store(in: &cancellables)
        // @Published sends before the property changes: read the new state on
        // the next main-actor turn, or the first discovery's devices are missed.
        self.deviceDiscovery.$devices
            .sink { [weak self] _ in
                Task { @MainActor in self?.refreshSSHAccessForDiscoveredProfiles() }
            }
            .store(in: &cancellables)
        operationCoordinator.$lanesRevision
            .sink { [weak self] _ in
                Task { @MainActor in self?.saveConfirmedAirportIdentities() }
            }
            .store(in: &cancellables)
    }

    var selectedProfile: DeviceProfile? {
        deviceRegistry.profile(id: selectedDeviceID)
    }

    var selectedDeviceID: DeviceProfile.ID? {
        route.selectedDeviceID
    }

    var showingAddDevice: Bool {
        route == .addDevice
    }

    var showingActivity: Bool {
        route == .activity
    }

    var backend: BackendClient {
        operationCoordinator.appLane.backend
    }

    var canCancelSelectedOperation: Bool {
        if let selectedDeviceID, operationCoordinator.isDeviceBusy(selectedDeviceID) {
            return operationCoordinator.canCancel(profileID: selectedDeviceID)
        }
        return deviceDiscovery.state == .checkingLocalNetwork || operationCoordinator.canCancel
    }

    func cancelSelectedOperation() {
        if let selectedDeviceID, operationCoordinator.isDeviceBusy(selectedDeviceID) {
            operationCoordinator.cancel(profileID: selectedDeviceID)
        } else if deviceDiscovery.state == .checkingLocalNetwork {
            deviceDiscovery.cancel()
        } else {
            operationCoordinator.cancel()
        }
    }

    func start() async {
        await appSettingsStore.load()
        perAppLanguage?.adopt(appSettingsStore.settings.language)
        applyAppSettings(appSettingsStore.settings)
        await deviceRegistry.load()
        await refreshPasswordStates()
        appReadinessStore.start()
        deviceDiscovery.startMonitoring()
    }

    func navigate(to route: AppRoute) {
        self.route = normalizedRoute(route)
    }

    func select(_ profile: DeviceProfile) {
        navigate(to: .device(profile.id))
    }

    /// Opens the device Add Device just saved; its page says so until you leave it.
    func openJustSaved(_ profile: DeviceProfile) {
        select(profile)
        if route == .device(profile.id) {
            justSavedProfileID = profile.id
        }
    }

    func showAddDevice() {
        navigate(to: .addDevice)
    }

    func dashboardSummary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        let passwordState = effectivePasswordState(for: profile)
        let activeOperation = operationCoordinator.activeOperation(for: profile)
        let displayStatus = DeviceStatusPolicy.status(
            for: profile,
            passwordState: passwordState,
            activeOperation: activeOperation
        )
        let primaryAction = DashboardPrimaryActionPolicy.primaryAction(
            for: profile,
            passwordState: passwordState,
            activeOperation: activeOperation
        )
        return DeviceDashboardSummary(
            profile: profile,
            passwordState: passwordState,
            displayStatus: displayStatus,
            primaryAction: primaryAction,
            hostWarning: HostCompatibilityPolicy.warning(enabled: appSettingsStore.settings.timeMachineWarningsEnabled)
        )
    }

    func saveAppSettings(_ settings: AppSettings) async throws {
        let previousSettings = appSettingsStore.settings
        try await appSettingsStore.save(settings) { [weak self] settings in
            // Only on a changed choice, so a language set in System Settings
            // survives saving anything else; and before applying, so System
            // is settled without our earlier choice.
            if previousSettings.language != settings.language {
                self?.perAppLanguage?.follow(from: previousSettings.language, to: settings.language)
            }
            self?.applyAppSettings(settings)
        }
        if previousSettings.telemetryEnabled != settings.telemetryEnabled {
            syncTelemetryPreference(settings.telemetryEnabled)
        }
        if previousSettings.helperPathOverride != settings.helperPathOverride {
            appReadinessStore.start()
        }
    }

    func password(for profile: DeviceProfile) -> String? {
        profilePersistence.credential(for: profile).password
    }

    @discardableResult
    func saveProfileEdits(
        profile: DeviceProfile,
        fields: DeviceProfileEditableFields,
        replacementPassword: String? = nil,
        stagedConfigURL: URL? = nil
    ) async throws -> DeviceProfile {
        try await profilePersistence.saveProfileEdits(
            profile: profile,
            fields: fields,
            replacementPassword: replacementPassword,
            stagedConfigURL: stagedConfigURL
        )
    }

    func forget(_ profile: DeviceProfile) async throws {
        let wasSelectedProfile = selectedDeviceID == profile.id
        try await profilePersistence.forget(profile)
        if wasSelectedProfile {
            route = firstProfileRoute() ?? .allDevices
        }
    }

    func refreshPasswordStates() async {
        await profilePersistence.refreshCredentialStates()
    }

    func diagnosticsExportContext(includeBackendEvents: Bool = true) -> DiagnosticsExportContext {
        let fallbackProfile = selectedProfile == nil
            ? deviceRegistry.profiles
                .filter { $0.lastDeployState?.status == .failed }
                .max { ($0.lastDeployState?.updatedAt ?? .distantPast) < ($1.lastDeployState?.updatedAt ?? .distantPast) }
            : nil
        let exportProfile = selectedProfile ?? fallbackProfile
        let eventLanes: [(key: OperationLaneKey, events: [BackendEvent])]
        if includeBackendEvents {
            eventLanes = operationCoordinator.allLanes.compactMap { lane in
                switch lane.key {
                case .app, .appWorkflow:
                    return (key: lane.key, events: lane.backend.events)
                case .device(let profileID), .deviceWorkflow(let profileID, _):
                    return profileID == exportProfile?.id ? (key: lane.key, events: lane.backend.events) : nil
                case .candidateHost, .localPath:
                    return nil
                }
            }
        } else {
            eventLanes = []
        }
        return DiagnosticsExportContext(
            generatedAt: Date(),
            appVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
            appBuild: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development",
            applicationSupportPath: appSettingsStore.settingsURL.deletingLastPathComponent().path,
            helperPath: backend.helperPath,
            appSettings: appSettingsStore.settings,
            updateChecks: UpdateCheckDiagnostics.read(),
            readinessState: appReadinessStore.state.kind,
            readinessVersionPayload: appReadinessStore.versionCheckPayload,
            capabilities: appReadinessStore.capabilities,
            validation: appReadinessStore.validation,
            runtimeIssues: appReadinessStore.issues,
            selectedProfile: exportProfile,
            selectedProfileIsFallback: fallbackProfile != nil,
            activeOperations: operationCoordinator.activeOperations,
            pendingConfirmation: operationCoordinator.pendingConfirmation,
            eventLanes: eventLanes
        )
    }

    private func normalizedRoute(_ route: AppRoute) -> AppRoute {
        guard case .device(let profileID) = route else {
            return route
        }
        if deviceRegistry.profile(id: profileID) != nil {
            return route
        }
        return firstProfileRoute() ?? .allDevices
    }

    private func firstProfileRoute() -> AppRoute? {
        deviceRegistry.profiles.first.map { .device($0.id) }
    }

    private func effectivePasswordState(for profile: DeviceProfile) -> DevicePasswordState {
        profilePersistence.credentialState(for: profile)
    }

    private func applyAppSettings(_ settings: AppSettings) {
        let previousLanguage = L10n.currentLanguage
        L10n.apply(language: settings.language, systemPreferences: perAppLanguage?.languages ?? L10n.macOSLanguages())
        if backend.helperPath != settings.helperPathOverride {
            backend.helperPath = settings.helperPathOverride
        }
        appReadinessStore.enableVersionCheck()
        deviceDiscovery.applyAppSettings(settings)
        if previousLanguage != settings.language {
            activityStore.refresh()
            objectWillChange.send()
            AppLanguageChanges.shared.objectWillChange.send()
        }
    }

    private func syncTelemetryPreference(_ enabled: Bool) {
        _ = operationCoordinator.run(
            operation: "set-telemetry",
            params: OperationParams.Readiness.setTelemetry(enabled: enabled),
            laneKey: .localPath("app-settings")
        )
    }

    private func syncSelection(profiles: [DeviceProfile]) {
        guard case .device(let selectedDeviceID) = route else {
            return
        }
        if profiles.contains(where: { $0.id == selectedDeviceID }) {
            return
        }
        route = profiles.first.map { .device($0.id) } ?? .allDevices
    }

    private func refreshSSHAccessForDiscoveredProfiles() {
        for profile in deviceRegistry.profiles where deviceDiscovery.currentDiscoveredDevice(for: profile) != nil {
            // With the password, the check of a device seen at its saved
            // address also reads its MAC, which an old profile does not have.
            // Read only if the check runs: not on every discovery change.
            sshAccessStore.refreshIfNeeded(profile: profile,
                                           password: profile.network.airportMAC == nil ? self.password(for: profile) : nil)
        }
    }

    /// The identity a finished operation confirmed, saved with its profile.
    /// Every lane counts, checkups and the background SSH check before any
    /// dashboard opens included; the registry decides whether the device is
    /// the saved one, against what discovery sees now.
    private func saveConfirmedAirportIdentities() {
        for lane in operationCoordinator.allLanes where !lane.backend.isRunning {
            guard let profileID = lane.key.deviceProfileID,
                  let event = lane.backend.events.last(where: { $0.type == "error" || $0.type == "result" }),
                  checkedIdentityEvents.updateValue(event.id, forKey: lane.key) != event.id,
                  let identity = AirportIdentityObservation(event: event) else { continue }
            let visible = deviceDiscovery.devices.map(\.observedIdentity)
            deviceRegistry.enqueueOperationUpdate { [deviceRegistry] in
                await deviceRegistry.confirmAirportIdentity(identity, for: profileID, visible: visible)
            }
        }
    }
}
