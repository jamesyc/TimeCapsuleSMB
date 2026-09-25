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
    case mountWaitInvalid
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
        case .mountWaitInvalid:
            return "mount_wait_invalid"
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
    let diagnosticText: String?

    var message: String {
        localError?.message ?? BackendErrorLocalization.message(operation: operation, code: code) ?? rawMessage ?? ""
    }

    init(event: BackendEvent) {
        self.operation = event.operation
        self.code = event.code ?? "operation_failed"
        self.rawMessage = event.type == "result"
            ? event.localizedPayloadSummaryText ?? event.localizedSummary
            : event.message ?? event.localizedSummary
        self.localError = nil
        self.recovery = try? event.recovery?.decode(BackendRecoveryPayload.self)
        self.diagnosticText = BackendDiagnosticText.make(
            message: event.message ?? event.payloadSummaryText,
            debug: event.debug
        )
    }

    init(operation: String, code: String, message: String, recovery: BackendRecoveryPayload? = nil) {
        self.operation = operation
        self.code = code
        self.rawMessage = message
        self.localError = nil
        self.recovery = recovery
        self.diagnosticText = nil
    }

    init(operation: String, localError: WorkflowLocalError, recovery: BackendRecoveryPayload? = nil) {
        self.operation = operation
        self.code = localError.code
        self.rawMessage = nil
        self.localError = localError
        self.recovery = recovery
        self.diagnosticText = nil
    }

    init(operation: String, deployState: DeviceDeployStateSnapshot) {
        self.operation = operation
        self.code = deployState.errorCode ?? "operation_failed"
        self.rawMessage = deployState.localizedSummary
        self.localError = nil
        self.recovery = deployState.recovery.map(BackendRecoveryPayload.init)
        self.diagnosticText = deployState.diagnosticText
    }
}

enum BackendDiagnosticText {
    static let maxUTF8Bytes = 32 * 1024
    static let truncationMarker = "\n[diagnostic text truncated at 32 KiB]"

    static func make(message: String?, debug: JSONValue?) -> String? {
        var lines: [String] = []
        if let message, !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            lines.append("Message:")
            lines.append(contentsOf: indentedLines(message, indent: 2))
        }
        if let debug {
            lines.append("Debug:")
            lines.append(contentsOf: render(redacted(debug), indent: 2))
        }
        guard !lines.isEmpty else {
            return nil
        }
        return limit(lines.joined(separator: "\n"))
    }

    static func redacted(_ value: JSONValue, key: String? = nil) -> JSONValue {
        if shouldRedact(key) {
            return .string("<redacted>")
        }
        switch value {
        case .object(let object):
            return .object(Dictionary(uniqueKeysWithValues: object.map { childKey, childValue in
                (childKey, redacted(childValue, key: childKey))
            }))
        case .array(let values):
            return .array(values.map { redacted($0, key: key) })
        default:
            return value
        }
    }

    static func redacted(_ value: String, key: String? = nil) -> String {
        shouldRedact(key) ? "<redacted>" : value
    }

    private static func shouldRedact(_ key: String?) -> Bool {
        guard let key = key?.lowercased() else {
            return false
        }
        return key.contains("password")
            || key.contains("token")
            || key.contains("secret")
            || key.contains("authorization")
            || key.contains("api_key")
            || key.contains("apikey")
            || key.contains("private_key")
            || key.contains("privatekey")
            || key.contains("credentials")
    }

    private static func render(_ value: JSONValue, indent: Int) -> [String] {
        let prefix = String(repeating: " ", count: indent)
        switch value {
        case .object(let object):
            return object.keys.sorted().flatMap { key -> [String] in
                guard let child = object[key] else { return [] }
                if let scalar = scalarText(child) {
                    return ["\(prefix)\(key): \(scalar)"]
                }
                return ["\(prefix)\(key):"] + render(child, indent: indent + 2)
            }
        case .array(let values):
            return values.flatMap { child -> [String] in
                if let scalar = scalarText(child) {
                    return ["\(prefix)- \(scalar)"]
                }
                return ["\(prefix)-"] + render(child, indent: indent + 2)
            }
        case .string(let string):
            return indentedLines(string, indent: indent)
        case .number, .bool, .null:
            return ["\(prefix)\(value.displayText)"]
        }
    }

    private static func scalarText(_ value: JSONValue) -> String? {
        switch value {
        case .string(let string) where !string.contains("\n"):
            return string
        case .number, .bool, .null:
            return value.displayText
        case .string, .object, .array:
            return nil
        }
    }

    private static func indentedLines(_ value: String, indent: Int) -> [String] {
        let prefix = String(repeating: " ", count: indent)
        return value.split(separator: "\n", omittingEmptySubsequences: false).map { "\(prefix)\($0)" }
    }

    private static func limit(_ value: String) -> String {
        let data = Data(value.utf8)
        guard data.count > maxUTF8Bytes else {
            return value
        }
        let markerData = Data(truncationMarker.utf8)
        var prefixLength = maxUTF8Bytes - markerData.count
        while prefixLength > 0 {
            if let prefix = String(data: data.prefix(prefixLength), encoding: .utf8) {
                return prefix + truncationMarker
            }
            prefixLength -= 1
        }
        return truncationMarker
    }
}

enum BackendErrorLocalization {
    static func message(operation: String, code: String) -> String? {
        for key in ["backend.error.\(operation).\(code)", "backend.error.\(code)"] {
            let value = L10n.string(key)
            if value != key {
                return value
            }
        }
        return nil
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
        if type == "error",
           let code,
           let localizedMessage = BackendErrorLocalization.message(operation: operation, code: code) {
            return L10n.format("event.summary.error", operation, localizedMessage)
        }
        return summary
    }
}
