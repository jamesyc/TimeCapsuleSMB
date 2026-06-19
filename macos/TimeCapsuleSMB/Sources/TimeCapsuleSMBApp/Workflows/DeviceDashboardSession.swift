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
    private let stateSynchronizer: DeviceDashboardStateSynchronizer
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
        let configureLaneKey = OperationLaneKey.deviceWorkflow(profile.id, .configure)
        self.lane = appStore.operationCoordinator.lane(for: configureLaneKey)
        self.deployStore = DeployWorkflowStore(
            coordinator: appStore.operationCoordinator,
            laneKey: .deviceWorkflow(profile.id, .deploy)
        )
        self.doctorStore = DoctorStore(
            coordinator: appStore.operationCoordinator,
            laneKey: .deviceWorkflow(profile.id, .doctor)
        )
        self.maintenanceStore = MaintenanceStore(
            coordinator: appStore.operationCoordinator,
            laneKey: .deviceWorkflow(profile.id, .maintenance)
        )
        self.flashStore = FlashWorkflowStore(
            coordinator: appStore.operationCoordinator,
            laneKey: .deviceWorkflow(profile.id, .flash)
        )
        self.profileEditorStore = DeviceProfileEditorStore(profile: profile, appStore: appStore)
        self.stateSynchronizer = DeviceDashboardStateSynchronizer(
            appStore: appStore,
            doctorStore: doctorStore,
            deployStore: deployStore,
            maintenanceStore: maintenanceStore,
            flashStore: flashStore
        )
        applyProfileSettings(profile.settings)
        forwardChildChanges()
        forwardLaneEvents()
        observeProfileEditor()
        observeRemoteWorkflowFailures()
        observeSSHAccessMaintenanceResults()
    }

    func summary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        appStore.dashboardSummary(for: profile)
    }

    func staleEndpointNotice(for profile: DeviceProfile) -> StaleEndpointNotice? {
        appStore.deviceDiscovery.staleEndpointNotice(for: latestProfile(for: profile))
    }

    func sshAccessNotice(for profile: DeviceProfile) -> SSHAccessNotice? {
        let currentProfile = latestProfile(for: profile)
        return appStore.sshAccessStore.notice(
            for: currentProfile,
            staleEndpointNotice: staleEndpointNotice(for: currentProfile)
        )
    }

    func refreshSSHAccessStatus(profile: DeviceProfile) {
        appStore.sshAccessStore.refresh(profile: latestProfile(for: profile))
    }

    func openSSHAccess(profile: DeviceProfile) {
        selectedTab = .maintenance
        maintenanceStore.selectedWorkflow = .sshAccess
        refreshSSHAccessStatus(profile: profile)
    }

    func enableSSHAccess(profile: DeviceProfile) {
        selectedTab = .maintenance
        maintenanceStore.selectedWorkflow = .sshAccess
        if let password = maintenancePassword(for: profile) {
            maintenanceStore.enableSSHAccess(password: password, profile: profile)
        }
    }

    func updateConfiguredAddressFromDiscovery(profile: DeviceProfile) {
        let currentProfile = latestProfile(for: profile)
        guard let notice = staleEndpointNotice(for: currentProfile) else {
            return
        }
        profileEditorStore.draft.host = notice.currentHost
        selectedTab = .settings
        guard appStore.password(for: currentProfile) != nil else {
            profileEditorStore.requestPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        Task { @MainActor in
            await profileEditorStore.save(profile: currentProfile)
        }
    }

    func performPrimaryAction(_ action: DashboardPrimaryAction, profile: DeviceProfile) {
        switch action {
        case .replacePassword:
            showPasswordReplacement()
        case .runCheckup:
            runCheckup(profile: profile)
        case .installSMB:
            runInstall(profile: profile)
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
            runInstall(profile: profile)
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
            runInstall(profile: profile)
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
            runInstall(profile: profile)
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
                stateSynchronizer.invalidateCheckupIfStarted(start)
            }
        case .planUninstall:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.planUninstall(password: password, profile: profile)
            }
        case .runUninstall:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runUninstall(password: password, profile: profile)
                if case .started(let operation) = start {
                    stateSynchronizer.trackUninstallStart(operation)
                }
                stateSynchronizer.invalidateCheckupIfStarted(start)
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
                stateSynchronizer.invalidateCheckupIfStarted(start)
            }
        case .scanMetadata:
            selectedTab = .maintenance
            maintenanceStore.scanRepairXattrs()
        case .repairMetadata:
            selectedTab = .maintenance
            maintenanceStore.runRepairXattrs()
        case .checkSSHAccess:
            maintenanceStore.checkSSHAccess(profile: profile)
        case .enableSSHAccess:
            if let password = maintenancePassword(for: profile) {
                maintenanceStore.enableSSHAccess(password: password, profile: profile)
            }
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
                stateSynchronizer.invalidateCheckupIfStarted(start)
            }
        case .writeRestore:
            if let password = maintenancePassword(for: profile) {
                let start = flashStore.write(mode: .restore, password: password, profile: profile)
                stateSynchronizer.invalidateCheckupIfStarted(start)
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
            stateSynchronizer.trackCheckupStart(operation)
        }
    }

    func runInstallPlan(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        profileEditorStore.clearPasswordAttention()
        selectedTab = .install
        deployStore.runPlan(password: password, profile: profile)
    }

    func runInstall(profile: DeviceProfile) {
        guard let password = appStore.password(for: profile) else {
            promptForPasswordReplacement(error: L10n.string("password.error.required"))
            return
        }
        profileEditorStore.clearPasswordAttention()
        selectedTab = .install
        if case .started(let operation) = deployStore.runDeploy(password: password, profile: profile) {
            stateSynchronizer.trackDeployStart(operation, profile: profile)
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
            runInstall(profile: profile)
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
        case .openSystemSettings:
            if let url = LocalNetworkRecovery.settingsURL {
                urlOpener.open(url)
                return true
            }
            return false
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

    private func latestProfile(for profile: DeviceProfile) -> DeviceProfile {
        appStore.deviceRegistry.profile(id: profile.id) ?? profile
    }

    func applyProfileSettings(_ settings: DeviceProfileSettings) {
        deployStore.nbnsEnabled = settings.nbnsEnabled
        deployStore.internalShareUseDiskRoot = settings.internalShareUseDiskRoot
        deployStore.smbBindLanOnly = settings.smbBindLanOnly
        deployStore.smbBrowseCompatibility = settings.smbBrowseCompatibility
        deployStore.mdnsAdvertiseAFP = settings.mdnsAdvertiseAFP
        deployStore.anyProtocol = settings.anyProtocol
        deployStore.fruitMetadataNetatalk = settings.fruitMetadataNetatalk
        deployStore.debugLogging = settings.debugLogging
        deployStore.ataIdleSeconds = String(settings.ataIdleSeconds)
        deployStore.ataStandby = settings.ataStandby.map { String($0) } ?? ""
        deployStore.mountWait = String(settings.mountWaitSeconds)
        maintenanceStore.mountWait = String(settings.mountWaitSeconds)
    }

    private func observeProfileEditor() {
        profileEditorStore.$savedProfile
            .compactMap { $0 }
            .sink { [weak self] profile in
                self?.applyProfileSettings(profile.settings)
            }
            .store(in: &cancellables)
    }

    private func observeRemoteWorkflowFailures() {
        deployStore.$error
            .sink { [weak self] error in
                self?.refreshSSHAccessAfterRemoteFailure(error)
            }
            .store(in: &cancellables)
        doctorStore.$error
            .sink { [weak self] error in
                self?.refreshSSHAccessAfterRemoteFailure(error)
            }
            .store(in: &cancellables)
        maintenanceStore.$error
            .sink { [weak self] error in
                self?.refreshSSHAccessAfterRemoteFailure(error)
            }
            .store(in: &cancellables)
    }

    private func observeSSHAccessMaintenanceResults() {
        maintenanceStore.$sshAccessPayload
            .compactMap { $0 }
            .sink { [weak self] payload in
                guard let self,
                      let profile = self.appStore.deviceRegistry.profile(id: self.id) else {
                    return
                }
                self.appStore.sshAccessStore.apply(payload: payload, profile: profile)
            }
            .store(in: &cancellables)
    }

    private func refreshSSHAccessAfterRemoteFailure(_ error: BackendErrorViewModel?) {
        guard error != nil,
              let profile = appStore.deviceRegistry.profile(id: id) else {
            return
        }
        Task { @MainActor in
            await Task.yield()
            appStore.sshAccessStore.refresh(profile: profile)
        }
    }

    private func retry(error: BackendErrorViewModel, profile: DeviceProfile) -> Bool {
        switch error.operation {
        case "doctor":
            runCheckup(profile: profile)
            return true
        case "deploy":
            runInstall(profile: profile)
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
        appStore.deviceDiscovery.objectWillChange
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
}
