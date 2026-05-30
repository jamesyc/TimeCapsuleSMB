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

    static func reachability(profile: DeviceProfile) -> [String: JSONValue] {
        [
            "ssh_host": .string(rootSSHTarget(profile.host)),
            "smb_hosts": .array(SMBAddressPolicy.reachabilityHostCandidates(for: profile).map(JSONValue.string)),
            "tcp_timeout": .number(2),
            "ssh_timeout": .number(8)
        ]
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
        skipSSH: Bool = false,
        skipBonjour: Bool = false,
        skipSMB: Bool = false
    ) -> [String: JSONValue] {
        [
            "skip_ssh": .bool(skipSSH),
            "skip_bonjour": .bool(skipBonjour),
            "skip_smb": .bool(skipSMB)
        ]
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
        mountWait: Double
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
        return params
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
        mountWait: Double
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
        return params
    }

    static func uninstallPlan(noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "dry_run": .bool(true),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ]
    }

    static func uninstallRun(noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "dry_run": .bool(false),
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait)
        ]
    }

    static func activatePlan() -> [String: JSONValue] {
        ["dry_run": .bool(true)]
    }

    static func activateRun() -> [String: JSONValue] {
        ["dry_run": .bool(false)]
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

    static func fsckRun(volume: String, noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
        [
            "no_reboot": .bool(noReboot),
            "no_wait": .bool(noWait),
            "mount_wait": .number(mountWait),
            "volume": .string(volume)
        ]
    }

    static func repairXattrsScan(path: String, options: RepairXattrsOptions = RepairXattrsOptions()) -> [String: JSONValue] {
        repairXattrsParams(path: path, dryRun: true, options: options)
    }

    static func repairXattrsRun(path: String, options: RepairXattrsOptions = RepairXattrsOptions()) -> [String: JSONValue] {
        repairXattrsParams(path: path, dryRun: false, options: options)
    }

    static func flashBackup() -> [String: JSONValue] {
        [
            "action": .string("backup")
        ]
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
        rebootAfterWrite: Bool? = nil,
        waitAfterReboot: Bool = true
    ) -> [String: JSONValue] {
        let shouldReboot = rebootAfterWrite ?? (mode == .restore)
        var params: [String: JSONValue] = [
            "action": .string("write"),
            "backup_dir": .string(backupDir),
            "mode": .string(mode.rawValue),
            "force": .bool(force),
            "reboot_after_write": .bool(shouldReboot)
        ]
        if shouldReboot {
            params["wait_after_reboot"] = .bool(waitAfterReboot)
        }
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
