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
        observeCredentialInvalidProfileIDs(maintenanceStore.$passwordInvalidProfileID)
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
                Task { @MainActor in
                    self?.updateCheckupSnapshot(state: state)
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
        maintenanceStore.$uninstallState
            .sink { [weak self] state in
                Task { @MainActor in
                    self?.updateUninstallSnapshot(state: state)
                }
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
        let observedAt = Date()
        let profile = appStore.deviceRegistry.profile(id: profileID)
        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: doctorStore.skipSSH,
            state: state,
            summary: summary
        )
        Task {
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
        let payloadFamily = deployStore.plan?.payloadFamily ?? profile.payloadFamily
        let stage = deployStore.currentStage?.stage
        let snapshots = DeviceDashboardSnapshotMapper.startedDeploySnapshots(
            operation: operation,
            payloadFamily: payloadFamily,
            stage: stage,
            startedAt: startedAt
        )
        Task {
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
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
            persistFailedDeployState(operation: operation, profileID: profileID)
            return
        }
        persistSucceededDeployState(operation: operation, profileID: profileID)
    }

    private func persistFailedDeployState(operation: ActiveOperation, profileID: DeviceProfile.ID) {
        Task {
            let failedAt = Date()
            let profile = appStore.deviceRegistry.profile(id: profileID)
            let stage = deployStore.currentStage?.stage
            let payloadFamily = profile?.lastDeployState?.payloadFamily
                ?? deployStore.plan?.payloadFamily
                ?? profile?.payloadFamily
            guard let snapshots = DeviceDashboardSnapshotMapper.failedDeploySnapshots(
                operation: operation,
                profile: profile,
                stage: stage,
                payloadFamily: payloadFamily,
                error: deployStore.error,
                failedAt: failedAt
            ) else {
                return
            }
            await appStore.deviceRegistry.updateInstallOperationState(
                deployState: snapshots.deployState,
                runtimeState: snapshots.runtimeState,
                for: profileID
            )
        }
    }

    private func persistSucceededDeployState(operation: ActiveOperation, profileID: DeviceProfile.ID) {
        guard let profile = appStore.deviceRegistry.profile(id: profileID),
              let result = deployStore.result else {
            return
        }
        Task {
            let finishedAt = Date()
            let stage = deployStore.currentStage?.stage ?? profile.lastDeployState?.stage
            let payloadFamily = deployStore.plan?.payloadFamily ?? profile.payloadFamily
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
