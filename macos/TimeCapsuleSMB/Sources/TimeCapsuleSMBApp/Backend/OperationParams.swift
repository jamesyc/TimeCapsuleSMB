import Foundation

struct RepairXattrsOptions: Equatable {
    var recursive: Bool = true
    var maxDepth: Int?
    var includeHidden: Bool = false
    var includeTimeMachine: Bool = false
    var fixPermissions: Bool = false
    var verbose: Bool = false
}

enum OperationParams {
    private static func rootSSHTarget(_ host: String) -> String {
        DeviceEndpointPolicy.rootSSHTarget(host)
    }

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

    static func versionCheck(url: String) -> [String: JSONValue] {
        var params: [String: JSONValue] = [:]
        let trimmedURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedURL.isEmpty {
            params["url"] = .string(trimmedURL)
        }
        return params
    }

    static func configure(
        host: String = "",
        selectedRecord: JSONValue? = nil,
        password: String,
        debugLogging: Bool,
        internalShareUseDiskRoot: Bool? = nil,
        anyProtocol: Bool? = nil,
        ataIdleSeconds: Int? = nil,
        ataStandby: Int? = nil,
        includeAtaStandby: Bool = false
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "password": .string(password),
            "persist_password": .bool(false),
            "debug_logging": .bool(debugLogging)
        ]
        if let selectedRecord {
            params["selected_record"] = selectedRecord
        } else {
            params["host"] = .string(rootSSHTarget(host))
        }
        if let internalShareUseDiskRoot {
            params["internal_share_use_disk_root"] = .bool(internalShareUseDiskRoot)
        }
        if let anyProtocol {
            params["any_protocol"] = .bool(anyProtocol)
        }
        if let ataIdleSeconds {
            params["ata_idle_seconds"] = .number(Double(ataIdleSeconds))
        }
        if let ataStandby {
            params["ata_standby"] = .number(Double(ataStandby))
        } else if includeAtaStandby {
            params["ata_standby"] = .string("")
        }
        return params
    }

    static func doctor(
        password: String,
        skipSSH: Bool = false,
        skipBonjour: Bool = false,
        skipSMB: Bool = false
    ) -> [String: JSONValue] {
        withCredentials([
            "skip_ssh": .bool(skipSSH),
            "skip_bonjour": .bool(skipBonjour),
            "skip_smb": .bool(skipSMB)
        ], password: password)
    }

    static func deployPlan(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        internalShareUseDiskRoot: Bool = false,
        anyProtocol: Bool = false,
        debugLogging: Bool,
        ataIdleSeconds: Int,
        ataStandby: Int?,
        mountWait: Double,
        password: String
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "internal_share_use_disk_root": .bool(internalShareUseDiskRoot),
            "any_protocol": .bool(anyProtocol),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ]
        params["ata_idle_seconds"] = .number(Double(ataIdleSeconds))
        if let ataStandby {
            params["ata_standby"] = .number(Double(ataStandby))
        } else {
            params["ata_standby"] = .string("")
        }
        return withCredentials(params, password: password)
    }

    static func deployRun(
        noReboot: Bool,
        noWait: Bool,
        nbnsEnabled: Bool,
        internalShareUseDiskRoot: Bool = false,
        anyProtocol: Bool = false,
        debugLogging: Bool,
        ataIdleSeconds: Int,
        ataStandby: Int?,
        mountWait: Double,
        password: String
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "dry_run": .bool(false),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "nbns_enabled": .bool(nbnsEnabled),
            "internal_share_use_disk_root": .bool(internalShareUseDiskRoot),
            "any_protocol": .bool(anyProtocol),
            "debug_logging": .bool(debugLogging),
            "mount_wait": .number(mountWait)
        ]
        params["ata_idle_seconds"] = .number(Double(ataIdleSeconds))
        if let ataStandby {
            params["ata_standby"] = .number(Double(ataStandby))
        } else {
            params["ata_standby"] = .string("")
        }
        return withCredentials(params, password: password)
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

    static func repairXattrsScan(path: String, options: RepairXattrsOptions = RepairXattrsOptions()) -> [String: JSONValue] {
        repairXattrsParams(path: path, dryRun: true, options: options)
    }

    static func repairXattrsRun(path: String, options: RepairXattrsOptions = RepairXattrsOptions()) -> [String: JSONValue] {
        repairXattrsParams(path: path, dryRun: false, options: options)
    }

    static func flashBackup(password: String) -> [String: JSONValue] {
        withCredentials([
            "action": .string("backup")
        ], password: password)
    }

    static func flashPlan(
        backupDir: String,
        mode: FlashPlanMode,
        force: Bool = false,
        firmwareVersion: String = "",
        firmwareTemplate: String = ""
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "action": .string("plan"),
            "backup_dir": .string(backupDir),
            "mode": .string(mode.rawValue),
            "force": .bool(force)
        ]
        let trimmedVersion = firmwareVersion.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedVersion.isEmpty {
            params["firmware_version"] = .string(trimmedVersion)
        }
        let trimmedTemplate = firmwareTemplate.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedTemplate.isEmpty {
            params["firmware_template"] = .string(trimmedTemplate)
        }
        return params
    }

    static func flashWrite(
        backupDir: String,
        mode: FlashPlanMode,
        force: Bool = false,
        firmwareVersion: String = "",
        firmwareTemplate: String = "",
        password: String
    ) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "action": .string("write"),
            "backup_dir": .string(backupDir),
            "mode": .string(mode.rawValue),
            "force": .bool(force)
        ]
        let trimmedVersion = firmwareVersion.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedVersion.isEmpty {
            params["firmware_version"] = .string(trimmedVersion)
        }
        let trimmedTemplate = firmwareTemplate.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedTemplate.isEmpty {
            params["firmware_template"] = .string(trimmedTemplate)
        }
        return withCredentials(params, password: password)
    }

    private static func repairXattrsParams(path: String, dryRun: Bool, options: RepairXattrsOptions) -> [String: JSONValue] {
        var params: [String: JSONValue] = [
            "path": .string(path),
            "dry_run": .bool(dryRun),
            "recursive": .bool(options.recursive),
            "include_hidden": .bool(options.includeHidden),
            "include_time_machine": .bool(options.includeTimeMachine),
            "fix_permissions": .bool(options.fixPermissions),
            "verbose": .bool(options.verbose)
        ]
        if let maxDepth = options.maxDepth {
            params["max_depth"] = .number(Double(maxDepth))
        }
        return params
    }
}
