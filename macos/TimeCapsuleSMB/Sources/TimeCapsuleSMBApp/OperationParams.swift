import Foundation

enum OperationParams {
    private static func withCredentials(_ params: [String: JSONValue], password: String) -> [String: JSONValue] {
        let trimmed = password.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return params
        }
        var updated = params
        updated["credentials"] = .object(["password": .string(password)])
        return updated
    }

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

    static func doctor(bonjourTimeout: Double, password: String) -> [String: JSONValue] {
        withCredentials(["bonjour_timeout": .number(bonjourTimeout)], password: password)
    }

    static func deployPlan(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        debugLogging: Bool,
        mountWait: Double,
        password: String
    ) -> [String: JSONValue] {
        withCredentials([
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ], password: password)
    }

    static func deployRun(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        debugLogging: Bool,
        mountWait: Double,
        password: String
    ) -> [String: JSONValue] {
        withCredentials([
            "dry_run": .bool(false),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ], password: password)
    }

    static func uninstallPlan(noReboot: Bool, noWait: Bool, mountWait: Double, password: String) -> [String: JSONValue] {
        withCredentials([
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ], password: password)
    }

    static func uninstallRun(noReboot: Bool, noWait: Bool, mountWait: Double, password: String) -> [String: JSONValue] {
        withCredentials([
            "dry_run": .bool(false),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ], password: password)
    }

    static func activateRun(password: String) -> [String: JSONValue] {
        withCredentials([:], password: password)
    }

    static func fsckList(mountWait: Double, password: String) -> [String: JSONValue] {
        withCredentials([
            "list_volumes": .bool(true),
            "mount_wait": .number(mountWait)
        ], password: password)
    }

    static func fsckPlan(volume: String, noReboot: Bool, noWait: Bool, mountWait: Double, password: String) -> [String: JSONValue] {
        withCredentials([
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait),
            "volume": .string(volume)
        ], password: password)
    }

    static func fsckRun(volume: String, noReboot: Bool, noWait: Bool, mountWait: Double, password: String) -> [String: JSONValue] {
        withCredentials([
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait),
            "volume": .string(volume)
        ], password: password)
    }

    static func repairXattrsScan(path: String) -> [String: JSONValue] {
        [
            "path": .string(path),
            "dry_run": .bool(true)
        ]
    }

    static func repairXattrsRun(path: String) -> [String: JSONValue] {
        [
            "path": .string(path),
            "dry_run": .bool(false)
        ]
    }
}
