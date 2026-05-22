import Combine
import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published var selectedDeviceID: DeviceProfile.ID?
    @Published var showingAddDevice = false
    @Published var showingActivity = false
    @Published var showingAppSettings = false

    let appReadinessStore: AppReadinessStore
    let appSettingsStore: AppSettingsStore
    let appUpdateStore: AppUpdateStore
    let deviceRegistry: DeviceRegistryStore
    let operationCoordinator: OperationCoordinator
    let passwordStore: PasswordStore
    let activityStore: ActivityStore
    let discoveryMonitor: DeviceDiscoveryMonitorStore

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
        activityStore: ActivityStore? = nil,
        appUpdateStore: AppUpdateStore? = nil,
        discoveryMonitor: DeviceDiscoveryMonitorStore? = nil
    ) {
        self.appReadinessStore = appReadinessStore
        self.appSettingsStore = appSettingsStore ?? AppSettingsStore()
        self.deviceRegistry = deviceRegistry
        self.operationCoordinator = operationCoordinator
        self.passwordStore = passwordStore
        self.activityStore = activityStore ?? ActivityStore(coordinator: operationCoordinator)
        self.appUpdateStore = appUpdateStore ?? AppUpdateStore(coordinator: operationCoordinator)
        self.discoveryMonitor = discoveryMonitor ?? DeviceDiscoveryMonitorStore(
            coordinator: operationCoordinator,
            readinessStore: appReadinessStore,
            registry: deviceRegistry
        )

        appReadinessStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        self.appSettingsStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        self.appUpdateStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        deviceRegistry.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        operationCoordinator.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        self.activityStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        self.discoveryMonitor.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        deviceRegistry.$profiles
            .sink { [weak self] profiles in
                Task { @MainActor in
                    self?.syncSelection(profiles: profiles)
                }
            }
            .store(in: &cancellables)
    }

    var selectedProfile: DeviceProfile? {
        deviceRegistry.profile(id: selectedDeviceID)
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
        discoveryMonitor.startMonitoring()
        if appSettingsStore.settings.checkForUpdatesOnLaunch {
            appUpdateStore.checkNow(settings: appSettingsStore.settings)
        }
    }

    func select(_ profile: DeviceProfile) {
        selectedDeviceID = profile.id
        showingAddDevice = false
        showingActivity = false
        showingAppSettings = false
    }

    func showAddDevice() {
        selectedDeviceID = nil
        showingAddDevice = true
        showingActivity = false
        showingAppSettings = false
    }

    func showActivity() {
        selectedDeviceID = nil
        showingAddDevice = false
        showingActivity = true
        showingAppSettings = false
    }

    func showAppSettings() {
        selectedDeviceID = nil
        showingAddDevice = false
        showingActivity = false
        showingAppSettings = true
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
        try await appSettingsStore.save(settings)
        applyAppSettings(settings)
        if previousSettings.telemetryEnabled != settings.telemetryEnabled {
            syncTelemetryPreference(settings.telemetryEnabled)
        }
        if previousSettings.helperPathOverride != settings.helperPathOverride {
            appReadinessStore.start()
        }
    }

    func password(for profile: DeviceProfile) -> String? {
        if profile.passwordState == .invalid {
            return nil
        }
        do {
            return try passwordStore.password(for: profile.keychainAccount)
        } catch PasswordStoreError.missing {
            Task { await deviceRegistry.updatePasswordState(.missing, for: profile.id) }
            return nil
        } catch {
            Task { await deviceRegistry.updatePasswordState(.keychainUnavailable, for: profile.id) }
            return nil
        }
    }

    func savePassword(_ password: String, for profile: DeviceProfile) async throws {
        try passwordStore.save(password, for: profile.keychainAccount)
        await deviceRegistry.updatePasswordState(.available, for: profile.id)
    }

    @discardableResult
    func saveProfileEdits(profile: DeviceProfile, fields: DeviceProfileEditableFields) async throws -> DeviceProfile {
        var updated = profile
        updated.displayName = fields.displayName
        updated.settings = fields.settings
        return try await deviceRegistry.updateProfile(updated)
    }

    func forget(_ profile: DeviceProfile) async throws {
        try passwordStore.deletePassword(for: profile.keychainAccount)
        try await deviceRegistry.delete(profile)
        if selectedDeviceID == profile.id {
            selectedDeviceID = deviceRegistry.profiles.first?.id
            showingAddDevice = false
            showingActivity = false
            showingAppSettings = false
        }
    }

    func refreshPasswordStates() async {
        for profile in deviceRegistry.profiles {
            await deviceRegistry.updatePasswordState(effectivePasswordState(for: profile), for: profile.id)
        }
    }

    private func effectivePasswordState(for profile: DeviceProfile) -> DevicePasswordState {
        if profile.passwordState == .invalid {
            return .invalid
        }
        return passwordStore.state(for: profile.keychainAccount)
    }

    private func applyAppSettings(_ settings: AppSettings) {
        if backend.helperPath != settings.helperPathOverride {
            backend.helperPath = settings.helperPathOverride
        }
        discoveryMonitor.applyAppSettings(settings)
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
        if let selectedDeviceID, profiles.contains(where: { $0.id == selectedDeviceID }) {
            return
        }
        selectedDeviceID = profiles.first?.id
        if !profiles.isEmpty {
            showingAddDevice = false
            showingActivity = false
            showingAppSettings = false
        }
    }
}
