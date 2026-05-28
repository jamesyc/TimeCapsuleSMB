import Combine
import Foundation

@MainActor
final class DeviceDashboardSession: ObservableObject, Identifiable {
    let id: DeviceProfile.ID
    @Published var selectedTab: DeviceDashboardTab = .overview

    let appStore: AppStore
    var deployStore: DeployWorkflowStore
    var doctorStore: DoctorStore
    var maintenanceStore: MaintenanceStore
    var flashStore: FlashWorkflowStore
    let profileEditorStore: DeviceProfileEditorStore

    private let urlOpener: URLOpening
    private let smbAccountResolver: SMBAccountResolving
    private let lane: OperationLane
    private var activeCheckupOperation: ActiveOperation?
    private var activeDeployOperation: ActiveOperation?
    private var activeUninstallOperation: ActiveOperation?
    private var cancellables: Set<AnyCancellable> = []

    var events: [BackendEvent] {
        lane.backend.events
    }

    init(
        profile: DeviceProfile,
        appStore: AppStore,
        urlOpener: URLOpening = WorkspaceURLOpener(),
        smbAccountResolver: SMBAccountResolving = KeychainSMBAccountResolver()
    ) {
        self.id = profile.id
        self.appStore = appStore
        self.urlOpener = urlOpener
        self.smbAccountResolver = smbAccountResolver
        let laneKey = OperationLaneKey.device(profile.id)
        let lane = appStore.operationCoordinator.lane(for: laneKey)
        self.lane = lane
        self.deployStore = DeployWorkflowStore(coordinator: appStore.operationCoordinator, laneKey: laneKey)
        self.doctorStore = DoctorStore(coordinator: appStore.operationCoordinator, laneKey: laneKey)
        self.maintenanceStore = MaintenanceStore(coordinator: appStore.operationCoordinator, laneKey: laneKey)
        self.flashStore = FlashWorkflowStore(coordinator: appStore.operationCoordinator, laneKey: laneKey)
        self.profileEditorStore = DeviceProfileEditorStore(profile: profile, appStore: appStore)
        applyProfileSettings(profile.settings)
        forwardChildChanges()
        forwardLaneEvents()
        observeSnapshots()
        observeProfileEditor()
    }

