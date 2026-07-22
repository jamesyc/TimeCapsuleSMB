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
    enum Readiness {
        static func versionCheck(url: String) -> [String: JSONValue] {
            var params: [String: JSONValue] = [:]
            let trimmedURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedURL.isEmpty {
                params["url"] = .string(trimmedURL)
            }
            return params
        }
    }

    enum Discovery {
        static func discover(timeout: Double) -> [String: JSONValue] {
            ["timeout": .number(timeout)]
        }
    }

    enum Reachability {
        static func check(profile: DeviceProfile) -> [String: JSONValue] {
            [
                "ssh_host": .string(DeviceEndpointPolicy.rootSSHTarget(profile.host)),
                "smb_hosts": .array(SMBAddressPolicy.reachabilityHostCandidates(for: profile).map(JSONValue.string)),
                "tcp_timeout": .number(2),
                "ssh_timeout": .number(8)
            ]
        }
    }

    enum SetSSH {
        static func status() -> [String: JSONValue] {
            ["action": .string("status")]
        }

        static func enable(noWait: Bool) -> [String: JSONValue] {
            [
                "action": .string("enable"),
                "no_wait": .bool(noWait)
            ]
        }
    }

    enum Configure {
        static func save(
            host: String = "",
            selectedRecord: JSONValue? = nil,
            password: String,
            debugLogging: Bool,
            internalShareUseDiskRoot: Bool? = nil,
            smbBindLanOnly: Bool? = nil,
            smbBrowseCompatibility: Bool? = nil,
            mdnsAdvertiseAFP: Bool? = nil,
            anyProtocol: Bool? = nil,
            requireSMBEncryption: Bool? = nil,
            forceDisableSMBSigningAndEncryption: Bool? = nil,
            fruitMetadataNetatalk: Bool? = nil,
            ataIdleSeconds: Int? = nil,
            ataStandby: Int? = nil,
            includeAtaStandby: Bool = false,
            localNetworkPreflight: LocalNetworkPreflightResult? = nil
        ) -> [String: JSONValue] {
            var params: [String: JSONValue] = [
                "password": .string(password),
                "persist_password": .bool(false),
                "debug_logging": .bool(debugLogging)
            ]
            if let selectedRecord {
                params["selected_record"] = selectedRecord
            } else {
                params["host"] = .string(DeviceEndpointPolicy.rootSSHTarget(host))
            }
            if let internalShareUseDiskRoot {
                params["internal_share_use_disk_root"] = .bool(internalShareUseDiskRoot)
            }
            if let smbBindLanOnly {
                params["smb_bind_lan_only"] = .bool(smbBindLanOnly)
            }
            if let smbBrowseCompatibility {
                params["smb_browse_compatibility"] = .bool(smbBrowseCompatibility)
            }
            if let mdnsAdvertiseAFP {
                params["mdns_advertise_afp"] = .bool(mdnsAdvertiseAFP)
            }
            if let anyProtocol {
                params["any_protocol"] = .bool(anyProtocol)
            }
            if let requireSMBEncryption {
                params["require_smb_encryption"] = .bool(requireSMBEncryption)
            }
            if let forceDisableSMBSigningAndEncryption {
                params["force_disable_smb_signing_and_encryption"] = .bool(forceDisableSMBSigningAndEncryption)
            }
            if let fruitMetadataNetatalk {
                params["fruit_metadata_netatalk"] = .bool(fruitMetadataNetatalk)
            }
            if let ataIdleSeconds {
                params["ata_idle_seconds"] = .number(Double(ataIdleSeconds))
            }
            if let ataStandby {
                params["ata_standby"] = .number(Double(ataStandby))
            } else if includeAtaStandby {
                params["ata_standby"] = .string("")
            }
            if let localNetworkPreflight {
                for (key, value) in localNetworkPreflight.telemetryFields {
                    params[key] = value
                }
            }
            return params
        }
    }

    enum Doctor {
        static func run(
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
    }

    enum Deploy {
        static func params(
            dryRun: Bool,
            noReboot: Bool,
            noWait: Bool,
            nbnsEnabled: Bool,
            internalShareUseDiskRoot: Bool = false,
            smbBindLanOnly: Bool = DeviceProfileSettings.default.smbBindLanOnly,
            smbBrowseCompatibility: Bool = false,
            mdnsAdvertiseAFP: Bool = DeviceProfileSettings.default.mdnsAdvertiseAFP,
            anyProtocol: Bool = false,
            requireSMBEncryption: Bool = DeviceProfileSettings.default.requireSMBEncryption,
            forceDisableSMBSigningAndEncryption: Bool = DeviceProfileSettings.default.forceDisableSMBSigningAndEncryption,
            fruitMetadataNetatalk: Bool = DeviceProfileSettings.default.fruitMetadataNetatalk,
            debugLogging: Bool,
            ataIdleSeconds: Int,
            ataStandby: Int?,
            mountWait: Double
        ) -> [String: JSONValue] {
            var params: [String: JSONValue] = [
                "dry_run": .bool(dryRun),
                "no_reboot": .bool(noReboot),
                "no_wait": .bool(noWait),
                "nbns_enabled": .bool(nbnsEnabled),
                "internal_share_use_disk_root": .bool(internalShareUseDiskRoot),
                "smb_bind_lan_only": .bool(smbBindLanOnly),
                "smb_browse_compatibility": .bool(smbBrowseCompatibility),
                "mdns_advertise_afp": .bool(mdnsAdvertiseAFP),
                "any_protocol": .bool(anyProtocol),
                "require_smb_encryption": .bool(requireSMBEncryption),
                "force_disable_smb_signing_and_encryption": .bool(forceDisableSMBSigningAndEncryption),
                "fruit_metadata_netatalk": .bool(fruitMetadataNetatalk),
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
    }

    enum Activation {
        static func params(dryRun: Bool) -> [String: JSONValue] {
            ["dry_run": .bool(dryRun)]
        }
    }

    enum Uninstall {
        static func params(dryRun: Bool, noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
            [
                "dry_run": .bool(dryRun),
                "no_reboot": .bool(noReboot),
                "no_wait": .bool(noWait),
                "mount_wait": .number(mountWait)
            ]
        }
    }

    enum Fsck {
        static func listVolumes(mountWait: Double) -> [String: JSONValue] {
            [
                "list_volumes": .bool(true),
                "mount_wait": .number(mountWait)
            ]
        }

        static func run(dryRun: Bool, volume: String, noReboot: Bool, noWait: Bool, mountWait: Double) -> [String: JSONValue] {
            [
                "dry_run": .bool(dryRun),
                "no_reboot": .bool(noReboot),
                "no_wait": .bool(noWait),
                "mount_wait": .number(mountWait),
                "volume": .string(volume)
            ]
        }
    }

    enum RepairXattrs {
        static func params(dryRun: Bool, path: String, options: RepairXattrsOptions = RepairXattrsOptions()) -> [String: JSONValue] {
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

    enum Flash {
        static func backup() -> [String: JSONValue] {
            [
                "action": .string("backup")
            ]
        }

        static func plan(
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
            OperationParams.appendFirmwareSelection(
                to: &params,
                firmwareVersion: firmwareVersion,
                firmwareTemplate: firmwareTemplate
            )
            return params
        }

        static func write(
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
            OperationParams.appendFirmwareSelection(
                to: &params,
                firmwareVersion: firmwareVersion,
                firmwareTemplate: firmwareTemplate
            )
            return params
        }
    }

    private static func appendFirmwareSelection(
        to params: inout [String: JSONValue],
        firmwareVersion: String,
        firmwareTemplate: String
    ) {
        let trimmedVersion = firmwareVersion.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedVersion.isEmpty {
            params["firmware_version"] = .string(trimmedVersion)
        }
        let trimmedTemplate = firmwareTemplate.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedTemplate.isEmpty {
            params["firmware_template"] = .string(trimmedTemplate)
        }
    }
}
