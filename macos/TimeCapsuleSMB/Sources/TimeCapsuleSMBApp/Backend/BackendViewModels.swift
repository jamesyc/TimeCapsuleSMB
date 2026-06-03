import Foundation

struct OperationStageState: Equatable {
    let operation: String
    let stage: String
    let risk: String?
    let cancellable: Bool?
    let description: String?

    init(
        operation: String,
        stage: String,
        risk: String? = nil,
        cancellable: Bool? = nil,
        description: String? = nil
    ) {
        self.operation = operation
        self.stage = stage
        self.risk = risk
        self.cancellable = cancellable
        self.description = description
    }

    init?(event: BackendEvent) {
        guard event.type == "stage", let stage = event.stage else {
            return nil
        }
        self.operation = event.operation
        self.stage = stage
        self.risk = event.risk
        self.cancellable = event.cancellable
        self.description = event.description
    }
}

enum WorkflowLocalError: Equatable {
    case operationAlreadyRunning
    case operationCouldNotStart
    case deployOptionsInvalid
    case ataIdleSecondsInvalid
    case ataStandbyInvalid
    case deployPlanStale
    case deployPlanNotReady
    case mountWaitInvalid
    case activationPlanRequired
    case uninstallPlanStale
    case uninstallPlanNotReady
    case fsckTargetRequired
    case fsckPlanStale
    case fsckPlanNotReady
    case repairXattrsDepthInvalid
    case repairXattrsPathRequired
    case repairXattrsScanStale
    case flashBackupUnavailable
    case flashBackupRequired
    case flashWritesDisabled
    case flashModeReadOnly
    case flashPlanRequired
    case flashPlanStale

    var code: String {
        switch self {
        case .operationAlreadyRunning:
            return "operation_already_running"
        case .operationCouldNotStart:
            return "operation_could_not_start"
        case .deployOptionsInvalid:
            return "deploy_options_invalid"
        case .ataIdleSecondsInvalid:
            return "ata_idle_seconds_invalid"
        case .ataStandbyInvalid:
            return "ata_standby_invalid"
        case .deployPlanStale:
            return "deploy_plan_stale"
        case .deployPlanNotReady:
            return "deploy_plan_not_ready"
        case .mountWaitInvalid:
            return "mount_wait_invalid"
        case .activationPlanRequired:
            return "activation_plan_required"
        case .uninstallPlanStale:
            return "uninstall_plan_stale"
        case .uninstallPlanNotReady:
            return "uninstall_plan_not_ready"
        case .fsckTargetRequired:
            return "fsck_target_required"
        case .fsckPlanStale:
            return "fsck_plan_stale"
        case .fsckPlanNotReady:
            return "fsck_plan_not_ready"
        case .repairXattrsDepthInvalid:
            return "repair_xattrs_depth_invalid"
        case .repairXattrsPathRequired:
            return "repair_xattrs_path_required"
        case .repairXattrsScanStale:
            return "repair_xattrs_scan_stale"
        case .flashBackupUnavailable:
            return "flash_backup_unavailable"
        case .flashBackupRequired:
            return "flash_backup_required"
        case .flashWritesDisabled:
            return "flash_writes_disabled"
        case .flashModeReadOnly:
            return "flash_mode_read_only"
        case .flashPlanRequired:
            return "flash_plan_required"
        case .flashPlanStale:
            return "flash_plan_stale"
        }
    }

    var message: String {
        L10n.string("workflow.error.\(code)")
    }
}

struct BackendErrorViewModel: Equatable {
    let operation: String
    let code: String
    private let rawMessage: String?
    let localError: WorkflowLocalError?
    let recovery: BackendRecoveryPayload?

    var message: String {
        localError?.message ?? rawMessage ?? ""
    }

    init(event: BackendEvent) {
        self.operation = event.operation
        self.code = event.code ?? "operation_failed"
        self.rawMessage = event.message ?? event.localizedSummary
        self.localError = nil
        self.recovery = try? event.recovery?.decode(BackendRecoveryPayload.self)
    }

    init(operation: String, code: String, message: String, recovery: BackendRecoveryPayload? = nil) {
        self.operation = operation
        self.code = code
        self.rawMessage = message
        self.localError = nil
        self.recovery = recovery
    }

    init(operation: String, localError: WorkflowLocalError, recovery: BackendRecoveryPayload? = nil) {
        self.operation = operation
        self.code = localError.code
        self.rawMessage = nil
        self.localError = localError
        self.recovery = recovery
    }

    init(operation: String, deployState: DeviceDeployStateSnapshot) {
        self.operation = operation
        self.code = deployState.errorCode ?? "operation_failed"
        self.rawMessage = deployState.localizedSummary
        self.localError = nil
        self.recovery = deployState.recovery.map(BackendRecoveryPayload.init)
    }
}

extension BackendEvent {
    var payloadSummaryText: String? {
        guard let payload else {
            return nil
        }
        for key in ["summary", "message", "summary_text"] {
            if let value = payload.stringValue(for: key) {
                return value
            }
        }
        return nil
    }

    var localizedPayloadSummaryText: String? {
        guard let payloadSummaryText else {
            return nil
        }
        return BackendSummaryLocalization.localized(payloadSummaryText, operation: operation, payload: payload)
    }

    var localizedSummary: String {
        if type == "result", let localizedPayloadSummaryText {
            return localizedPayloadSummaryText
        }
        return summary
    }
}
