import Combine
import Foundation

@MainActor
final class AppStore: ObservableObject {
    private enum PasswordRollback {
        case delete
        case restore(String)
    }

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
        activityStore: ActivityStore? = nil,
        appUpdateStore: AppUpdateStore? = nil,
        discoveryMonitor: DeviceDiscoveryMonitorStore? = nil,
        reachabilityStore: DeviceReachabilityStore? = nil
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
        self.reachabilityStore = reachabilityStore ?? DeviceReachabilityStore(coordinator: operationCoordinator)

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
        self.reachabilityStore.objectWillChange
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
        if previousSettings.helperPathOverride != settings.helperPathOverride
            || readinessVersionCheck(for: previousSettings) != readinessVersionCheck(for: settings)
        {
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

    @discardableResult
    func saveProfileEdits(
        profile: DeviceProfile,
        fields: DeviceProfileEditableFields,
        replacementPassword: String? = nil
    ) async throws -> DeviceProfile {
        var updated = profile
        updated.displayName = fields.displayName
        updated.settings = fields.settings

        let rollback: PasswordRollback?
        if let replacementPassword {
            rollback = try passwordRollback(for: profile.keychainAccount)
            try passwordStore.save(replacementPassword, for: profile.keychainAccount)
            updated.passwordState = .available
        } else {
            rollback = nil
        }

        do {
            return try await deviceRegistry.updateProfile(updated)
        } catch {
            if let rollback {
                rollbackPassword(rollback, account: profile.keychainAccount)
            }
            throw error
        }
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

    private func effectivePasswordState(for profile: DeviceProfile) -> DevicePasswordState {
        if profile.passwordState == .invalid {
            return .invalid
        }
        return passwordStore.state(for: profile.keychainAccount)
    }

    private func applyAppSettings(_ settings: AppSettings) {
        let previousLanguage = L10n.currentLanguage
        L10n.apply(language: settings.language)
        if backend.helperPath != settings.helperPathOverride {
            backend.helperPath = settings.helperPathOverride
        }
        appReadinessStore.applyVersionCheck(readinessVersionCheck(for: settings))
        discoveryMonitor.applyAppSettings(settings)
        if previousLanguage != settings.language {
            objectWillChange.send()
        }
    }

    private func readinessVersionCheck(for settings: AppSettings) -> AppReadinessVersionCheck {
        AppReadinessVersionCheck(url: settings.versionCheckURL)
    }

    private func passwordRollback(for account: String) throws -> PasswordRollback {
        do {
            return .restore(try passwordStore.password(for: account))
        } catch PasswordStoreError.missing {
            return .delete
        } catch {
            throw error
        }
    }

    private func rollbackPassword(_ rollback: PasswordRollback, account: String) {
        switch rollback {
        case .delete:
            try? passwordStore.deletePassword(for: account)
        case .restore(let password):
            try? passwordStore.save(password, for: account)
        }
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
