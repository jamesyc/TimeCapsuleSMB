import Foundation

struct PendingConfirmation: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let actionTitle: String
    let operation: String
    let params: [String: JSONValue]

    static func deploy(noReboot: Bool, nbnsEnabled: Bool, debugLogging: Bool, mountWait: Double, noWait: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? L10n.string("confirm.deploy.no_reboot.title") : (noWait ? L10n.string("confirm.deploy.no_wait.title") : L10n.string("confirm.deploy.reboot.title")),
            message: noReboot
                ? L10n.string("confirm.deploy.no_reboot.message")
                : (noWait
                    ? L10n.string("confirm.deploy.no_wait.message")
                    : L10n.string("confirm.deploy.reboot.message")),
            actionTitle: noReboot ? L10n.string("action.deploy") : L10n.string("action.deploy_allow_reboot"),
            operation: "deploy",
            params: OperationParams.deployConfirmed(
                noReboot: noReboot,
                noWait: noWait,
                nbnsEnabled: nbnsEnabled,
                debugLogging: debugLogging,
                mountWait: mountWait
            )
        )
    }

    static func activate() -> PendingConfirmation {
        PendingConfirmation(
            title: L10n.string("confirm.activate.title"),
            message: L10n.string("confirm.activate.message"),
            actionTitle: L10n.string("action.activate"),
            operation: "activate",
            params: OperationParams.activateConfirmed()
        )
    }

    static func fsck(volume: String, noReboot: Bool, mountWait: Double, noWait: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? L10n.string("confirm.fsck.no_reboot.title") : (noWait ? L10n.string("confirm.fsck.no_wait.title") : L10n.string("confirm.fsck.reboot.title")),
            message: noReboot
                ? L10n.string("confirm.fsck.no_reboot.message")
                : (noWait
                    ? L10n.string("confirm.fsck.no_wait.message")
                    : L10n.string("confirm.fsck.reboot.message")),
            actionTitle: L10n.string("action.run_fsck"),
            operation: "fsck",
            params: OperationParams.fsckConfirmed(
                volume: volume,
                noReboot: noReboot,
                noWait: noWait,
                mountWait: mountWait
            )
        )
    }

    static func uninstall(noReboot: Bool, mountWait: Double, noWait: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? L10n.string("confirm.uninstall.no_reboot.title") : (noWait ? L10n.string("confirm.uninstall.no_wait.title") : L10n.string("confirm.uninstall.reboot.title")),
            message: noReboot
                ? L10n.string("confirm.uninstall.no_reboot.message")
                : (noWait
                    ? L10n.string("confirm.uninstall.no_wait.message")
                    : L10n.string("confirm.uninstall.reboot.message")),
            actionTitle: L10n.string("action.uninstall"),
            operation: "uninstall",
            params: OperationParams.uninstallConfirmed(
                noReboot: noReboot,
                noWait: noWait,
                mountWait: mountWait
            )
        )
    }

    static func repairXattrs(path: String) -> PendingConfirmation {
        PendingConfirmation(
            title: L10n.string("confirm.repair_xattrs.title"),
            message: L10n.string("confirm.repair_xattrs.message"),
            actionTitle: L10n.string("action.repair_xattrs"),
            operation: "repair-xattrs",
            params: OperationParams.repairXattrsConfirmed(path: path)
        )
    }
}
