import Combine
import Foundation
#if canImport(AppKit)
import AppKit
#endif

enum DeviceDashboardTab: String, CaseIterable, Equatable, Identifiable {
    case overview
    case install
    case checkup
    case maintenance
    case advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview:
            return L10n.string("dashboard.tab.overview")
        case .install:
            return L10n.string("dashboard.tab.install")
        case .checkup:
            return L10n.string("dashboard.tab.checkup")
        case .maintenance:
            return L10n.string("dashboard.tab.maintenance")
        case .advanced:
            return L10n.string("dashboard.tab.advanced")
        }
    }
}

@MainActor
final class DeviceDashboardSession: ObservableObject, Identifiable {
    let id: DeviceProfile.ID
    @Published var selectedTab: DeviceDashboardTab = .overview
    @Published var replacementPassword = ""
    @Published private(set) var passwordError: String?

    let appStore: AppStore
    var deployStore: DeployWorkflowStore
    var doctorStore: DoctorStore
    var maintenanceStore: MaintenanceStore
    let profileEditorStore: DeviceProfileEditorStore

    private var activeCheckupOperation: ActiveOperation?
    private var activeDeployOperation: ActiveOperation?
    private var cancellables: Set<AnyCancellable> = []

    init(profile: DeviceProfile, appStore: AppStore) {
        self.id = profile.id
        self.appStore = appStore
        self.deployStore = DeployWorkflowStore(coordinator: appStore.operationCoordinator)
        self.doctorStore = DoctorStore(coordinator: appStore.operationCoordinator)
        self.maintenanceStore = MaintenanceStore(coordinator: appStore.operationCoordinator)
        self.profileEditorStore = DeviceProfileEditorStore(profile: profile, appStore: appStore)
        applyProfileSettings(profile.settings)
        forwardChildChanges()
        observeSnapshots()
        observeProfileEditor()
    }

