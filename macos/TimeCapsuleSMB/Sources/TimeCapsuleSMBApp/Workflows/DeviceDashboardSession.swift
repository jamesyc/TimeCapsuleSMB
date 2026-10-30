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
    private var pendingSSHRefresh: (operation: String, requestID: String, sshOutcomeIDs: Set<UUID>)?
    private var handledSSHFailures: [String: String] = [:]
    // By event, not request: a confirmed replay reuses its confirmation's request ID.
    private var checkedAddressOutcomes: Set<UUID> = []
    // A Checkup waiting for the Local Network preflight before it starts.
    private var checkupPreflightTask: Task<Void, Never>?

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
        // Outcomes from before this dashboard opened were answered then.
        checkedAddressOutcomes = Set(addressOutcomeEvents().map(\.id))
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
        case .openBootHook:
            selectedTab = .maintenance
            maintenanceStore.select(.bootHook)
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
        case .runWipeDisk:
            if let password = maintenancePassword(for: profile) {
                let start = maintenanceStore.runWipeDisk(password: password, profile: profile)
                if case .started(let operation) = start {
                    stateSynchronizer.trackWipeDiskStart(operation)
                }
                stateSynchronizer.invalidateCheckupIfStarted(start)
            }
        case .installSMB:
            runInstall(profile: profile)
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
        guard checkupPreflightTask == nil else {
            return
        }
        guard let checker = appStore.localNetworkPreflightChecker else {
            startCheckup(password: password, profile: profile, localNetworkPreflight: nil)
            return
        }
        // Checkup's This Mac section reports whether macOS lets the app reach
        // the local network; asking takes milliseconds once macOS has answered.
        doctorStore.beginPreparing()
        checkupPreflightTask = Task { [weak self] in
            let result = await checker.check()
            guard let self else {
                return
            }
            self.checkupPreflightTask = nil
            self.startCheckup(password: password, profile: profile, localNetworkPreflight: result)
        }
    }

    private func startCheckup(password: String, profile: DeviceProfile, localNetworkPreflight: LocalNetworkPreflightResult?) {
        let start = doctorStore.runDoctor(password: password, profile: profile, localNetworkPreflight: localNetworkPreflight)
        if case .started(let operation) = start {
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

    /// What the dashboard does for a recovery action, or nil where it can do
    /// nothing; an error box shows a button only for an action that does
    /// something. A tab adds Diagnostics.
    func recoveryAction(_ kind: RecoveryActionKind, error: BackendErrorViewModel, profile: DeviceProfile) -> (() -> Void)? {
        switch kind {
        case .retry:
            return retryAction(for: error, profile: profile)
        case .runCheckup:
            return { [weak self] in self?.runCheckup(profile: profile) }
        case .installSMB:
            return { [weak self] in self?.runInstall(profile: profile) }
        case .startSMB:
            return { [weak self] in self?.showMaintenance(.activate) }
        case .uninstall:
            return { [weak self] in self?.showMaintenance(.uninstall) }
        case .diskRepair:
            return { [weak self] in self?.showMaintenance(.fsck) }
        case .wipeDisk:
            return { [weak self] in self?.showMaintenance(.wipeDisk) }
        case .replacePassword:
            return { [weak self] in self?.showPasswordReplacement() }
        case .openSystemSettings:
            guard let url = LocalNetworkRecovery.settingsURL else {
                return nil
            }
            return { [weak self] in self?.urlOpener.open(url) }
        case .openSSHAccess:
            return { [weak self] in self?.openSSHAccess(profile: profile) }
        case .diagnostics, .copyDiagnostics, .generic:
            return nil
        }
    }

    @discardableResult
    func handleRecoveryAction(_ action: RecoveryAction, error: BackendErrorViewModel, profile: DeviceProfile) -> Bool {
        guard let perform = recoveryAction(action.kind, error: error, profile: profile) else {
            return false
        }
        perform()
        return true
    }

    private func showMaintenance(_ workflow: MaintenanceWorkflow) {
        selectedTab = .maintenance
        maintenanceStore.selectedWorkflow = workflow
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
                Task { @MainActor [weak self] in
                    self?.runPendingSSHRefresh()
                    self?.refreshDiscoveryAfterAddressOutcome()
                }
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
            maintenanceStore.wipeDiskStore.$error,
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
        pendingSSHRefresh = (error.operation, requestID, Set(sshCheckOutcomes().map(\.id)))
        Task { @MainActor [weak self] in self?.runPendingSSHRefresh() }
    }

    /// Error codes after which the device may be at another DHCP address.
    static let addressOutcomeCodes: Set<String> = ["device_unreachable", "device_identity_mismatch", "reboot_not_finished"]

    /// The helper found the device at another address (`current_host`), or
    /// could not find it where it was saved: look again, so the stale-address
    /// banner offers the current one. The helper never saves an address itself.
    private func refreshDiscoveryAfterAddressOutcome() {
        var refresh = false
        var refreshSSH = false
        for event in addressOutcomeEvents() {
            guard checkedAddressOutcomes.insert(event.id).inserted else { continue }
            // A result carries the new address in its payload, a failure in its debug fields.
            let movedTo = event.payload?.stringValue(for: "current_host") ?? event.debug?.stringValue(for: "current_host")
            // Checkup reports a failed check in its result, not as an error.
            let checkCodes = Self.checkCodes(in: event)
            if movedTo != nil || Self.addressOutcomeCodes.contains(event.code ?? "") || checkCodes.contains("device_unreachable") {
                refresh = true
            }
            refreshSSH = refreshSSH || checkCodes.contains("ssh_disabled")
        }
        if refresh {
            appStore.deviceDiscovery.refresh()
        }
        if refreshSSH, let profile = appStore.deviceRegistry.profile(id: id) {
            // Shows the SSH banner, whose button turns SSH on.
            appStore.sshAccessStore.refresh(profile: profile)
        }
    }

    private static func checkCodes(in event: BackendEvent) -> Set<String> {
        guard event.operation == "doctor", case .object(let payload)? = event.payload,
              case .array(let results)? = payload["results"] else { return [] }
        return Set(results.compactMap { result in
            guard case .object(let check) = result, case .object(let details)? = check["details"] else { return nil }
            return details["code"].flatMap { if case .string(let code) = $0 { return code } else { return nil } }
        })
    }

    /// Each finished lane's outcome. The revision moves on every event of every
    /// lane, so a running lane, whose outcome is still to come, is not scanned;
    /// a run has one outcome, and the next run starts only after this one ends.
    private func addressOutcomeEvents() -> [BackendEvent] {
        appStore.operationCoordinator.allLanes
            .filter { $0.key.deviceProfileID == id && !$0.backend.isRunning }
            .compactMap { lane in lane.backend.events.last { $0.type == "error" || $0.type == "result" } }
    }

    /// The last outcome of `operation` on this device's own lanes. A
    /// background check's never counts: it reports no failure of its own.
    private func latestTerminalEvent(for operation: String) -> BackendEvent? {
        appStore.operationCoordinator.allLanes
            .filter { $0.key.deviceProfileID == id && !$0.key.isBackground }
            .flatMap { $0.backend.events }
            .last { $0.operation == operation && ($0.type == "error" || $0.type == "result") }
    }

    /// Each lane's last SSH check outcome, the background check's included.
    /// Their order across lanes is unknown, so each is compared with what
    /// that lane held before, never with another lane's.
    private func sshCheckOutcomes() -> [BackendEvent] {
        appStore.operationCoordinator.allLanes
            .filter { $0.key.deviceProfileID == id }
            .compactMap { lane in
                lane.backend.events.last { $0.operation == "set-ssh" && ($0.type == "error" || $0.type == "result") }
            }
    }

    private func runPendingSSHRefresh() {
        guard let pending = pendingSSHRefresh else { return }
        guard let profile = appStore.deviceRegistry.profile(id: id) else {
            pendingSSHRefresh = nil
            return
        }
        // A successful retry or a newer SSH check already answers this failure.
        let succeeded = { (event: BackendEvent) in event.type == "result" && event.ok == true }
        if let retry = latestTerminalEvent(for: pending.operation), succeeded(retry), retry.requestId != pending.requestID {
            pendingSSHRefresh = nil
            return
        }
        // By event: a confirmed replay reuses its request ID, and an event may have none.
        if sshCheckOutcomes().contains(where: { succeeded($0) && !pending.sshOutcomeIDs.contains($0.id) }) {
            pendingSSHRefresh = nil
            return
        }
        // A background check still running answers it once it finishes.
        guard !appStore.operationCoordinator.isDeviceBusy(id),
              !appStore.operationCoordinator.lane(for: .deviceWorkflow(id, .sshStatus)).isBusy else { return }
        pendingSSHRefresh = nil
        appStore.sshAccessStore.refresh(profile: profile)
    }

    /// What reruns the failed operation, or nil for one the dashboard cannot
    /// run again from its error box. Activate, Uninstall and Disk Repair ask
    /// for confirmation first, and their error box sits beside the button that
    /// does that; a profile save and the flash steps have buttons of their own.
    func retryAction(for error: BackendErrorViewModel, profile: DeviceProfile) -> (() -> Void)? {
        switch error.operation {
        case "doctor":
            return { [weak self] in self?.runCheckup(profile: profile) }
        case "deploy":
            return { [weak self] in self?.runInstall(profile: profile) }
        default:
            return nil
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
