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
            summary: "",
            device: summary.sections[.device],
            thisMac: summary.sections[.thisMac]
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

        // The device's health is its own section's: a problem on this Mac
        // (its settings, its network, its saved password) leaves it green.
        guard state == .passed || state == .warning || state == .failed,
              let device = summary.sections[.device] else {
            return nil
        }
        let countSummary = L10n.format("summary.checkup_counts", device.passCount, device.warnCount, device.failCount)
        let countSummaryRef = BackendSummary(
            key: "summary.checkup_counts",
            arguments: [.int(device.passCount), .int(device.warnCount), .int(device.failCount)],
            text: countSummary
        )
        let payloadFamily = profile.runtimeState?.payloadFamily ?? profile.payloadFamily
        switch device.status {
        case .notChecked:
            // Nothing ran on the device: keep what was known about it.
            return nil
        case .pass:
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
        case .warn:
            return DeviceRuntimeStateSnapshot(
                state: .installedUnverified,
                source: .doctor,
                stage: nil,
                payloadFamily: payloadFamily,
                verified: false,
                summary: countSummary,
                summaryRef: countSummaryRef,
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        case .fail:
            // Installed files gone from every disk: only Install brings Samba back.
            if summary.runtimeNotInstalled || summary.payloadMissingFromDisk {
                return DeviceRuntimeStateSnapshot(
                    state: .notInstalled,
                    source: .doctor,
                    stage: nil,
                    payloadFamily: payloadFamily,
                    verified: false,
                    summary: "",
                    errorCode: summary.payloadMissingFromDisk
                        ? DoctorSummary.payloadMissingFromDiskResultCode
                        : DoctorSummary.runtimeNotInstalledResultCode,
                    errorMessage: nil,
                    recovery: nil
                )
            }
            return DeviceRuntimeStateSnapshot(
                // Installed but not running: on NetBSD4 that is what Activate fixes.
                state: summary.runtimeNotStarted && profile.traits.isNetBSD4 ? .activationNeeded : .unhealthy,
                source: .doctor,
                stage: nil,
                payloadFamily: payloadFamily,
                verified: false,
                summary: countSummary,
                summaryRef: countSummaryRef,
                // A disk that will not mount has one fix the dashboard names.
                errorCode: summary.diskNotMounting ? DoctorSummary.diskNotMountingResultCode : "doctor_failed",
                errorMessage: nil,
                recovery: nil
            )
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
        var deployState = current
        deployState.updatedAt = observedAt
        deployState.finishedAt = nil
        deployState.status = status
        deployState.stage = stage
        deployState.diagnosticText = nil
        return (
            deployState: deployState,
            runtimeState: DeviceRuntimeStateSnapshot(
                state: .installing,
                source: .deploy,
                stage: stage,
                payloadFamily: runtimeState?.payloadFamily ?? current.payloadFamily,
                verified: runtimeState?.verified,
                summary: runtimeState?.summary ?? "",
                summaryRef: runtimeState?.summaryRef,
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
    ) -> (deployState: DeviceDeployStateSnapshot, runtimeState: DeviceRuntimeStateSnapshot) {
        let current = profile?.lastDeployState?.operationID == operation.id.uuidString
            ? profile?.lastDeployState : nil
        let errorCode = error?.code
        let errorMessage = error?.message ?? L10n.string("install.state.deploy_failed")
        let recovery = error?.recovery.map(DeviceRecoverySnapshot.init)
        return (
            deployState: DeviceDeployStateSnapshot(
                operationID: operation.id.uuidString,
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
                recovery: recovery,
                diagnosticText: error?.diagnosticText
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
        let current = profile.lastDeployState?.operationID == operation.id.uuidString
            ? profile.lastDeployState : nil
        let runtimeState: DeviceRuntimeState = result.verified == true ? .installedVerified : .installedUnverified
        let summary = result.message ?? ""
        let summaryRef = result.summaryRef
        return (
            deployState: DeviceDeployStateSnapshot(
                operationID: operation.id.uuidString,
                startedAt: current?.startedAt ?? finishedAt,
                updatedAt: finishedAt,
                finishedAt: finishedAt,
                status: .succeeded,
                stage: stage,
                payloadFamily: payloadFamily,
                rebootRequested: result.rebootRequested,
                verified: result.verified,
                summary: summary,
                summaryRef: summaryRef,
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
                summaryRef: summaryRef,
                errorCode: nil,
                errorMessage: nil,
                recovery: nil
            )
        )
    }
}
