import Foundation

enum OperationParams {
    static func discover(timeout: Double) -> [String: JSONValue] {
        ["timeout": .number(timeout)]
    }

    static func configure(host: String, password: String, debugLogging: Bool) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "host": .string(host),
            "password": .string(password)
        ]
        if debugLogging {
            params["debug_logging"] = .bool(true)
        }
        return params
    }

    static func doctor(bonjourTimeout: Double) -> [String: JSONValue] {
        ["bonjour_timeout": .number(bonjourTimeout)]
    }

    static func deployPlan(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        debugLogging: Bool,
        mountWait: Double
    ) -> [String: JSONValue] {
        [
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ]
    }

    static func deployConfirmed(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        debugLogging: Bool,
        mountWait: Double
    ) -> [String: JSONValue] {
        [
            "dry_run": .bool(false),
            "confirm_deploy": .bool(true),
            "confirm_reboot": .bool(!noReboot),
            "confirm_netbsd4_activation": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ]
    }

    static func uninstallPlan(noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ]
    }

    static func uninstallConfirmed(noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "dry_run": .bool(false),
            "confirm_uninstall": .bool(true),
            "confirm_reboot": .bool(!noReboot),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ]
    }

    static func activateConfirmed() -> [String: JSONValue] {
        ["confirm_netbsd4_activation": .bool(true)]
    }

    static func fsckList(mountWait: Double) -> [String: JSONValue] {
        [
            "list_volumes": .bool(true),
            "mount_wait": .number(mountWait)
        ]
    }

    static func fsckPlan(volume: String, noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait),
            "volume": .string(volume)
        ]
    }

    static func fsckConfirmed(volume: String, noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "confirm_fsck": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait),
            "volume": .string(volume)
        ]
    }

    static func repairXattrsScan(path: String) -> [String: JSONValue] {
        [
            "path": .string(path),
            "dry_run": .bool(true)
        ]
    }

    static func repairXattrsConfirmed(path: String) -> [String: JSONValue] {
        [
            "path": .string(path),
            "dry_run": .bool(false),
            "confirm_repair": .bool(true)
        ]
    }
}
