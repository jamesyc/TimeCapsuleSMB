import Foundation

enum RecoveryActionKind: String, Equatable {
    case retry
    case runCheckup = "run_checkup"
    case installSMB = "install_smb"
    case startSMB = "start_smb"
    case uninstall
    case diskRepair = "disk_repair"
    case metadataRepair = "repair_metadata"
    case openFinder = "open_finder"
    case replacePassword = "replace_password"
    case copyDiagnostics = "copy_diagnostics"
    case diagnostics = "open_diagnostics"
    case openSystemSettings = "open_system_settings"
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
            actions.append(action(for: .replacePassword))
        }

        for actionID in error.recovery?.actionIDs ?? [] {
            guard let kind = RecoveryActionKind(rawValue: actionID), kind != .generic else {
                continue
            }
            if allows(kind, for: error) {
                actions.append(action(for: kind))
            }
        }

        if let suggested = error.recovery?.suggestedOperation, suggested != error.operation {
            let suggestedAction = action(forSuggestedOperation: suggested)
            if allows(suggestedAction.kind, for: error) {
                actions.append(suggestedAction)
            }
        }

        if error.recovery?.retryable == true || error.code == "operation_failed" {
            actions.append(action(for: .retry))
        }
        actions.append(action(for: .copyDiagnostics))
        return deduplicated(actions)
    }

    private static func allows(_ kind: RecoveryActionKind, for error: BackendErrorViewModel) -> Bool {
        if error.operation == "deploy" {
            switch kind {
            case .openFinder, .installSMB:
                return false
            default:
                break
            }
        }
        return true
    }

    private static func action(forSuggestedOperation operation: String) -> RecoveryAction {
        switch operation {
        case "doctor":
            return action(for: .runCheckup)
        case "deploy":
            return action(for: .installSMB)
        case "activate":
            return action(for: .startSMB)
        case "uninstall":
            return action(for: .uninstall)
        case "fsck":
            return action(for: .diskRepair)
        case "repair-xattrs":
            return action(for: .metadataRepair)
        case "validate-install":
            return action(for: .diagnostics)
        default:
            return RecoveryAction(title: operation, kind: .generic)
        }
    }

    private static func action(for kind: RecoveryActionKind) -> RecoveryAction {
        RecoveryAction(title: title(for: kind), kind: kind)
    }

    private static func title(for kind: RecoveryActionKind) -> String {
        switch kind {
        case .retry:
            return L10n.string("recovery.action.retry")
        case .runCheckup:
            return L10n.string("recovery.action.run_checkup")
        case .installSMB:
            return L10n.string("recovery.action.install_smb")
        case .startSMB:
            return L10n.string("recovery.action.start_smb")
        case .uninstall:
            return L10n.string("recovery.action.uninstall")
        case .diskRepair:
            return L10n.string("recovery.action.disk_repair")
        case .metadataRepair:
            return L10n.string("recovery.action.metadata_repair")
        case .openFinder:
            return L10n.string("recovery.action.open_finder")
        case .replacePassword:
            return L10n.string("recovery.action.replace_password")
        case .copyDiagnostics:
            return L10n.string("recovery.action.copy_diagnostics")
        case .diagnostics:
            return L10n.string("recovery.action.open_diagnostics")
        case .openSystemSettings:
            return L10n.string("recovery.action.open_system_settings")
        case .generic:
            return L10n.string("recovery.action.open")
        }
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
