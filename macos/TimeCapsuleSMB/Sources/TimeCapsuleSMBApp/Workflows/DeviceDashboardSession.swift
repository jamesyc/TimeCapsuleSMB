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
            activeDeployOperation = operation
            persistStartedDeployState(operation: operation, profile: profile)
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
                    self?.updateDeployState(state: state)
                }
            }
            .store(in: &cancellables)
        deployStore.$currentStage
            .sink { [weak self] _ in
                Task { @MainActor in
                    self?.updateCurrentDeployStage()
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
        let runtimeState = runtimeStateFromCheckup(profileID: profileID, state: state, summary: summary)
        Task {
            await appStore.deviceRegistry.updateCheckup(
                DeviceCheckupSnapshot(
                    checkedAt: observedAt,
                    state: state,
                    passCount: summary.passCount,
                    warnCount: summary.warnCount,
                    failCount: summary.failCount,
                    summary: ""
                ),
                runtimeState: runtimeState,
                for: profileID
            )
        }
    }

    private func runtimeStateFromCheckup(
        profileID: DeviceProfile.ID,
        state: DoctorWorkflowState,
        summary: DoctorSummary
    ) -> DeviceRuntimeStateSnapshot? {
        guard !doctorStore.skipSSH,
              let profile = appStore.deviceRegistry.profile(id: profileID) else {
            return nil
        }
        if profile.runtimeState?.state == .installing {
            return nil
        }
        let countSummary = L10n.format("summary.checkup_counts", summary.passCount, summary.warnCount, summary.failCount)
        switch state {
        case .passed:
            return DeviceRuntimeStateSnapshot(
                state: .installedVerified,
                source: .doctor,
                stage: nil,
                payloadFamily: profile.runtimeState?.payloadFamily ?? profile.payloadFamily,
                verified: true,
                summary: "",
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        case .warning:
            let currentRuntimeState = profile.runtimeState?.state
            let runtimeAlreadyInstalled = currentRuntimeState?.isInstalled == true
            let nextState: DeviceRuntimeState = profile.traits.needsActivationAfterReboot && runtimeAlreadyInstalled
                ? .activationNeeded
                : .installedUnverified
            return DeviceRuntimeStateSnapshot(
                state: nextState,
                source: .doctor,
                stage: nil,
                payloadFamily: profile.runtimeState?.payloadFamily ?? profile.payloadFamily,
                verified: false,
                summary: countSummary,
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        case .failed:
            return DeviceRuntimeStateSnapshot(
                state: .unhealthy,
                source: .doctor,
                stage: nil,
                payloadFamily: profile.runtimeState?.payloadFamily ?? profile.payloadFamily,
                verified: false,
                summary: countSummary,
                errorCode: "doctor_failed",
                errorMessage: nil,
                recovery: nil
            )
        case .idle, .running, .runFailed:
            return nil
        }
    }

    private func persistStartedDeployState(operation: ActiveOperation, profile: DeviceProfile) {
        let startedAt = Date()
        let payloadFamily = deployStore.plan?.payloadFamily ?? profile.payloadFamily
        let stage = deployStore.currentStage?.stage
        Task {
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: DeviceDeployStateSnapshot(
                    operationID: operation.id.uuidString,
                    startedAt: startedAt,
                    updatedAt: startedAt,
                    finishedAt: nil,
                    status: .deploying,
                    stage: stage,
                    payloadFamily: payloadFamily,
                    rebootRequested: nil,
                    verified: nil,
                    summary: "",
                    errorCode: nil,
                    errorMessage: nil,
                    recovery: nil
                ),
                runtimeState: DeviceRuntimeStateSnapshot(
                    state: .installing,
                    source: .deploy,
                    stage: stage,
                    payloadFamily: payloadFamily,
                    verified: nil,
                    summary: "",
                    errorCode: nil,
                    errorMessage: nil,
                    recovery: nil
                ),
                for: profile.id
            )
        }
    }

    private func updateCurrentDeployStage() {
        guard [.deploying, .awaitingConfirmation].contains(deployStore.state),
              let operation = activeDeployOperation,
              let profileID = operation.profileID else {
            return
        }
        let observedAt = Date()
        Task {
            guard let profile = appStore.deviceRegistry.profile(id: profileID),
                  let current = profile.lastDeployState,
                  current.operationID == operation.id.uuidString,
                  current.status.isInProgress else {
                return
            }
            let stage = deployStore.currentStage?.stage ?? current.stage
            let runtimeState = profile.runtimeState
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: DeviceDeployStateSnapshot(
                    operationID: current.operationID,
                    startedAt: current.startedAt,
                    updatedAt: observedAt,
                    finishedAt: nil,
                    status: current.status,
                    stage: stage,
                    payloadFamily: current.payloadFamily,
                    rebootRequested: current.rebootRequested,
                    verified: current.verified,
                    summary: current.summary,
                    errorCode: current.errorCode,
                    errorMessage: current.errorMessage,
                    recovery: current.recovery
                ),
                runtimeState: DeviceRuntimeStateSnapshot(
                    state: .installing,
                    source: .deploy,
                    stage: stage,
                    payloadFamily: runtimeState?.payloadFamily ?? current.payloadFamily,
                    verified: runtimeState?.verified,
                    summary: runtimeState?.summary ?? "",
                    errorCode: runtimeState?.errorCode,
                    errorMessage: runtimeState?.errorMessage,
                    recovery: runtimeState?.recovery
                ),
                for: profileID
            )
        }
    }

    private func updateDeployState(state: DeployWorkflowState) {
        guard let operation = activeDeployOperation,
              let profileID = operation.profileID else {
            return
        }
        if state == .awaitingConfirmation {
            persistAwaitingConfirmationDeployState(profileID: profileID)
            return
        }
        guard [.deployed, .deployFailed].contains(state) else {
            return
        }
        defer {
            activeDeployOperation = nil
        }
        if state == .deployFailed {
            Task {
                let failedAt = Date()
                let profile = appStore.deviceRegistry.profile(id: profileID)
                let stage = deployStore.currentStage?.stage
                let payloadFamily = profile?.lastDeployState?.payloadFamily ?? deployStore.plan?.payloadFamily ?? profile?.payloadFamily
                let errorCode = deployStore.error?.code
                let errorMessage = deployStore.error?.message ?? L10n.string("install.state.deploy_failed")
                let recovery = deployStore.error?.recovery.map(DeviceRecoverySnapshot.init)
                await appStore.deviceRegistry.updateInstallOperationState(
                    deployState: DeviceDeployStateSnapshot(
                        operationID: profile?.lastDeployState?.operationID ?? operation.id.uuidString,
                        startedAt: profile?.lastDeployState?.startedAt ?? failedAt,
                        updatedAt: failedAt,
                        finishedAt: failedAt,
                        status: .failed,
                        stage: stage,
                        payloadFamily: payloadFamily,
                        rebootRequested: nil,
                        verified: nil,
                        summary: "",
                        errorCode: errorCode,
                        errorMessage: errorMessage,
                        recovery: recovery
                    ),
                    runtimeState: DeviceRuntimeStateSnapshot(
                        state: .installFailed,
                        source: .deploy,
                        stage: stage,
                        payloadFamily: payloadFamily,
                        verified: false,
                        summary: "",
                        errorCode: errorCode,
                        errorMessage: errorMessage,
                        recovery: recovery
                    ),
                    for: profileID
                )
            }
            return
        }
        guard state == .deployed,
              let profile = appStore.deviceRegistry.profile(id: profileID),
              let result = deployStore.result else {
            return
        }
        Task {
            let finishedAt = Date()
            let stage = deployStore.currentStage?.stage ?? profile.lastDeployState?.stage
            let payloadFamily = deployStore.plan?.payloadFamily ?? profile.payloadFamily
            let runtimeState: DeviceRuntimeState = result.verified == true ? .installedVerified : .installedUnverified
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: DeviceDeployStateSnapshot(
                    operationID: profile.lastDeployState?.operationID ?? operation.id.uuidString,
                    startedAt: profile.lastDeployState?.startedAt ?? finishedAt,
                    updatedAt: finishedAt,
                    finishedAt: finishedAt,
                    status: .succeeded,
                    stage: stage,
                    payloadFamily: payloadFamily,
                    rebootRequested: result.rebootRequested,
                    verified: result.verified,
                    summary: result.message ?? "",
                    errorCode: nil,
                    errorMessage: nil,
                    recovery: nil
                ),
                runtimeState: DeviceRuntimeStateSnapshot(
                    state: runtimeState,
                    source: .deploy,
                    stage: stage,
                    payloadFamily: payloadFamily,
                    verified: result.verified,
                    summary: result.message ?? "",
                    errorCode: nil,
                    errorMessage: nil,
                    recovery: nil
                ),
                for: profile.id
            )
        }
    }

    private func persistAwaitingConfirmationDeployState(profileID: DeviceProfile.ID) {
        Task {
            guard let profile = appStore.deviceRegistry.profile(id: profileID),
                  let current = profile.lastDeployState,
                  current.status.isInProgress else {
                return
            }
            let observedAt = Date()
            let stage = deployStore.currentStage?.stage ?? current.stage
            let runtimeState = profile.runtimeState
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: DeviceDeployStateSnapshot(
                    operationID: current.operationID,
                    startedAt: current.startedAt,
                    updatedAt: observedAt,
                    finishedAt: nil,
                    status: .awaitingConfirmation,
                    stage: stage,
                    payloadFamily: current.payloadFamily,
                    rebootRequested: current.rebootRequested,
                    verified: current.verified,
                    summary: current.summary,
                    errorCode: current.errorCode,
                    errorMessage: current.errorMessage,
                    recovery: current.recovery
                ),
                runtimeState: DeviceRuntimeStateSnapshot(
                    state: .installing,
                    source: .deploy,
                    stage: stage,
                    payloadFamily: runtimeState?.payloadFamily ?? current.payloadFamily,
                    verified: runtimeState?.verified,
                    summary: runtimeState?.summary ?? "",
                    errorCode: runtimeState?.errorCode,
                    errorMessage: runtimeState?.errorMessage,
                    recovery: runtimeState?.recovery
                ),
                for: profileID
            )
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
            await appStore.deviceRegistry.clearInstallState(for: profileID)
        }
    }
}
