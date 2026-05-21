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

    static func configure(
        host: String = "",
        selectedRecord: JSONValue? = nil,
        password: String,
        debugLogging: Bool
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "password": .string(password)
        ]
        if let selectedRecord {
            params["selected_record"] = selectedRecord
        } else {
            params["host"] = .string(host)
        }
        if debugLogging {
            params["debug_logging"] = .bool(true)
        }
        return params
    }

    static func doctor(
        bonjourTimeout: Double,
        password: String,
        skipSSH: Bool = false,
        skipBonjour: Bool = false,
        skipSMB: Bool = false
    ) -> [String: JSONValue] {
        withCredentials([
            "bonjour_timeout": .number(bonjourTimeout),
            "skip_ssh": .bool(skipSSH),
            "skip_bonjour": .bool(skipBonjour),
            "skip_smb": .bool(skipSMB)
        ], password: password)
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

    static func activatePlan(password: String) -> [String: JSONValue] {
        withCredentials(["dry_run": .bool(true)], password: password)
    }

    static func activateRun(password: String) -> [String: JSONValue] {
        withCredentials(["dry_run": .bool(false)], password: password)
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
