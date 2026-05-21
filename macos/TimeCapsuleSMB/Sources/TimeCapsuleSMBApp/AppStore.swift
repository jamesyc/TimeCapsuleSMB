import Combine
import Foundation

enum DashboardPrimaryAction: String, Equatable {
    case addDevice
    case replacePassword
    case runCheckup
    case installSMB
    case viewCheckup
    case openSMB
}

struct DeviceDashboardSummary: Equatable {
    let profile: DeviceProfile
    let passwordState: DevicePasswordState
    let primaryAction: DashboardPrimaryAction
    let hostWarning: HostCompatibilityWarning?
}

@MainActor
final class AppStore: ObservableObject {
    @Published var selectedDeviceID: DeviceProfile.ID?
    @Published var showingAddDevice = false

    let appReadinessStore: AppReadinessStore
    let deviceRegistry: DeviceRegistryStore
    let operationCoordinator: OperationCoordinator
    let passwordStore: PasswordStore

    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        let coordinator = OperationCoordinator()
        self.init(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: DeviceRegistryStore(),
            operationCoordinator: coordinator,
            passwordStore: KeychainPasswordStore()
        )
    }

    init(
        appReadinessStore: AppReadinessStore,
        deviceRegistry: DeviceRegistryStore,
        operationCoordinator: OperationCoordinator,
        passwordStore: PasswordStore
    ) {
        self.appReadinessStore = appReadinessStore
        self.deviceRegistry = deviceRegistry
        self.operationCoordinator = operationCoordinator
        self.passwordStore = passwordStore

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

    func start() {
        deviceRegistry.load()
        refreshPasswordStates()
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
        let passwordState = passwordStore.state(for: profile.keychainAccount)
        let primaryAction: DashboardPrimaryAction
        if passwordState != .available {
            primaryAction = .replacePassword
        } else if profile.lastCheckup == nil {
            primaryAction = .runCheckup
        } else if profile.lastDeploy == nil {
            primaryAction = .installSMB
        } else if profile.lastCheckup?.failCount ?? 0 > 0 || profile.lastCheckup?.warnCount ?? 0 > 0 {
            primaryAction = .viewCheckup
        } else {
            primaryAction = .openSMB
        }
        return DeviceDashboardSummary(
            profile: profile,
            passwordState: passwordState,
            primaryAction: primaryAction,
            hostWarning: HostCompatibilityPolicy.warning()
        )
    }

    func password(for profile: DeviceProfile) -> String? {
        do {
            return try passwordStore.password(for: profile.keychainAccount)
        } catch PasswordStoreError.missing {
            deviceRegistry.updatePasswordState(.missing, for: profile.id)
            return nil
        } catch {
            deviceRegistry.updatePasswordState(.keychainUnavailable, for: profile.id)
            return nil
        }
    }

    func savePassword(_ password: String, for profile: DeviceProfile) throws {
        try passwordStore.save(password, for: profile.keychainAccount)
        deviceRegistry.updatePasswordState(.available, for: profile.id)
    }

    func forget(_ profile: DeviceProfile) throws {
        try passwordStore.deletePassword(for: profile.keychainAccount)
        try deviceRegistry.delete(profile)
        if selectedDeviceID == profile.id {
            selectedDeviceID = deviceRegistry.profiles.first?.id
            showingAddDevice = false
        }
    }

    func refreshPasswordStates() {
        for profile in deviceRegistry.profiles {
            deviceRegistry.updatePasswordState(passwordStore.state(for: profile.keychainAccount), for: profile.id)
        }
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
