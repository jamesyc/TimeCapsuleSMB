import Foundation

enum FlashUserAction: String, Hashable, Identifiable {
    case backupAndInspect
    case planPatch
    case planRestore
    case checkApple
    case downloadApple
    case writePatch
    case writeRestore

    var id: String { rawValue }

    var title: String {
        switch self {
        case .backupAndInspect:
            return L10n.string("flash.action.backup_inspect")
        case .planPatch:
            return L10n.string("flash.action.plan_patch")
        case .planRestore:
            return L10n.string("flash.action.plan_restore")
        case .checkApple:
            return L10n.string("flash.action.check_apple")
        case .downloadApple:
            return L10n.string("flash.action.download_apple")
        case .writePatch:
            return L10n.string("flash.action.write_patch")
        case .writeRestore:
            return L10n.string("flash.action.write_restore")
        }
    }

    var systemImage: String {
        switch self {
        case .backupAndInspect:
            return "externaldrive.badge.questionmark"
        case .planPatch, .planRestore:
            return "doc.text.magnifyingglass"
        case .checkApple:
            return "checkmark.seal"
        case .downloadApple:
            return "checkmark.shield"
        case .writePatch:
            return "bolt.trianglebadge.exclamationmark"
        case .writeRestore:
            return "arrow.uturn.backward.circle"
        }
    }

    static func planAction(for mode: FlashPlanMode) -> FlashUserAction {
        switch mode {
        case .patch:
            return .planPatch
        case .restore:
            return .planRestore
        case .checkApple:
            return .checkApple
        case .downloadOnly:
            return .downloadApple
        }
    }
}

struct FlashPresentation: Equatable {
    let title: String
    let message: String
    let stateTitle: String
    let primaryActions: [FlashUserAction]
    let secondaryActions: [FlashUserAction]
    let enabledActions: Set<FlashUserAction>
    let rows: [PresentationRow]
    let warnings: [String]
    private let backupActionTitle: String

    @MainActor
    init(store: FlashWorkflowStore) {
        self.title = L10n.string("flash.title")
        self.message = store.error?.message ?? store.writeResult?.localizedSummary ?? store.plan?.localizedSummary ?? store.backup?.localizedSummary ?? store.eligibilityMessage
        self.stateTitle = store.state.title
        self.primaryActions = [.backupAndInspect, .planPatch, .planRestore, .writePatch, .writeRestore]
        self.secondaryActions = [.checkApple, .downloadApple]
        self.enabledActions = Self.enabledActions(store: store)
        self.rows = Self.rows(store: store)
        self.warnings = Self.warnings(store: store)
        self.backupActionTitle = store.backupSnapshotStale
            ? L10n.string("flash.action.backup_inspect_again")
            : L10n.string("flash.action.backup_inspect")
    }

    func isEnabled(_ action: FlashUserAction) -> Bool {
        enabledActions.contains(action)
    }

    func title(for action: FlashUserAction) -> String {
        action == .backupAndInspect ? backupActionTitle : action.title
    }

    @MainActor
    private static func enabledActions(store: FlashWorkflowStore) -> Set<FlashUserAction> {
        var actions: Set<FlashUserAction> = []
        if store.canBackup {
            actions.insert(.backupAndInspect)
        }
        if store.canPlanWrites {
            actions.formUnion([.planPatch, .planRestore])
        }
        if store.canPlan {
            actions.formUnion([.checkApple, .downloadApple])
        }
        if store.canWritePatch {
            actions.insert(.writePatch)
        }
        if store.canWriteRestore {
            actions.insert(.writeRestore)
        }
        return actions
    }

    @MainActor
    private static func rows(store: FlashWorkflowStore) -> [PresentationRow] {
        var rows: [PresentationRow] = []
        if let backup = store.backup {
            rows.append(PresentationRow(label: L10n.string("flash.row.backup_dir"), value: backup.backupDir))
            rows.append(PresentationRow(label: L10n.string("flash.row.active_bank"), value: backup.activeBank ?? L10n.string("value.unknown")))
            rows.append(PresentationRow(label: L10n.string("flash.row.banks"), value: "\(backup.banks.count)"))
        }
        if let plan = store.plan {
            rows.append(PresentationRow(label: L10n.string("flash.row.mode"), value: plan.mode.title))
            rows.append(PresentationRow(label: L10n.string("flash.row.write_requested"), value: plan.writeRequested ? L10n.string("value.yes") : L10n.string("value.no")))
            if let match = plan.appleFirmwareMatch {
                rows.append(PresentationRow(label: L10n.string("flash.row.apple_match"), value: match.matched ? L10n.string("value.yes") : L10n.string("value.no")))
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_version"), value: match.templateVersion)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_product"), value: match.templateProductID)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_source"), value: match.templateSource)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_payload_sha256"), value: match.innerSHA256)
            }
            if let payload = plan.firmwarePayload {
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_version"), value: payload.templateVersion)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_product"), value: payload.templateProductID)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_source"), value: payload.templateSource)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_payload_path"), value: plan.firmwarePayloadPath)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_payload_sha256"), value: payload.payloadSHA256)
                if let payloadSize = payload.payloadSize {
                    rows.append(PresentationRow(label: L10n.string("flash.row.firmware_payload_size"), value: Self.byteCount(payloadSize)))
                }
            }
        }
        if let result = store.writeResult {
            rows.append(PresentationRow(label: L10n.string("flash.row.write_status"), value: result.writeStatus))
            rows.append(PresentationRow(label: L10n.string("flash.row.write_validated"), value: result.writeValidated ? L10n.string("value.yes") : L10n.string("value.no")))
        }
        return rows
    }

    private static func appendIfPresent(_ rows: inout [PresentationRow], label: String, value: String?) {
        guard let value else {
            return
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        rows.append(PresentationRow(label: label, value: trimmed))
    }

    private static func byteCount(_ value: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file)
    }

    @MainActor
    private static func warnings(store: FlashWorkflowStore) -> [String] {
        var warnings: [String] = []
        if store.backupSnapshotStale {
            warnings.append(L10n.string("flash.warning.snapshot_stale"))
        }
        if store.manualPowerCycleRequiredAfterWrite {
            warnings.append(L10n.string("flash.warning.manual_power_cycle"))
        }
        return warnings
    }
}

extension FlashManualPowerCycleNotice {
    var title: String {
        L10n.string("flash.manual_power_cycle.title")
    }

    var message: String {
        L10n.string("flash.manual_power_cycle.message")
    }

    var actionTitle: String {
        L10n.string("action.ok")
    }

    var viewCheckupActionTitle: String {
        L10n.string("dashboard.action.view_checkup")
    }
}

extension FlashPlanMode {
    var title: String {
        switch self {
        case .patch:
            return L10n.string("flash.mode.patch")
        case .restore:
            return L10n.string("flash.mode.restore")
        case .checkApple:
            return L10n.string("flash.mode.check_apple")
        case .downloadOnly:
            return L10n.string("flash.mode.download_only")
        }
    }
}
