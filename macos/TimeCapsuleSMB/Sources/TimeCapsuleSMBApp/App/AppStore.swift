import Combine
import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published var selectedDeviceID: DeviceProfile.ID?
    @Published var showingAddDevice = false

    let appReadinessStore: AppReadinessStore
    let deviceRegistry: DeviceRegistryStore
    let operationCoordinator: OperationCoordinator
    let passwordStore: PasswordStore
    let activityStore: ActivityStore

    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        let coordinator = OperationCoordinator()
        self.init(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: DeviceRegistryStore(),
            operationCoordinator: coordinator,
            passwordStore: KeychainPasswordStore(),
            activityStore: ActivityStore(coordinator: coordinator)
        )
    }

    init(
        appReadinessStore: AppReadinessStore,
        deviceRegistry: DeviceRegistryStore,
        operationCoordinator: OperationCoordinator,
        passwordStore: PasswordStore,
        activityStore: ActivityStore? = nil
    ) {
        self.appReadinessStore = appReadinessStore
        self.deviceRegistry = deviceRegistry
        self.operationCoordinator = operationCoordinator
        self.passwordStore = passwordStore
        self.activityStore = activityStore ?? ActivityStore(coordinator: operationCoordinator)

        appReadinessStore.objectWillChange
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
        operationCoordinator.backend
    }

    func start() async {
        await deviceRegistry.load()
        await refreshPasswordStates()
        appReadinessStore.start()
    }

    func select(_ profile: DeviceProfile) {
        selectedDeviceID = profile.id
        showingAddDevice = false
    }

    func showAddDevice() {
        selectedDeviceID = nil
        showingAddDevice = true
    }

    func dashboardSummary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        let passwordState = effectivePasswordState(for: profile)
        let displayStatus = DeviceStatusPolicy.status(
            for: profile,
            passwordState: passwordState,
            activeOperation: operationCoordinator.activeOperation
        )
        let primaryAction = DashboardPrimaryActionPolicy.primaryAction(
            for: profile,
            passwordState: passwordState,
            activeOperation: operationCoordinator.activeOperation
        )
        return DeviceDashboardSummary(
            profile: profile,
            passwordState: passwordState,
            displayStatus: displayStatus,
            primaryAction: primaryAction,
            hostWarning: HostCompatibilityPolicy.warning()
        )
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

    private func syncSelection(profiles: [DeviceProfile]) {
        if let selectedDeviceID, profiles.contains(where: { $0.id == selectedDeviceID }) {
            return
        }
        selectedDeviceID = profiles.first?.id
        if !profiles.isEmpty {
            showingAddDevice = false
        }
    }
}
