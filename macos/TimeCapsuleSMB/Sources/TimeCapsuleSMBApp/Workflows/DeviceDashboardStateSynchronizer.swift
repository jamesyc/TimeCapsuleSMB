import Combine
import Foundation

@MainActor
final class DeviceDashboardStateSynchronizer {
    private let appStore: AppStore
    private let doctorStore: DoctorStore
    private let deployStore: DeployWorkflowStore
    private let maintenanceStore: MaintenanceStore

    private var activeCheckupOperation: ActiveOperation?
    private var activeDeployOperation: ActiveOperation?
    private var activeUninstallOperation: ActiveOperation?
    private var cancellables: Set<AnyCancellable> = []

    init(
        appStore: AppStore,
        doctorStore: DoctorStore,
        deployStore: DeployWorkflowStore,
        maintenanceStore: MaintenanceStore,
        flashStore: FlashWorkflowStore
    ) {
        self.appStore = appStore
        self.doctorStore = doctorStore
        self.deployStore = deployStore
        self.maintenanceStore = maintenanceStore
        observeSnapshots()
        observeCredentialInvalidProfileIDs(doctorStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(deployStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(maintenanceStore.activationStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(maintenanceStore.uninstallStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(maintenanceStore.fsckStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(maintenanceStore.repairXattrsStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(maintenanceStore.sshAccessStore.$passwordInvalidProfileID)
        observeCredentialInvalidProfileIDs(flashStore.$passwordInvalidProfileID)
    }

    func trackCheckupStart(_ operation: ActiveOperation) {
        activeCheckupOperation = operation
    }

    func trackDeployStart(_ operation: ActiveOperation, profile: DeviceProfile) {
        activeDeployOperation = operation
        persistStartedDeployState(operation: operation, profile: profile)
        invalidateCheckup(for: operation)
    }

    func trackUninstallStart(_ operation: ActiveOperation) {
        activeUninstallOperation = operation
    }

    func invalidateCheckupIfStarted(_ start: OperationStartResult) {
        guard case .started(let operation) = start else {
            return
        }
        invalidateCheckup(for: operation)
    }

    private func observeSnapshots() {
        doctorStore.$state
            .sink { [weak self] state in
                self?.updateCheckupSnapshot(state: state)
            }
            .store(in: &cancellables)
        deployStore.$state
            .sink { [weak self] state in
                self?.updateDeployState(state: state)
            }
            .store(in: &cancellables)
        deployStore.$currentStage
            .sink { [weak self] stage in
                self?.updateCurrentDeployStage(stage: stage)
            }
            .store(in: &cancellables)
        maintenanceStore.uninstallStore.$state
            .sink { [weak self] state in
                self?.updateUninstallSnapshot(state: state)
            }
            .store(in: &cancellables)
    }

    private func observeCredentialInvalidProfileIDs(_ publisher: Published<DeviceProfile.ID?>.Publisher) {
        publisher
            .sink { [weak self] profileID in
                guard let profileID else { return }
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.appStore.profilePersistence.markCredentialInvalid(profileID: profileID)
                }
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
        guard appStore.operationCoordinator.activeOperation(for: .deviceWorkflow(profileID, .deploy)) == nil else {
            return
        }
        let observedAt = Date()
        let skipSSH = doctorStore.skipSSH
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
                profile: appStore.deviceRegistry.profile(id: profileID),
                skipSSH: skipSSH,
                state: state,
                summary: summary
            )
            await appStore.deviceRegistry.updateCheckup(
                DeviceDashboardSnapshotMapper.checkupSnapshot(
                    state: state,
                    summary: summary,
                    observedAt: observedAt
                ),
                runtimeState: runtimeState,
                for: profileID
            )
        }
    }

    private func persistStartedDeployState(operation: ActiveOperation, profile: DeviceProfile) {
        let startedAt = Date()
        let payloadFamily = profile.payloadFamily
        let stage = deployStore.currentStage?.stage
        let snapshots = DeviceDashboardSnapshotMapper.startedDeploySnapshots(
            operation: operation,
            payloadFamily: payloadFamily,
            stage: stage,
            startedAt: startedAt
        )
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
                for: profile.id
            )
        }
    }

    private func updateCurrentDeployStage(stage: OperationStageState?) {
        guard [.deploying, .awaitingConfirmation].contains(deployStore.state),
              let operation = activeDeployOperation,
              let profileID = operation.profileID else {
            return
        }
        let observedAt = Date()
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            guard let profile = appStore.deviceRegistry.profile(id: profileID),
                  let current = profile.lastDeployState,
                  current.operationID == operation.id.uuidString,
                  current.status.isInProgress else {
                return
            }
            let stage = stage?.stage ?? current.stage
            let snapshots = DeviceDashboardSnapshotMapper.inProgressDeploySnapshots(
                current: current,
                runtimeState: profile.runtimeState,
                status: current.status,
                stage: stage,
                observedAt: observedAt
            )
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
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
            persistAwaitingConfirmationDeployState(operation: operation, profileID: profileID)
            return
        }
        guard [.deployed, .deployFailed].contains(state) else {
            return
        }
        defer {
            activeDeployOperation = nil
        }
        if state == .deployFailed {
            persistFailedDeployState(operation: operation, profileID: profileID)
            return
        }
        persistSucceededDeployState(operation: operation, profileID: profileID)
    }

    private func persistFailedDeployState(operation: ActiveOperation, profileID: DeviceProfile.ID) {
        let failedAt = Date()
        let stage = deployStore.currentStage?.stage
        let error = deployStore.error
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            let profile = appStore.deviceRegistry.profile(id: profileID)
            let payloadFamily = profile?.lastDeployState?.payloadFamily
                ?? profile?.payloadFamily
            let snapshots = DeviceDashboardSnapshotMapper.failedDeploySnapshots(
                operation: operation,
                profile: profile,
                stage: stage,
                payloadFamily: payloadFamily,
                error: error,
                failedAt: failedAt
            )
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
                for: profileID
            )
        }
    }

    private func persistSucceededDeployState(operation: ActiveOperation, profileID: DeviceProfile.ID) {
        guard let result = deployStore.result else {
            return
        }
        let finishedAt = Date()
        let currentStage = deployStore.currentStage?.stage
        let rsyncEnabled = deployStore.runOptions?.rsyncEnabled
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            guard let profile = appStore.deviceRegistry.profile(id: profileID) else { return }
            let stage = currentStage ?? profile.lastDeployState?.stage
            let payloadFamily = profile.payloadFamily
            let snapshots = DeviceDashboardSnapshotMapper.succeededDeploySnapshots(
                operation: operation,
                profile: profile,
                result: result,
                payloadFamily: payloadFamily,
                stage: stage,
                finishedAt: finishedAt
            )
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
                rsyncEnabled: rsyncEnabled,
                for: profile.id
            )
        }
    }

    private func persistAwaitingConfirmationDeployState(operation: ActiveOperation, profileID: DeviceProfile.ID) {
        let observedAt = Date()
        let stage = deployStore.currentStage?.stage
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            guard let profile = appStore.deviceRegistry.profile(id: profileID),
                  let current = profile.lastDeployState,
                  current.operationID == operation.id.uuidString,
                  current.status.isInProgress else {
                return
            }
            let stage = stage ?? current.stage
            let snapshots = DeviceDashboardSnapshotMapper.inProgressDeploySnapshots(
                current: current,
                runtimeState: profile.runtimeState,
                status: .awaitingConfirmation,
                stage: stage,
                observedAt: observedAt
            )
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
                for: profileID
            )
        }
    }

    private func invalidateCheckup(for operation: ActiveOperation) {
        guard let profileID = operation.profileID else {
            return
        }
        doctorStore.invalidateResult()
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            await appStore.deviceRegistry.clearCheckup(for: profileID)
        }
    }

    private func updateUninstallSnapshot(state: MaintenanceOperationState) {
        // The raw store publishes synchronously. Idle ends cancellation/reset
        // tracking without interpreting it as a successful uninstall.
        guard [.idle, .succeeded, .failed].contains(state) else {
            return
        }
        defer {
            activeUninstallOperation = nil
        }
        guard state == .succeeded,
              let profileID = activeUninstallOperation?.profileID else {
            return
        }
        appStore.deviceRegistry.enqueueOperationUpdate { [self] in
            await appStore.deviceRegistry.clearInstallState(for: profileID)
        }
    }
}
