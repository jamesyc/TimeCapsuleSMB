import Foundation

enum FlashUserAction: String, Equatable, Identifiable {
    case backupAndInspect
    case patchBootHook
    case restoreFirmware

    var id: String { rawValue }

    var title: String {
        switch self {
        case .backupAndInspect:
            return L10n.string("flash.action.backup_inspect")
        case .patchBootHook:
            return L10n.string("flash.action.patch_boot_hook")
        case .restoreFirmware:
            return L10n.string("flash.action.restore_firmware")
        }
    }
}

struct FlashPresentation: Equatable {
    let title: String
    let message: String
    let stateTitle: String
    let actions: [FlashUserAction]
    let enabledActions: Set<FlashUserAction>

    init(state: FlashWorkflowState, message: String) {
        self.title = L10n.string("flash.title")
        self.message = message
        self.stateTitle = state.title
        self.actions = [.backupAndInspect, .patchBootHook, .restoreFirmware]
        switch state {
        case .eligibleForReadOnlyAnalysis, .planAvailable:
            self.enabledActions = [.backupAndInspect]
        case .writeLocked, .awaitingStrongConfirmation:
            self.enabledActions = [.backupAndInspect]
        case .writing, .readbackValidating, .writeValidated, .manualPowerCycleRequired, .restoreRebooting:
            self.enabledActions = []
        case .unavailable, .disabledInThisBuild, .readingBanks, .savingBackup, .analyzingBanks, .failed:
            self.enabledActions = []
        }
    }

    func isEnabled(_ action: FlashUserAction) -> Bool {
        enabledActions.contains(action)
    }
}
