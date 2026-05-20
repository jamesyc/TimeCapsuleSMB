import Foundation

struct PendingConfirmation: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let actionTitle: String
    let operation: String
    let params: [String: JSONValue]

    static func deploy(noReboot: Bool, nbnsEnabled: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? "Deploy Without Reboot?" : "Deploy And Reboot?",
            message: noReboot
                ? "This will upload and install the managed TimeCapsuleSMB payload without rebooting the device."
                : "This will upload and install the managed TimeCapsuleSMB payload. NetBSD 6 devices will reboot; NetBSD 4 devices may activate the runtime immediately.",
            actionTitle: noReboot ? "Deploy" : "Deploy And Allow Reboot",
            operation: "deploy",
            params: [
                "dry_run": .bool(false),
                "confirm_deploy": .bool(true),
                "confirm_reboot": .bool(!noReboot),
                "confirm_netbsd4_activation": .bool(true),
                "no_reboot": .bool(noReboot),
                "nbns_enabled": .bool(nbnsEnabled)
            ]
        )
    }

    static func activate() -> PendingConfirmation {
        PendingConfirmation(
            title: "Activate NetBSD 4 Runtime?",
            message: "This will restart the deployed Samba runtime on an older NetBSD 4 device.",
            actionTitle: "Activate",
            operation: "activate",
            params: ["confirm_netbsd4_activation": .bool(true)]
        )
    }

    static func fsck(volume: String, noReboot: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? "Run Disk Repair Without Reboot?" : "Run Disk Repair And Reboot?",
            message: noReboot
                ? "This will run fsck on the selected Time Capsule disk without requesting a reboot afterward."
                : "This will run fsck on the selected Time Capsule disk and wait for the device to reboot.",
            actionTitle: "Run fsck",
            operation: "fsck",
            params: [
                "confirm_fsck": .bool(true),
                "no_reboot": .bool(noReboot),
                "volume": .string(volume)
            ]
        )
    }

    static func uninstall(noReboot: Bool) -> PendingConfirmation {
        PendingConfirmation(
            title: noReboot ? "Uninstall Without Reboot?" : "Uninstall And Reboot?",
            message: noReboot
                ? "This will remove the managed TimeCapsuleSMB payload without rebooting the device."
                : "This will remove the managed TimeCapsuleSMB payload and wait for the device to reboot.",
            actionTitle: "Uninstall",
            operation: "uninstall",
            params: [
                "dry_run": .bool(false),
                "confirm_uninstall": .bool(true),
                "confirm_reboot": .bool(!noReboot),
                "no_reboot": .bool(noReboot)
            ]
        )
    }

    static func repairXattrs(path: String) -> PendingConfirmation {
        PendingConfirmation(
            title: "Repair Extended Attributes?",
            message: "This will repair extended attributes at the selected mounted SMB path.",
            actionTitle: "Repair xattrs",
            operation: "repair-xattrs",
            params: [
                "path": .string(path),
                "dry_run": .bool(false),
                "confirm_repair": .bool(true)
            ]
        )
    }
}