    func summary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        appStore.dashboardSummary(for: profile)
    }

    func runCheckup(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            passwordError = L10n.string("password.error.required")
            return
        }
        passwordError = nil
        selectedTab = .checkup
        if case .started(let operation) = doctorStore.runDoctor(password: password, profile: profile) {
            activeCheckupOperation = operation
        }
    }

    func runInstallPlan(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            passwordError = L10n.string("password.error.required")
            return
        }
        passwordError = nil
        selectedTab = .install
        _ = deployStore.runPlan(password: password, profile: profile)
    }

    func runInstall(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            passwordError = L10n.string("password.error.required")
            return
        }
        passwordError = nil
        selectedTab = .install
        if case .started(let operation) = deployStore.runDeploy(password: password, profile: profile) {
            activeDeployOperation = operation
        }
    }

    func maintenancePassword(for profile: DeviceProfile) -> String? {
        guard let password = appStore.password(for: profile) else {
            passwordError = L10n.string("password.error.required")
            return nil
        }
        passwordError = nil
        selectedTab = .maintenance
        return password
    }

    @discardableResult
    func handleRecoveryAction(_ action: RecoveryAction, error: BackendErrorViewModel, profile: DeviceProfile) -> Bool {
        switch action.kind {
        case .retry:
            return retry(error: error, profile: profile)
        case .runCheckup:
            runCheckup(profile: profile)
            return true
        case .installSMB:
            runInstallPlan(profile: profile)
            return true
        case .startSMB:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .activate
            return true
        case .uninstall:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .uninstall
            return true
        case .diskRepair:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .fsck
            return true
        case .metadataRepair:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .repairXattrs
            return true
        case .replacePassword:
            selectedTab = .overview
            return true
        case .openFinder:
            openSMBAddress(for: profile)
            return true
        case .diagnostics, .copyDiagnostics, .generic:
            return false
        }
    }

    func applyProfileSettings(_ settings: DeviceProfileSettings) {
        deployStore.nbnsEnabled = settings.nbnsEnabled
        deployStore.debugLogging = settings.debugLogging
        deployStore.mountWait = String(settings.mountWaitSeconds)
        maintenanceStore.mountWait = String(settings.mountWaitSeconds)
    }

    private func observeSnapshots() {
        doctorStore.$state
            .sink { [weak self] state in
                Task { @MainActor in
                    self?.updateCheckupSnapshot(state: state)
                }
            }
            .store(in: &cancellables)
        doctorStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
                }
            }
            .store(in: &cancellables)
        deployStore.$state
            .sink { [weak self] state in
                Task { @MainActor in
                    self?.updateDeploySnapshot(state: state)
                }
            }
            .store(in: &cancellables)
        deployStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
                }
            }
            .store(in: &cancellables)
        maintenanceStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
                }
            }
            .store(in: &cancellables)
    }

    private func observeProfileEditor() {
        profileEditorStore.$savedProfile
            .compactMap { $0 }
            .sink { [weak self] profile in
                self?.applyProfileSettings(profile.settings)
            }
            .store(in: &cancellables)
    }

    private func retry(error: BackendErrorViewModel, profile: DeviceProfile) -> Bool {
        switch error.operation {
        case "doctor":
            runCheckup(profile: profile)
            return true
        case "deploy":
            runInstallPlan(profile: profile)
            return true
        case "activate":
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .activate
            return true
        case "uninstall":
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .uninstall
            return true
        case "fsck":
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .fsck
            return true
        case "repair-xattrs":
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .repairXattrs
            return true
        default:
            return false
        }
    }

    private func openSMBAddress(for profile: DeviceProfile) {
        let host = profile.host
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: #"^.*@"#, with: "", options: .regularExpression)
        guard !host.isEmpty, let url = URL(string: "smb://\(host)") else {
            return
        }
        #if canImport(AppKit)
        NSWorkspace.shared.open(url)
        #endif
    }

    private func forwardChildChanges() {
        deployStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        doctorStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        maintenanceStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
        profileEditorStore.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    private func updateCheckupSnapshot(state: DoctorWorkflowState) {
        guard [.passed, .warning, .failed, .runFailed].contains(state) else {
            return
        }
        defer {
            activeCheckupOperation = nil
        }
        guard [.passed, .warning, .failed].contains(state),
              let profileID = activeCheckupOperation?.profileID,
              let summary = doctorStore.summary else {
            return
        }
        Task {
            await appStore.deviceRegistry.updateCheckup(DeviceCheckupSnapshot(
                checkedAt: Date(),
                state: state,
                passCount: summary.passCount,
                warnCount: summary.warnCount,
                failCount: summary.failCount,
                summary: L10n.format("summary.checkup_counts", summary.passCount, summary.warnCount, summary.failCount)
            ), for: profileID)
        }
    }

    private func updateDeploySnapshot(state: DeployWorkflowState) {
        guard [.deployed, .deployFailed].contains(state) else {
            return
        }
        defer {
            activeDeployOperation = nil
        }
        guard state == .deployed,
              let profileID = activeDeployOperation?.profileID,
              let profile = appStore.deviceRegistry.profile(id: profileID),
              let result = deployStore.result else {
            return
        }
        Task {
            await appStore.deviceRegistry.updateDeploy(DeviceDeploySnapshot(
                deployedAt: Date(),
                state: state,
                payloadFamily: deployStore.plan?.payloadFamily ?? profile.payloadFamily,
                rebootRequested: result.rebootRequested,
                verified: result.verified,
                summary: result.message ?? L10n.string("deploy.result.default_message")
            ), for: profile.id)
        }
    }
}

@MainActor
final class DashboardStore: ObservableObject {
    let appStore: AppStore

    private var sessions: [DeviceProfile.ID: DeviceDashboardSession] = [:]
    private var cancellables: Set<AnyCancellable> = []

    init(appStore: AppStore) {
        self.appStore = appStore
        appStore.deviceRegistry.$profiles
            .sink { [weak self] profiles in
                Task { @MainActor in
                    self?.pruneSessions(profiles: profiles)
                }
            }
            .store(in: &cancellables)
        appStore.operationCoordinator.$activeOperation
            .sink { [weak self] _ in
                Task { @MainActor in
                    guard let self else { return }
                    self.pruneSessions(profiles: self.appStore.deviceRegistry.profiles)
                }
            }
            .store(in: &cancellables)
    }

    func session(for profile: DeviceProfile) -> DeviceDashboardSession {
        if let session = sessions[profile.id] {
            return session
        }
        let session = DeviceDashboardSession(profile: profile, appStore: appStore)
        sessions[profile.id] = session
        objectWillChange.send()
        return session
    }

    func hasSession(for profileID: DeviceProfile.ID) -> Bool {
        sessions[profileID] != nil
    }

    private func pruneSessions(profiles: [DeviceProfile]) {
        let existingIDs = Set(profiles.map(\.id))
        let activeProfileID = appStore.operationCoordinator.activeOperation?.profileID
        sessions = sessions.filter { id, _ in
            existingIDs.contains(id) || id == activeProfileID
        }
    }
}
