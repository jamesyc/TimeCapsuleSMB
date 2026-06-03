import Foundation

enum DeviceDashboardSnapshotMapper {
    static func checkupSnapshot(
        state: DoctorWorkflowState,
        summary: DoctorSummary,
        observedAt: Date
    ) -> DeviceCheckupSnapshot {
        DeviceCheckupSnapshot(
            checkedAt: observedAt,
            state: state,
            passCount: summary.passCount,
            warnCount: summary.warnCount,
            failCount: summary.failCount,
            summary: ""
        )
    }

    static func runtimeStateFromCheckup(
        profile: DeviceProfile?,
        skipSSH: Bool,
        state: DoctorWorkflowState,
        summary: DoctorSummary
    ) -> DeviceRuntimeStateSnapshot? {
        guard !skipSSH, let profile else {
            return nil
        }
        if profile.runtimeState?.state == .installing {
            return nil
        }

        let countSummary = L10n.format("summary.checkup_counts", summary.passCount, summary.warnCount, summary.failCount)
        let payloadFamily = profile.runtimeState?.payloadFamily ?? profile.payloadFamily
        switch state {
        case .passed:
            return DeviceRuntimeStateSnapshot(
                state: .installedVerified,
                source: .doctor,
                stage: nil,
                payloadFamily: payloadFamily,
                verified: true,
                summary: "",
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        case .warning:
            let runtimeAlreadyInstalled = profile.runtimeState?.state.isInstalled == true
            let nextState: DeviceRuntimeState = profile.traits.needsActivationAfterReboot && runtimeAlreadyInstalled
                ? .activationNeeded
                : .installedUnverified
            return DeviceRuntimeStateSnapshot(
                state: nextState,
                source: .doctor,
                stage: nil,
                payloadFamily: payloadFamily,
                verified: false,
                summary: countSummary,
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        case .failed:
            if summary.runtimeNotInstalled {
                return DeviceRuntimeStateSnapshot(
                    state: .notInstalled,
                    source: .doctor,
                    stage: nil,
                    payloadFamily: payloadFamily,
                    verified: false,
                    summary: "",
                    errorCode: DoctorSummary.runtimeNotInstalledResultCode,
                    errorMessage: nil,
                    recovery: nil
                )
            }
            return DeviceRuntimeStateSnapshot(
                state: .unhealthy,
                source: .doctor,
                stage: nil,
                payloadFamily: payloadFamily,
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

    static func startedDeploySnapshots(
        operation: ActiveOperation,
        payloadFamily: String?,
        stage: String?,
        startedAt: Date
    ) -> (deployState: DeviceDeployStateSnapshot, runtimeState: DeviceRuntimeStateSnapshot) {
        (
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
            )
        )
    }

    static func inProgressDeploySnapshots(
        current: DeviceDeployStateSnapshot,
        runtimeState: DeviceRuntimeStateSnapshot?,
        status: DeviceDeployStateStatus,
        stage: String?,
        observedAt: Date
    ) -> (deployState: DeviceDeployStateSnapshot, runtimeState: DeviceRuntimeStateSnapshot) {
        (
            deployState: DeviceDeployStateSnapshot(
                operationID: current.operationID,
                startedAt: current.startedAt,
                updatedAt: observedAt,
                finishedAt: nil,
                status: status,
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
            )
        )
    }

    static func failedDeploySnapshots(
        operation: ActiveOperation,
        profile: DeviceProfile?,
        stage: String?,
        payloadFamily: String?,
        error: BackendErrorViewModel?,
        failedAt: Date
    ) -> (deployState: DeviceDeployStateSnapshot, runtimeState: DeviceRuntimeStateSnapshot)? {
        let current = profile?.lastDeployState
        let errorCode = error?.code
        let errorMessage = error?.message ?? L10n.string("install.state.deploy_failed")
        let recovery = error?.recovery.map(DeviceRecoverySnapshot.init)
        return (
            deployState: DeviceDeployStateSnapshot(
                operationID: current?.operationID ?? operation.id.uuidString,
                startedAt: current?.startedAt ?? failedAt,
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
            )
        )
    }

    static func succeededDeploySnapshots(
        operation: ActiveOperation,
        profile: DeviceProfile,
        result: DeployResultPayload,
        payloadFamily: String?,
        stage: String?,
        finishedAt: Date
    ) -> (deployState: DeviceDeployStateSnapshot, runtimeState: DeviceRuntimeStateSnapshot) {
        let runtimeState: DeviceRuntimeState = result.verified == true ? .installedVerified : .installedUnverified
        let summary = result.message ?? ""
        return (
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
                summary: summary,
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
                summary: summary,
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        )
    }
}
