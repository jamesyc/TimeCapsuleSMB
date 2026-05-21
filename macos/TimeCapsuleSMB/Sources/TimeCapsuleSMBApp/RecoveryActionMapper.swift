import Foundation

enum RecoveryActionKind: String, Equatable {
    case retry
    case runCheckup
    case installSMB
    case startSMB
    case diskRepair
    case metadataRepair
    case openFinder
    case replacePassword
    case copyDiagnostics
    case diagnostics
    case generic
}

struct RecoveryAction: Equatable, Identifiable {
    var id: String {
        "\(kind.rawValue):\(title)"
    }

    let title: String
    let kind: RecoveryActionKind
}

enum RecoveryActionMapper {
    static func actions(for error: BackendErrorViewModel) -> [RecoveryAction] {
        var actions: [RecoveryAction] = []
        if error.code == "auth_failed" {
            actions.append(RecoveryAction(title: "Replace Password", kind: .replacePassword))
        }

        if let suggested = error.recovery?.suggestedOperation {
            actions.append(action(forSuggestedOperation: suggested))
        }

        for title in error.recovery?.actions ?? [] {
            actions.append(RecoveryAction(title: title, kind: inferKind(from: title)))
        }

        if error.recovery?.retryable == true || error.code == "operation_failed" {
            actions.append(RecoveryAction(title: "Retry", kind: .retry))
        }
        actions.append(RecoveryAction(title: "Copy Diagnostics", kind: .copyDiagnostics))
        return deduplicated(actions)
    }

    private static func action(forSuggestedOperation operation: String) -> RecoveryAction {
        switch operation {
        case "doctor":
            return RecoveryAction(title: "Run Checkup", kind: .runCheckup)
        case "deploy":
            return RecoveryAction(title: "Install SMB", kind: .installSMB)
        case "activate":
            return RecoveryAction(title: "Start SMB", kind: .startSMB)
        case "fsck":
            return RecoveryAction(title: "Run Disk Repair", kind: .diskRepair)
        case "repair-xattrs":
            return RecoveryAction(title: "Repair File Metadata", kind: .metadataRepair)
        case "validate-install":
            return RecoveryAction(title: "Open Diagnostics", kind: .diagnostics)
        default:
            return RecoveryAction(title: operation, kind: .generic)
        }
    }

    private static func inferKind(from title: String) -> RecoveryActionKind {
        let lower = title.lowercased()
        if lower.contains("password") {
            return .replacePassword
        }
        if lower.contains("checkup") || lower.contains("doctor") {
            return .runCheckup
        }
        if lower.contains("deploy") || lower.contains("install") {
            return .installSMB
        }
        if lower.contains("activate") || lower.contains("start smb") {
            return .startSMB
        }
        if lower.contains("finder") || lower.contains("smb://") {
            return .openFinder
        }
        if lower.contains("fsck") || lower.contains("disk") {
            return .diskRepair
        }
        if lower.contains("xattr") || lower.contains("metadata") {
            return .metadataRepair
        }
        if lower.contains("diagnostic") {
            return .diagnostics
        }
        if lower.contains("retry") {
            return .retry
        }
        return .generic
    }

    private static func deduplicated(_ actions: [RecoveryAction]) -> [RecoveryAction] {
        var seen: Set<String> = []
        var output: [RecoveryAction] = []
        for action in actions {
            if seen.insert(action.id).inserted {
                output.append(action)
            }
        }
        return output
    }
}
