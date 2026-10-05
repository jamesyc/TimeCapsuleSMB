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
    private var pendingSSHRefresh: (operation: String, requestID: String, sshRequestID: String?)?
    private var handledSSHFailures: [String: String] = [:]

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
        case .reinstall, .installUpdate:
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
        case .runActivation:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runActivation(password: password, profile: profile)
                stateSynchronizer.invalidateCheckupIfStarted(start)
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
        case .openSystemSettings:
            if let url = LocalNetworkRecovery.settingsURL {
                urlOpener.open(url)
                return true
            }
            return false
        case .openSSHAccess:
            openSSHAccess(profile: profile)
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

    private func latestProfile(for profile: DeviceProfile) -> DeviceProfile {
        appStore.deviceRegistry.profile(id: profile.id) ?? profile
    }

    func applyProfileSettings(_ settings: DeviceProfileSettings) {
        deployStore.rsyncEnabled = settings.rsyncEnabled
        deployStore.internalShareUseDiskRoot = settings.internalShareUseDiskRoot
        deployStore.smbBrowseCompatibility = settings.smbBrowseCompatibility
        deployStore.mdnsAdvertiseAFP = settings.mdnsAdvertiseAFP
        deployStore.anyProtocol = settings.anyProtocol
        deployStore.requireSMBEncryption = settings.requireSMBEncryption
        deployStore.forceDisableSMBSigningAndEncryption = settings.forceDisableSMBSigningAndEncryption
        deployStore.fruitMetadataNetatalk = settings.fruitMetadataNetatalk
        deployStore.vfsAIOForkEnabled = settings.vfsAIOForkEnabled
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
        appStore.operationCoordinator.$lanesRevision
            .sink { [weak self] _ in
                // Published changes arrive before the stored value changes. Recheck
                // actual device ownership on the actor after that mutation finishes.
                Task { @MainActor [weak self] in self?.runPendingSSHRefresh() }
            }
            .store(in: &cancellables)
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
        for publisher in [
            maintenanceStore.activationStore.$error,
            maintenanceStore.uninstallStore.$error,
            maintenanceStore.fsckStore.$error,
            maintenanceStore.repairXattrsStore.$error,
            maintenanceStore.sshAccessStore.$error
        ] {
            publisher
                .sink { [weak self] error in
                    self?.refreshSSHAccessAfterRemoteFailure(error)
                }
                .store(in: &cancellables)
        }
    }

    private func observeSSHAccessMaintenanceResults() {
        maintenanceStore.sshAccessStore.$payload
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
        guard let error,
              !["operation_rejected", "cancelled", "confirmation_cancelled"].contains(error.code),
              let event = latestTerminalEvent(for: error.operation),
              let requestID = event.requestId,
              event.type == "error" || event.ok == false,
              handledSSHFailures[error.operation] != requestID else { return }
        handledSSHFailures[error.operation] = requestID
        pendingSSHRefresh = (error.operation, requestID, latestTerminalEvent(for: "set-ssh")?.requestId)
        Task { @MainActor [weak self] in self?.runPendingSSHRefresh() }
    }

    private func latestTerminalEvent(for operation: String) -> BackendEvent? {
        appStore.operationCoordinator.allLanes
            .filter { $0.key.deviceProfileID == id }
            .flatMap { $0.backend.events }
            .last { $0.operation == operation && ($0.type == "error" || $0.type == "result") }
    }

    private func runPendingSSHRefresh() {
        guard let pending = pendingSSHRefresh else { return }
        guard let profile = appStore.deviceRegistry.profile(id: id) else {
            pendingSSHRefresh = nil
            return
        }
        // A successful retry or a newer SSH check already answers this failure.
        for (operation, previousID) in [(pending.operation, Optional(pending.requestID)), ("set-ssh", pending.sshRequestID)] {
            if let event = latestTerminalEvent(for: operation), event.type == "result", event.ok == true,
               event.requestId != previousID {
                pendingSSHRefresh = nil
                return
            }
        }
        guard !appStore.operationCoordinator.isDeviceBusy(id) else { return }
        pendingSSHRefresh = nil
        appStore.sshAccessStore.refresh(profile: profile)
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
        Publishers.MergeMany(
            deployStore.objectWillChange,
            doctorStore.objectWillChange,
            maintenanceStore.objectWillChange,
            flashStore.objectWillChange,
            profileEditorStore.objectWillChange,
            appStore.deviceDiscovery.objectWillChange
        )
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