    func summary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        appStore.dashboardSummary(for: profile)
    }

    func performPrimaryAction(_ action: DashboardPrimaryAction, profile: DeviceProfile) {
        switch action {
        case .replacePassword:
            showPasswordReplacement()
        case .runCheckup:
            runCheckup(profile: profile)
        case .installSMB:
            runInstallPlan(profile: profile)
        case .viewCheckup:
            selectedTab = .checkup
        case .openSMB:
            openSMBAddress(for: profile)
        }
    }

    func performSecondaryAction(_ action: DashboardSecondaryAction, profile: DeviceProfile) {
        switch action {
        case .refreshStatus:
            refreshReachability(profile: profile)
        case .runCheckup:
            runCheckup(profile: profile)
        case .installUpdate:
            runInstallPlan(profile: profile)
        case .openFinder:
            openSMBAddress(for: profile)
        case .replacePassword:
            showPasswordReplacement()
        case .viewCheckup:
            selectedTab = .checkup
        case .startSMB:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .activate
        case .settings:
            selectedTab = .settings
        }
    }

    func performInstallAction(_ action: InstallUserAction, profile: DeviceProfile, showDiagnostics: () -> Void) {
        switch action {
        case .createPlan, .regeneratePlan, .reinstall:
            runInstallPlan(profile: profile)
        case .installUpdate:
            runInstall(profile: profile)
        case .openFinder:
            openSMBAddress(for: profile)
        case .runCheckup:
            runCheckup(profile: profile)
        case .viewCheckup:
            selectedTab = .checkup
        case .viewDiagnostics:
            showDiagnostics()
        }
    }

    func performCheckupAction(_ action: CheckupUserAction, profile: DeviceProfile, showDiagnostics: () -> Void) {
        switch action {
        case .runCheckup:
            runCheckup(profile: profile)
        case .installUpdate:
            runInstallPlan(profile: profile)
        case .startSMB:
            selectedTab = .maintenance
            maintenanceStore.selectedWorkflow = .activate
        case .replacePassword:
            showPasswordReplacement()
        case .openFinder:
            openSMBAddress(for: profile)
        case .viewDiagnostics:
            showDiagnostics()
        }
    }

    func performMaintenanceAction(_ action: MaintenanceUserAction, profile: DeviceProfile, showDiagnostics: () -> Void) {
        switch action {
        case .planActivation:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.planActivation(password: password, profile: profile)
            }
        case .runActivation:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runActivation(password: password, profile: profile)
                invalidateCheckupIfStarted(start)
            }
        case .planUninstall:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.planUninstall(password: password, profile: profile)
            }
        case .runUninstall:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runUninstall(password: password, profile: profile)
                if case .started(let operation) = start {
                    activeUninstallOperation = operation
                }
                invalidateCheckupIfStarted(start)
            }
        case .findVolumes:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.refreshFsckTargets(password: password, profile: profile)
            }
        case .planFsck:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.planFsck(password: password, profile: profile)
            }
        case .runFsck:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runFsck(password: password, profile: profile)
                invalidateCheckupIfStarted(start)
            }
        case .scanMetadata:
            selectedTab = .maintenance
            maintenanceStore.scanRepairXattrs()
        case .repairMetadata:
            selectedTab = .maintenance
            maintenanceStore.runRepairXattrs()
        case .viewDiagnostics:
            showDiagnostics()
        }
    }

    func performFlashAction(_ action: FlashUserAction, profile: DeviceProfile) {
        switch action {
        case .backupAndInspect:
            if let password = maintenancePassword(for: profile) {
                flashStore.backupAndInspect(password: password, profile: profile)
            }
        case .planPatch:
            flashStore.planFlash(mode: .patch, profile: profile)
        case .planRestore:
            flashStore.planFlash(mode: .restore, profile: profile)
        case .checkApple:
            flashStore.planFlash(mode: .checkApple, profile: profile)
        case .downloadApple:
            flashStore.planFlash(mode: .downloadOnly, profile: profile)
        case .writePatch:
            if let password = maintenancePassword(for: profile) {
                let start = flashStore.write(mode: .patch, password: password, profile: profile)
                invalidateCheckupIfStarted(start)
            }
        case .writeRestore:
            if let password = maintenancePassword(for: profile) {
                let start = flashStore.write(mode: .restore, password: password, profile: profile)
                invalidateCheckupIfStarted(start)
            }
        }
    }

    func viewCheckupAfterFlashNotice() {
        flashStore.dismissManualPowerCycleNotice()
        selectedTab = .checkup
    }

    func runCheckup(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        profileEditorStore.clearPasswordAttention()
        selectedTab = .checkup
        if case .started(let operation) = doctorStore.runDoctor(password: password, profile: profile) {
            activeCheckupOperation = operation
        }
    }

    func runInstallPlan(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        profileEditorStore.clearPasswordAttention()
        selectedTab = .install
        _ = deployStore.runPlan(password: password, profile: profile)
    }

    func runInstall(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        profileEditorStore.clearPasswordAttention()
        selectedTab = .install
        if case .started(let operation) = deployStore.runDeploy(password: password, profile: profile) {
            activeDeployOperation = operation
            invalidateCheckup(for: operation)
        }
    }

    func refreshReachability(profile: DeviceProfile) {
        appStore.reachabilityStore.refresh(profile: profile, password: appStore.password(for: profile))
    }

    func maintenancePassword(for profile: DeviceProfile) -> String? {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return nil
        }
        profileEditorStore.clearPasswordAttention()
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
            showPasswordReplacement()
            return true
        case .openFinder:
            openSMBAddress(for: profile)
            return true
        case .diagnostics, .copyDiagnostics, .generic:
            return false
        }
    }

    private func showPasswordReplacement() {
        promptForPasswordReplacement(error: nil)
    }

    private func promptForPasswordReplacement(error: String?) {
        profileEditorStore.requestPasswordReplacement(error: error)
        selectedTab = .settings
    }

    func applyProfileSettings(_ settings: DeviceProfileSettings) {
        deployStore.nbnsEnabled = settings.nbnsEnabled
        deployStore.internalShareUseDiskRoot = settings.internalShareUseDiskRoot
        deployStore.anyProtocol = settings.anyProtocol
        deployStore.debugLogging = settings.debugLogging
        deployStore.ataIdleSeconds = String(settings.ataIdleSeconds)
        deployStore.ataStandby = settings.ataStandby.map { String($0) } ?? ""
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
                    await self.appStore.profilePersistence.markCredentialInvalid(profileID: profileID)
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
                    await self.appStore.profilePersistence.markCredentialInvalid(profileID: profileID)
                }
            }
            .store(in: &cancellables)
        maintenanceStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.profilePersistence.markCredentialInvalid(profileID: profileID)
                }
            }
            .store(in: &cancellables)
        flashStore.$passwordInvalidProfileID
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.profilePersistence.markCredentialInvalid(profileID: profileID)
                }
            }
            .store(in: &cancellables)
        maintenanceStore.$uninstallState
            .sink { [weak self] state in
                Task { @MainActor in
                    self?.updateUninstallSnapshot(state: state)
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
        guard let url = SMBAddressPolicy.url(for: profile, account: smbAccountResolver.account(for: profile)) else {
            return
        }
        urlOpener.open(url)
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
        flashStore.objectWillChange
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

    private func forwardLaneEvents() {
        lane.backend.$events
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
        let observedAt = Date()
        Task {
            await appStore.deviceRegistry.updateCheckup(DeviceCheckupSnapshot(
                checkedAt: observedAt,
                state: state,
                passCount: summary.passCount,
                warnCount: summary.warnCount,
                failCount: summary.failCount,
                summary: ""
            ), for: profileID)
            if let snapshot = verifiedDeploySnapshotFromPassedCheckup(profileID: profileID, state: state, observedAt: observedAt) {
                await appStore.deviceRegistry.updateDeploy(snapshot, for: profileID)
            }
        }
    }

    private func verifiedDeploySnapshotFromPassedCheckup(
        profileID: DeviceProfile.ID,
        state: DoctorWorkflowState,
        observedAt: Date
    ) -> DeviceDeploySnapshot? {
        guard state == .passed,
              !doctorStore.skipSSH,
              let profile = appStore.deviceRegistry.profile(id: profileID) else {
            return nil
        }
        if profile.lastDeploy?.verified == true {
            return nil
        }
        return DeviceDeploySnapshot(
            deployedAt: profile.lastDeploy?.deployedAt ?? observedAt,
            state: .deployed,
            payloadFamily: profile.lastDeploy?.payloadFamily ?? profile.payloadFamily,
            rebootRequested: profile.lastDeploy?.rebootRequested,
            verified: true,
            summary: profile.lastDeploy?.summary ?? ""
        )
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
                summary: result.message ?? ""
            ), for: profile.id)
        }
    }

    private func invalidateCheckupIfStarted(_ start: OperationStartResult) {
        guard case .started(let operation) = start else {
            return
        }
        invalidateCheckup(for: operation)
    }

    private func invalidateCheckup(for operation: ActiveOperation) {
        guard let profileID = operation.profileID else {
            return
        }
        doctorStore.invalidateResult()
        Task {
            await appStore.deviceRegistry.clearCheckup(for: profileID)
        }
    }

    private func updateUninstallSnapshot(state: MaintenanceOperationState) {
        guard [.succeeded, .failed].contains(state) else {
            return
        }
        defer {
            activeUninstallOperation = nil
        }
        guard state == .succeeded,
              let profileID = activeUninstallOperation?.profileID else {
            return
        }
        Task {
            await appStore.deviceRegistry.clearDeploy(for: profileID)
        }
    }
}
