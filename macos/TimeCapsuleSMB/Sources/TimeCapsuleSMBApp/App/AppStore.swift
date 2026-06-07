import Combine
import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published private(set) var route: AppRoute = .allDevices

    let appReadinessStore: AppReadinessStore
    let appSettingsStore: AppSettingsStore
    let appUpdateStore: AppUpdateStore
    let deviceRegistry: DeviceRegistryStore
    let operationCoordinator: OperationCoordinator
    let passwordStore: PasswordStore
    let profilePersistence: DeviceProfilePersistenceService
    let activityStore: ActivityStore
    let deviceDiscovery: DeviceDiscoveryStore
    let reachabilityStore: DeviceReachabilityStore

    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        let coordinator = OperationCoordinator()
        self.init(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: AppSettingsStore(),
            deviceRegistry: DeviceRegistryStore(),
            operationCoordinator: coordinator,
            passwordStore: KeychainPasswordStore(),
            activityStore: ActivityStore(coordinator: coordinator)
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
        appUpdateStore: AppUpdateStore? = nil,
        deviceDiscovery: DeviceDiscoveryStore? = nil,
        reachabilityStore: DeviceReachabilityStore? = nil
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
        self.appUpdateStore = appUpdateStore ?? AppUpdateStore(coordinator: operationCoordinator)
        self.deviceDiscovery = deviceDiscovery ?? DeviceDiscoveryStore(
            coordinator: operationCoordinator,
            readinessStore: appReadinessStore,
            registry: deviceRegistry
        )
        self.reachabilityStore = reachabilityStore ?? DeviceReachabilityStore(coordinator: operationCoordinator)

        deviceRegistry.$profiles
            .sink { [weak self] profiles in
                self?.syncSelection(profiles: profiles)
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

    var showingAppSettings: Bool {
        route == .appSettings
    }

    var backend: BackendClient {
        operationCoordinator.appLane.backend
    }

    func start() async {
        await appSettingsStore.load()
        applyAppSettings(appSettingsStore.settings)
        await deviceRegistry.load()
        await refreshPasswordStates()
        appReadinessStore.start()
        deviceDiscovery.startMonitoring()
        if appSettingsStore.settings.checkForUpdatesOnLaunch {
            appUpdateStore.checkNow(settings: appSettingsStore.settings)
        }
    }

    func navigate(to route: AppRoute) {
        self.route = normalizedRoute(route)
    }

    func select(_ profile: DeviceProfile) {
        navigate(to: .device(profile.id))
    }

    func showAddDevice() {
        navigate(to: .addDevice)
    }

    func showActivity() {
        navigate(to: .activity)
    }

    func showAppSettings() {
        navigate(to: .appSettings)
    }

    func showAllDevices() {
        navigate(to: .allDevices)
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
            self?.applyAppSettings(settings)
        }
        if previousSettings.telemetryEnabled != settings.telemetryEnabled {
            syncTelemetryPreference(settings.telemetryEnabled)
        }
        if previousSettings.helperPathOverride != settings.helperPathOverride
            || readinessVersionCheck(for: previousSettings) != readinessVersionCheck(for: settings)
        {
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
        replacementPassword: String? = nil
    ) async throws -> DeviceProfile {
        try await profilePersistence.saveProfileEdits(
            profile: profile,
            fields: fields,
            replacementPassword: replacementPassword
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
        DiagnosticsExportContext(
            generatedAt: Date(),
            appVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
            appBuild: Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development",
            applicationSupportPath: appSettingsStore.settingsURL.deletingLastPathComponent().path,
            helperPath: backend.helperPath,
            appSettings: appSettingsStore.settings,
            readinessState: appReadinessStore.state.kind,
            readinessVersionPayload: appReadinessStore.versionCheckPayload,
            capabilities: appReadinessStore.capabilities,
            validation: appReadinessStore.validation,
            runtimeIssues: appReadinessStore.issues,
            updateState: appUpdateStore.state,
            updatePayload: appUpdateStore.payload,
            updateError: appUpdateStore.error,
            selectedProfile: selectedProfile,
            activeOperations: operationCoordinator.activeOperations,
            pendingConfirmation: operationCoordinator.pendingConfirmation,
            events: includeBackendEvents ? operationCoordinator.allLanes.flatMap { $0.backend.events } : []
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
        switch profilePersistence.credential(for: profile) {
        case .available:
            return .available
        case .missing:
            return .missing
        case .invalid:
            return .invalid
        case .unavailable:
            return .keychainUnavailable
        }
    }

    private func applyAppSettings(_ settings: AppSettings) {
        let previousLanguage = L10n.currentLanguage
        L10n.apply(language: settings.language)
        if backend.helperPath != settings.helperPathOverride {
            backend.helperPath = settings.helperPathOverride
        }
        appReadinessStore.applyVersionCheck(readinessVersionCheck(for: settings))
        deviceDiscovery.applyAppSettings(settings)
        if previousLanguage != settings.language {
            activityStore.refresh()
            objectWillChange.send()
        }
    }

    private func readinessVersionCheck(for settings: AppSettings) -> AppReadinessVersionCheck {
        AppReadinessVersionCheck(url: settings.versionCheckURL)
    }

    private func syncTelemetryPreference(_ enabled: Bool) {
        let params: [String: JSONValue] = ["enabled": .bool(enabled)]
        _ = operationCoordinator.run(
            operation: "set-telemetry",
            params: params,
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
}
