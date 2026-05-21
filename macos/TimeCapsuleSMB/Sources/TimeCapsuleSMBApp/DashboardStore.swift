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
            return "Overview"
        case .install:
            return "Install / Update"
        case .checkup:
            return "Checkup"
        case .maintenance:
            return "Maintenance"
        case .advanced:
            return "Advanced"
        }
    }
}

@MainActor
final class DashboardStore: ObservableObject {
    @Published var selectedTab: DeviceDashboardTab = .overview
    @Published private(set) var passwordError: String?

    let appStore: AppStore
    var deployStore: DeployWorkflowStore
    var doctorStore: DoctorStore
    var maintenanceStore: MaintenanceStore

    private var activeCheckupOperation: ActiveOperation?
    private var activeDeployOperation: ActiveOperation?
    private var cancellables: Set<AnyCancellable> = []

    init(appStore: AppStore) {
        self.appStore = appStore
        self.deployStore = DeployWorkflowStore(coordinator: appStore.operationCoordinator)
        self.doctorStore = DoctorStore(coordinator: appStore.operationCoordinator)
        self.maintenanceStore = MaintenanceStore(coordinator: appStore.operationCoordinator)
        forwardChildChanges()
        observeSnapshots()
    }

    func summary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        appStore.dashboardSummary(for: profile)
    }

    func runCheckup(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            passwordError = "Password is required."
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
            passwordError = "Password is required."
            return
        }
        passwordError = nil
        selectedTab = .install
        deployStore.nbnsEnabled = profile.settings.nbnsEnabled
        deployStore.debugLogging = profile.settings.debugLogging
        deployStore.mountWait = String(profile.settings.mountWaitSeconds)
        _ = deployStore.runPlan(password: password, profile: profile)
    }

    func runInstall(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            passwordError = "Password is required."
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
            passwordError = "Password is required."
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
                Task { @MainActor in
                    self?.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
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
                Task { @MainActor in
                    self?.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
                }
            }
            .store(in: &cancellables)
        maintenanceStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor in
                    self?.appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID)
                }
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
        appStore.deviceRegistry.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(),
            state: state,
            passCount: summary.passCount,
            warnCount: summary.warnCount,
            failCount: summary.failCount,
            summary: "PASS \(summary.passCount), WARN \(summary.warnCount), FAIL \(summary.failCount)"
        ), for: profileID)
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
        appStore.deviceRegistry.updateDeploy(DeviceDeploySnapshot(
            deployedAt: Date(),
            state: state,
            payloadFamily: deployStore.plan?.payloadFamily ?? profile.payloadFamily,
            rebootRequested: result.rebootRequested,
            verified: result.verified,
            summary: result.message ?? "Install completed."
        ), for: profile.id)
    }
}
