import Foundation

struct CapabilitiesPayload: Decodable, Equatable {
    let schemaVersion: Int
    let apiSchemaVersion: Int
    let helperVersion: String
    let helperVersionCode: Int
    let operations: [String]
    let distributionRoot: String
    let artifactManifestSHA256: String?
    let confirmationSchemaVersion: Int
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case apiSchemaVersion = "api_schema_version"
        case helperVersion = "helper_version"
        case helperVersionCode = "helper_version_code"
        case operations
        case distributionRoot = "distribution_root"
        case artifactManifestSHA256 = "artifact_manifest_sha256"
        case confirmationSchemaVersion = "confirmation_schema_version"
        case summary
    }
}

struct InstallValidationPayload: Decodable, Equatable {
    let schemaVersion: Int
    let ok: Bool
    let checks: [InstallCheckPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case ok
        case checks
        case counts
        case summary
    }
}

struct VersionCheckPayload: Decodable, Equatable {
    let schemaVersion: Int
    let shouldBlock: Bool
    let updateAvailable: Bool
    let checkedURL: String
    let message: String
    let downloadURL: String
    let localVersionCode: Int
    let currentVersion: Int?
    let minSupportedVersion: Int?
    let latestTag: String?
    let source: String
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case shouldBlock = "should_block"
        case updateAvailable = "update_available"
        case checkedURL = "checked_url"
        case message
        case downloadURL = "download_url"
        case localVersionCode = "local_version_code"
        case currentVersion = "current_version"
        case minSupportedVersion = "min_supported_version"
        case latestTag = "latest_tag"
        case source
        case summary
    }
}

struct ReachabilityPayload: Decodable, Equatable {
    let schemaVersion: Int
    let status: String
    let sshHost: String?
    let smbHost: String?
    let checks: [ReachabilityCheckPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case status
        case sshHost = "ssh_host"
        case smbHost = "smb_host"
        case checks
        case counts
        case summary
    }
}

struct ReachabilityCheckPayload: Decodable, Equatable {
    let id: String
    let status: String
    let message: String
    let host: String?
    let detail: String?
}

struct InstallCheckPayload: Decodable, Equatable {
    let id: String
    let ok: Bool
    let message: String
    let details: JSONValue?
}

struct DiscoverPayload: Decodable, Equatable {
    let schemaVersion: Int
    let instances: [BonjourServiceInstancePayload]
    let resolved: [BonjourResolvedServicePayload]
    let devices: [DiscoveredDevicePayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case instances
        case resolved
        case devices
        case counts
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.instances = try container.decodeIfPresent([BonjourServiceInstancePayload].self, forKey: .instances) ?? []
        self.resolved = try container.decodeIfPresent([BonjourResolvedServicePayload].self, forKey: .resolved) ?? []
        self.devices = try container.decodeIfPresent([DiscoveredDevicePayload].self, forKey: .devices) ?? []
        self.counts = try container.decodeIfPresent([String: Int].self, forKey: .counts) ?? [:]
        self.summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? ""
    }
}

struct DiscoveredDevicePayload: Decodable, Equatable {
    let id: String
    let name: String
    let host: String
    let sshHost: String?
    let hostname: String
    let addresses: [String]
    let ipv4: [String]
    let ipv6: [String]
    let preferredIPv4: String?
    let linkLocalOnly: Bool
    let syap: String?
    let model: String?
    let serviceType: String
    let fullname: String
    let selectedRecord: JSONValue

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case host
        case sshHost = "ssh_host"
        case hostname
        case addresses
        case ipv4
        case ipv6
        case preferredIPv4 = "preferred_ipv4"
        case linkLocalOnly = "link_local_only"
        case syap
        case model
        case serviceType = "service_type"
        case fullname
        case selectedRecord = "selected_record"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try container.decodeIfPresent(String.self, forKey: .id) ?? ""
        self.name = try container.decodeIfPresent(String.self, forKey: .name) ?? ""
        self.host = try container.decodeIfPresent(String.self, forKey: .host) ?? ""
        self.sshHost = try container.decodeIfPresent(String.self, forKey: .sshHost)
        self.hostname = try container.decodeIfPresent(String.self, forKey: .hostname) ?? ""
        self.addresses = try container.decodeIfPresent([String].self, forKey: .addresses) ?? []
        self.ipv4 = try container.decodeIfPresent([String].self, forKey: .ipv4) ?? []
        self.ipv6 = try container.decodeIfPresent([String].self, forKey: .ipv6) ?? []
        self.preferredIPv4 = try container.decodeIfPresent(String.self, forKey: .preferredIPv4)
        self.linkLocalOnly = try container.decodeIfPresent(Bool.self, forKey: .linkLocalOnly) ?? false
        self.syap = try container.decodeIfPresent(String.self, forKey: .syap)
        self.model = try container.decodeIfPresent(String.self, forKey: .model)
        self.serviceType = try container.decodeIfPresent(String.self, forKey: .serviceType) ?? ""
        self.fullname = try container.decodeIfPresent(String.self, forKey: .fullname) ?? ""
        self.selectedRecord = try container.decodeIfPresent(JSONValue.self, forKey: .selectedRecord) ?? .null
    }
}

struct BonjourServiceInstancePayload: Decodable, Equatable {
    let serviceType: String
    let name: String
    let fullname: String

    enum CodingKeys: String, CodingKey {
        case serviceType = "service_type"
        case name
        case fullname
    }
}

struct BonjourResolvedServicePayload: Decodable, Equatable {
    let name: String
    let hostname: String
    let serviceType: String
    let port: Int
    let ipv4: [String]
    let ipv6: [String]
    let services: [String]
    let properties: [String: String]
    let fullname: String

    enum CodingKeys: String, CodingKey {
        case name
        case hostname
        case serviceType = "service_type"
        case port
        case ipv4
        case ipv6
        case services
        case properties
        case fullname
    }

    init(
        name: String,
        hostname: String,
        serviceType: String = "",
        port: Int = 0,
        ipv4: [String] = [],
        ipv6: [String] = [],
        services: [String] = [],
        properties: [String: String] = [:],
        fullname: String = ""
    ) {
        self.name = name
        self.hostname = hostname
        self.serviceType = serviceType
        self.port = port
        self.ipv4 = ipv4
        self.ipv6 = ipv6
        self.services = services
        self.properties = properties
        self.fullname = fullname
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.name = try container.decodeIfPresent(String.self, forKey: .name) ?? ""
        self.hostname = try container.decodeIfPresent(String.self, forKey: .hostname) ?? ""
        self.serviceType = try container.decodeIfPresent(String.self, forKey: .serviceType) ?? ""
        self.port = try container.decodeIfPresent(Int.self, forKey: .port) ?? 0
        self.ipv4 = try container.decodeIfPresent([String].self, forKey: .ipv4) ?? []
        self.ipv6 = try container.decodeIfPresent([String].self, forKey: .ipv6) ?? []
        self.services = try container.decodeIfPresent([String].self, forKey: .services) ?? []
        self.properties = try container.decodeIfPresent([String: String].self, forKey: .properties) ?? [:]
        self.fullname = try container.decodeIfPresent(String.self, forKey: .fullname) ?? ""
    }

    var jsonValue: JSONValue {
        .object([
            "name": .string(name),
            "hostname": .string(hostname),
            "service_type": .string(serviceType),
            "port": .number(Double(port)),
            "ipv4": .array(ipv4.map(JSONValue.string)),
            "ipv6": .array(ipv6.map(JSONValue.string)),
            "services": .array(services.map(JSONValue.string)),
            "properties": .object(properties.mapValues(JSONValue.string)),
            "fullname": .string(fullname)
        ])
    }
}

struct ConfigurePayload: Decodable, Equatable {
    let schemaVersion: Int
    let configPath: String
    let host: String
    let configureId: String
    let sshAuthenticated: Bool
    let deviceSyap: String?
    let deviceModel: String?
    let compatibility: DeviceCompatibilityPayload?
    let device: DevicePayload?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case configPath = "config_path"
        case host
        case configureId = "configure_id"
        case sshAuthenticated = "ssh_authenticated"
        case deviceSyap = "device_syap"
        case deviceModel = "device_model"
        case compatibility
        case device
        case summary
    }
}

struct DevicePayload: Decodable, Equatable {
    let host: String?
    let syap: String?
    let model: String?
}

struct DeviceCompatibilityPayload: Decodable, Equatable {
    let osName: String?
    let osRelease: String?
    let arch: String?
    let elfEndianness: String?
    let payloadFamily: String?
    let deviceGeneration: String?
    let supported: Bool?
    let reasonCode: String?
    let reasonDetail: String?
    let syapCandidates: [String]
    let modelCandidates: [String]

    enum CodingKeys: String, CodingKey {
        case osName = "os_name"
        case osRelease = "os_release"
        case arch
        case elfEndianness = "elf_endianness"
        case payloadFamily = "payload_family"
        case deviceGeneration = "device_generation"
        case supported
        case reasonCode = "reason_code"
        case reasonDetail = "reason_detail"
        case syapCandidates = "syap_candidates"
        case modelCandidates = "model_candidates"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.osName = try container.decodeIfPresent(String.self, forKey: .osName)
        self.osRelease = try container.decodeIfPresent(String.self, forKey: .osRelease)
        self.arch = try container.decodeIfPresent(String.self, forKey: .arch)
        self.elfEndianness = try container.decodeIfPresent(String.self, forKey: .elfEndianness)
        self.payloadFamily = try container.decodeIfPresent(String.self, forKey: .payloadFamily)
        self.deviceGeneration = try container.decodeIfPresent(String.self, forKey: .deviceGeneration)
        self.supported = try container.decodeIfPresent(Bool.self, forKey: .supported)
        self.reasonCode = try container.decodeIfPresent(String.self, forKey: .reasonCode)
        self.reasonDetail = try container.decodeIfPresent(String.self, forKey: .reasonDetail)
        self.syapCandidates = try container.decodeIfPresent([String].self, forKey: .syapCandidates) ?? []
        self.modelCandidates = try container.decodeIfPresent([String].self, forKey: .modelCandidates) ?? []
    }
}

enum DeployStartupMode: String, Decodable, Equatable {
    case rebootThenVerify = "reboot_then_verify"
    case rebootThenActivate = "reboot_then_activate"
    case activateNow = "activate_now"

    static func fallback(netbsd4: Bool, requiresReboot: Bool) -> DeployStartupMode {
        if !requiresReboot {
            return .activateNow
        }
        return netbsd4 ? .rebootThenActivate : .rebootThenVerify
    }
}

struct DeployPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let host: String
    let volumeRoot: String?
    let payloadDir: String
    let payloadFamily: String?
    let netbsd4: Bool
    let requiresReboot: Bool
    let rebootRequired: Bool?
    let startupMode: DeployStartupMode
    let uploads: [JSONValue]
    let preUploadActions: [JSONValue]
    let postUploadActions: [JSONValue]
    let activationActions: [JSONValue]
    let postDeployChecks: [PlannedCheckPayload]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case host
        case volumeRoot = "volume_root"
        case payloadDir = "payload_dir"
        case payloadFamily = "payload_family"
        case netbsd4
        case requiresReboot = "requires_reboot"
        case rebootRequired = "reboot_required"
        case startupMode = "startup_mode"
        case uploads
        case preUploadActions = "pre_upload_actions"
        case postUploadActions = "post_upload_actions"
        case activationActions = "activation_actions"
        case postDeployChecks = "post_deploy_checks"
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.host = try container.decode(String.self, forKey: .host)
        self.volumeRoot = try container.decodeIfPresent(String.self, forKey: .volumeRoot)
        self.payloadDir = try container.decode(String.self, forKey: .payloadDir)
        self.payloadFamily = try container.decodeIfPresent(String.self, forKey: .payloadFamily)
        self.netbsd4 = try container.decode(Bool.self, forKey: .netbsd4)
        self.requiresReboot = try container.decode(Bool.self, forKey: .requiresReboot)
        self.rebootRequired = try container.decodeIfPresent(Bool.self, forKey: .rebootRequired)
        self.startupMode = try container.decodeIfPresent(DeployStartupMode.self, forKey: .startupMode)
            ?? DeployStartupMode.fallback(netbsd4: netbsd4, requiresReboot: requiresReboot)
        self.uploads = try container.decodeIfPresent([JSONValue].self, forKey: .uploads) ?? []
        self.preUploadActions = try container.decodeIfPresent([JSONValue].self, forKey: .preUploadActions) ?? []
        self.postUploadActions = try container.decodeIfPresent([JSONValue].self, forKey: .postUploadActions) ?? []
        self.activationActions = try container.decodeIfPresent([JSONValue].self, forKey: .activationActions) ?? []
        self.postDeployChecks = try container.decodeIfPresent([PlannedCheckPayload].self, forKey: .postDeployChecks) ?? []
        self.summary = try container.decode(String.self, forKey: .summary)
    }
}

struct DeployResultPayload: Decodable, Equatable {
    let schemaVersion: Int
    let payloadDir: String
    let netbsd4: Bool
    let payloadFamily: String?
    let requiresReboot: Bool
    let rebooted: Bool?
    let rebootRequested: Bool?
    let waited: Bool?
    let verified: Bool?
    let message: String?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case payloadDir = "payload_dir"
        case netbsd4
        case payloadFamily = "payload_family"
        case requiresReboot = "requires_reboot"
        case rebooted
        case rebootRequested = "reboot_requested"
        case waited
        case verified
        case message
        case summary
    }
}

struct DoctorPayload: Decodable, Equatable {
    let schemaVersion: Int
    let fatal: Bool
    let results: [DoctorCheckPayload]
    let counts: [String: Int]
    let error: String?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case fatal
        case results
        case counts
        case error
        case summary
    }
}

struct DoctorCheckPayload: Decodable, Equatable {
    let status: String
    let message: String
    let details: JSONValue

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.status = try container.decode(String.self, forKey: .status)
        self.message = try container.decode(String.self, forKey: .message)
        self.details = try container.decodeIfPresent(JSONValue.self, forKey: .details) ?? .object([:])
    }

    enum CodingKeys: String, CodingKey {
        case status
        case message
        case details
    }
}

struct FsckVolumeListPayload: Decodable, Equatable {
    let schemaVersion: Int
    let targets: [FsckTargetPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case targets
        case counts
        case summary
    }
}

struct FsckTargetPayload: Decodable, Equatable {
    let name: String?
    let builtin: Bool?
    let device: String
    let mountpoint: String
}

struct ActivationPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let actions: [JSONValue]
    let postActivationChecks: [PlannedCheckPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case actions
        case postActivationChecks = "post_activation_checks"
        case counts
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.actions = try container.decodeIfPresent([JSONValue].self, forKey: .actions) ?? []
        self.postActivationChecks = try container.decodeIfPresent([PlannedCheckPayload].self, forKey: .postActivationChecks) ?? []
        self.counts = try container.decodeIfPresent([String: Int].self, forKey: .counts) ?? [:]
        self.summary = try container.decode(String.self, forKey: .summary)
    }
}

struct ActivationResultPayload: Decodable, Equatable {
    let schemaVersion: Int
    let alreadyActive: Bool
    let message: String?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case alreadyActive = "already_active"
        case message
        case summary
    }
}

struct UninstallPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let host: String
    let volumeRoots: [String]
    let payloadDirs: [String]
    let remoteActions: [JSONValue]
    let requiresReboot: Bool
    let rebootRequired: Bool?
    let postUninstallChecks: [PlannedCheckPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case host
        case volumeRoots = "volume_roots"
        case payloadDirs = "payload_dirs"
        case remoteActions = "remote_actions"
        case requiresReboot = "requires_reboot"
        case rebootRequired = "reboot_required"
        case postUninstallChecks = "post_uninstall_checks"
        case counts
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.host = try container.decode(String.self, forKey: .host)
        self.volumeRoots = try container.decodeIfPresent([String].self, forKey: .volumeRoots) ?? []
        self.payloadDirs = try container.decodeIfPresent([String].self, forKey: .payloadDirs) ?? []
        self.remoteActions = try container.decodeIfPresent([JSONValue].self, forKey: .remoteActions) ?? []
        self.requiresReboot = try container.decode(Bool.self, forKey: .requiresReboot)
        self.rebootRequired = try container.decodeIfPresent(Bool.self, forKey: .rebootRequired)
        self.postUninstallChecks = try container.decodeIfPresent([PlannedCheckPayload].self, forKey: .postUninstallChecks) ?? []
        self.counts = try container.decodeIfPresent([String: Int].self, forKey: .counts) ?? [:]
        self.summary = try container.decode(String.self, forKey: .summary)
    }
}

struct FsckPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let target: FsckTargetPayload?
    let device: String
    let mountpoint: String
    let rebootRequired: Bool
    let waitAfterReboot: Bool
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case target
        case device
        case mountpoint
        case rebootRequired = "reboot_required"
        case waitAfterReboot = "wait_after_reboot"
        case summary
    }
}

struct FsckResultPayload: Decodable, Equatable {
    let schemaVersion: Int
    let device: String
    let mountpoint: String
    let returncode: Int?
    let rebootRequested: Bool?
    let waited: Bool?
    let verified: Bool?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case device
        case mountpoint
        case returncode
        case rebootRequested = "reboot_requested"
        case waited
        case verified
        case summary
    }
}

struct RepairXattrsPayload: Decodable, Equatable {
    let schemaVersion: Int
    let returncode: Int?
    let root: String?
    let findingCount: Int
    let repairableCount: Int
    let counts: [String: Int]
    let stats: JSONValue?
    let report: String?
    let telemetryResult: JSONValue?
    let error: String?
    let summary: String
    let summaryText: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case returncode
        case root
        case findingCount = "finding_count"
        case repairableCount = "repairable_count"
        case counts
        case stats
        case report
        case telemetryResult = "telemetry_result"
        case error
        case summary
        case summaryText = "summary_text"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.returncode = try container.decodeIfPresent(Int.self, forKey: .returncode)
        self.root = try container.decodeIfPresent(String.self, forKey: .root)
        self.findingCount = try container.decodeIfPresent(Int.self, forKey: .findingCount) ?? 0
        self.repairableCount = try container.decodeIfPresent(Int.self, forKey: .repairableCount) ?? 0
        self.counts = try container.decodeIfPresent([String: Int].self, forKey: .counts) ?? [:]
        self.stats = try container.decodeIfPresent(JSONValue.self, forKey: .stats)
        self.report = try container.decodeIfPresent(String.self, forKey: .report)
        self.telemetryResult = try container.decodeIfPresent(JSONValue.self, forKey: .telemetryResult)
        self.error = try container.decodeIfPresent(String.self, forKey: .error)
        self.summary = try container.decode(String.self, forKey: .summary)
        self.summaryText = try container.decodeIfPresent(String.self, forKey: .summaryText)
    }
}

struct FlashBankPayload: Decodable, Equatable, Identifiable {
    let name: String
    let device: String
    let size: Int
    let sha256: String
    let backupValid: Bool
    let activeCandidate: Bool
    let wouldWrite: Bool
    let writeDecision: String
    let login: JSONValue?
    let footer: JSONValue?
    let patch: JSONValue?
    let patchError: String?
    let analysisError: String?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case device
        case size
        case sha256
        case backupValid = "backup_valid"
        case activeCandidate = "active_candidate"
        case wouldWrite = "would_write"
        case writeDecision = "write_decision"
        case login
        case footer
        case patch
        case patchError = "patch_error"
        case analysisError = "analysis_error"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.name = try container.decodeIfPresent(String.self, forKey: .name) ?? ""
        self.device = try container.decodeIfPresent(String.self, forKey: .device) ?? ""
        self.size = try container.decodeIfPresent(Int.self, forKey: .size) ?? 0
        self.sha256 = try container.decodeIfPresent(String.self, forKey: .sha256) ?? ""
        self.backupValid = try container.decodeIfPresent(Bool.self, forKey: .backupValid) ?? false
        self.activeCandidate = try container.decodeIfPresent(Bool.self, forKey: .activeCandidate) ?? false
        self.wouldWrite = try container.decodeIfPresent(Bool.self, forKey: .wouldWrite) ?? false
        self.writeDecision = try container.decodeIfPresent(String.self, forKey: .writeDecision) ?? ""
        self.login = try container.decodeIfPresent(JSONValue.self, forKey: .login)
        self.footer = try container.decodeIfPresent(JSONValue.self, forKey: .footer)
        self.patch = try container.decodeIfPresent(JSONValue.self, forKey: .patch)
        self.patchError = try container.decodeIfPresent(String.self, forKey: .patchError)
        self.analysisError = try container.decodeIfPresent(String.self, forKey: .analysisError)
    }
}

struct FlashBackupPayload: Decodable, Equatable {
    let schemaVersion: Int
    let backupDir: String
    let host: String?
    let syap: String?
    let deviceModel: String?
    let osRelease: String?
    let activeBank: String?
    let banks: [FlashBankPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case backupDir = "backup_dir"
        case host
        case syap
        case deviceModel = "device_model"
        case osRelease = "os_release"
        case activeBank = "active_bank"
        case banks
        case counts
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.backupDir = try container.decode(String.self, forKey: .backupDir)
        self.host = try container.decodeIfPresent(String.self, forKey: .host)
        self.syap = try container.decodeIfPresent(String.self, forKey: .syap)
        self.deviceModel = try container.decodeIfPresent(String.self, forKey: .deviceModel)
        self.osRelease = try container.decodeIfPresent(String.self, forKey: .osRelease)
        self.activeBank = try container.decodeIfPresent(String.self, forKey: .activeBank)
        self.banks = try container.decodeIfPresent([FlashBankPayload].self, forKey: .banks) ?? []
        self.counts = try container.decodeIfPresent([String: Int].self, forKey: .counts) ?? [:]
        self.summary = try container.decode(String.self, forKey: .summary)
    }
}

struct FlashAppleFirmwareMatchPayload: Decodable, Equatable {
    let matched: Bool
    let templateSource: String
    let templatePath: String?
    let templateProductID: String?
    let templateVersion: String?
    let templateSHA256: String?
    let innerSHA256: String?
    let innerSize: Int?
    let keyID: String?
    let innerModel: Int?
    let innerVersion: String?

    enum CodingKeys: String, CodingKey {
        case matched
        case templateSource = "template_source"
        case templatePath = "template_path"
        case templateProductID = "template_product_id"
        case templateVersion = "template_version"
        case templateSHA256 = "template_sha256"
        case innerSHA256 = "inner_sha256"
        case innerSize = "inner_size"
        case keyID = "key_id"
        case innerModel = "inner_model"
        case innerVersion = "inner_version"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.matched = try container.decodeIfPresent(Bool.self, forKey: .matched) ?? false
        self.templateSource = try container.decodeIfPresent(String.self, forKey: .templateSource) ?? ""
        self.templatePath = try container.decodeIfPresent(String.self, forKey: .templatePath)
        self.templateProductID = try container.decodeIfPresent(String.self, forKey: .templateProductID)
        self.templateVersion = try container.decodeIfPresent(String.self, forKey: .templateVersion)
        self.templateSHA256 = try container.decodeIfPresent(String.self, forKey: .templateSHA256)
        self.innerSHA256 = try container.decodeIfPresent(String.self, forKey: .innerSHA256)
        self.innerSize = try container.decodeIfPresent(Int.self, forKey: .innerSize)
        self.keyID = try container.decodeIfPresent(String.self, forKey: .keyID)
        self.innerModel = try container.decodeIfPresent(Int.self, forKey: .innerModel)
        self.innerVersion = try container.decodeIfPresent(String.self, forKey: .innerVersion)
    }
}

struct FlashBankAppleFirmwareMatchPayload: Decodable, Equatable {
    let bank: String
    let match: FlashAppleFirmwareMatchPayload

    enum CodingKeys: String, CodingKey {
        case bank
        case match
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.bank = try container.decodeIfPresent(String.self, forKey: .bank) ?? ""
        self.match = try container.decodeIfPresent(FlashAppleFirmwareMatchPayload.self, forKey: .match)
            ?? FlashAppleFirmwareMatchPayload.empty
    }
}

extension FlashAppleFirmwareMatchPayload {
    static let empty = FlashAppleFirmwareMatchPayload(
        matched: false,
        templateSource: "",
        templatePath: nil,
        templateProductID: nil,
        templateVersion: nil,
        templateSHA256: nil,
        innerSHA256: nil,
        innerSize: nil,
        keyID: nil,
        innerModel: nil,
        innerVersion: nil
    )

    init(
        matched: Bool,
        templateSource: String,
        templatePath: String?,
        templateProductID: String?,
        templateVersion: String?,
        templateSHA256: String?,
        innerSHA256: String?,
        innerSize: Int?,
        keyID: String?,
        innerModel: Int?,
        innerVersion: String?
    ) {
        self.matched = matched
        self.templateSource = templateSource
        self.templatePath = templatePath
        self.templateProductID = templateProductID
        self.templateVersion = templateVersion
        self.templateSHA256 = templateSHA256
        self.innerSHA256 = innerSHA256
        self.innerSize = innerSize
        self.keyID = keyID
        self.innerModel = innerModel
        self.innerVersion = innerVersion
    }
}

struct FlashFirmwarePayload: Decodable, Equatable {
    let templateSource: String
    let templatePath: String?
    let templateProductID: String?
    let templateVersion: String?
    let templateSHA256: String?
    let payloadSHA256: String?
    let payloadSize: Int?
    let expectedPrefixSHA256: String?
    let expectedPrefixSize: Int?
    let expectedLoginClassification: String?
    let keyID: String?
    let innerModel: Int?
    let innerVersion: String?
    let innerPayloadSize: Int?

    enum CodingKeys: String, CodingKey {
        case templateSource = "template_source"
        case templatePath = "template_path"
        case templateProductID = "template_product_id"
        case templateVersion = "template_version"
        case templateSHA256 = "template_sha256"
        case payloadSHA256 = "payload_sha256"
        case payloadSize = "payload_size"
        case expectedPrefixSHA256 = "expected_prefix_sha256"
        case expectedPrefixSize = "expected_prefix_size"
        case expectedLoginClassification = "expected_login_classification"
        case keyID = "key_id"
        case innerModel = "inner_model"
        case innerVersion = "inner_version"
        case innerPayloadSize = "inner_payload_size"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.templateSource = try container.decodeIfPresent(String.self, forKey: .templateSource) ?? ""
        self.templatePath = try container.decodeIfPresent(String.self, forKey: .templatePath)
        self.templateProductID = try container.decodeIfPresent(String.self, forKey: .templateProductID)
        self.templateVersion = try container.decodeIfPresent(String.self, forKey: .templateVersion)
        self.templateSHA256 = try container.decodeIfPresent(String.self, forKey: .templateSHA256)
        self.payloadSHA256 = try container.decodeIfPresent(String.self, forKey: .payloadSHA256)
        self.payloadSize = try container.decodeIfPresent(Int.self, forKey: .payloadSize)
        self.expectedPrefixSHA256 = try container.decodeIfPresent(String.self, forKey: .expectedPrefixSHA256)
        self.expectedPrefixSize = try container.decodeIfPresent(Int.self, forKey: .expectedPrefixSize)
        self.expectedLoginClassification = try container.decodeIfPresent(String.self, forKey: .expectedLoginClassification)
        self.keyID = try container.decodeIfPresent(String.self, forKey: .keyID)
        self.innerModel = try container.decodeIfPresent(Int.self, forKey: .innerModel)
        self.innerVersion = try container.decodeIfPresent(String.self, forKey: .innerVersion)
        self.innerPayloadSize = try container.decodeIfPresent(Int.self, forKey: .innerPayloadSize)
    }
}

struct FlashPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let backupDir: String
    let mode: FlashPlanMode
    let writeRequested: Bool
    let alreadySatisfied: Bool
    let activeBank: String?
    let banks: [FlashBankPayload]
    let flashPlan: JSONValue?
    let appleFirmwareMatch: FlashAppleFirmwareMatchPayload?
    let appleFirmwareMatches: [FlashBankAppleFirmwareMatchPayload]
    let appleMatchStatus: String?
    let firmwarePayload: FlashFirmwarePayload?
    let firmwarePayloadPath: String?
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case backupDir = "backup_dir"
        case mode
        case writeRequested = "write_requested"
        case alreadySatisfied = "already_satisfied"
        case activeBank = "active_bank"
        case banks
        case flashPlan = "flash_plan"
        case appleFirmwareMatch = "apple_firmware_match"
        case appleFirmwareMatches = "apple_firmware_matches"
        case appleMatchStatus = "apple_match_status"
        case firmwarePayload = "firmware_payload"
        case firmwarePayloadPath = "firmware_payload_path"
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.backupDir = try container.decode(String.self, forKey: .backupDir)
        self.mode = try container.decodeIfPresent(FlashPlanMode.self, forKey: .mode) ?? .patch
        self.writeRequested = try container.decodeIfPresent(Bool.self, forKey: .writeRequested) ?? false
        self.alreadySatisfied = try container.decodeIfPresent(Bool.self, forKey: .alreadySatisfied) ?? false
        self.activeBank = try container.decodeIfPresent(String.self, forKey: .activeBank)
        self.banks = try container.decodeIfPresent([FlashBankPayload].self, forKey: .banks) ?? []
        self.flashPlan = try container.decodeIfPresent(JSONValue.self, forKey: .flashPlan)
        self.appleFirmwareMatch = try container.decodeIfPresent(FlashAppleFirmwareMatchPayload.self, forKey: .appleFirmwareMatch)
        self.appleFirmwareMatches = try container.decodeIfPresent([FlashBankAppleFirmwareMatchPayload].self, forKey: .appleFirmwareMatches) ?? []
        self.appleMatchStatus = try container.decodeIfPresent(String.self, forKey: .appleMatchStatus)
        self.firmwarePayload = try container.decodeIfPresent(FlashFirmwarePayload.self, forKey: .firmwarePayload)
        self.firmwarePayloadPath = try container.decodeIfPresent(String.self, forKey: .firmwarePayloadPath)
        self.summary = try container.decode(String.self, forKey: .summary)
    }
}

struct FlashWritePayload: Decodable, Equatable {
    let schemaVersion: Int
    let backupDir: String
    let mode: FlashPlanMode
    let writeStatus: String
    let writeValidated: Bool
    let writeOutcome: JSONValue?
    let writeResult: JSONValue?
    let writeMayHaveModifiedDevice: Bool
    let postWriteAction: String
    let rebootRequested: Bool
    let rebooted: Bool
    let waitedAfterReboot: Bool
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case backupDir = "backup_dir"
        case mode
        case writeStatus = "write_status"
        case writeValidated = "write_validated"
        case writeOutcome = "write_outcome"
        case writeResult = "write_result"
        case postWriteAction = "post_write_action"
        case rebootRequested = "reboot_requested"
        case rebooted
        case waitedAfterReboot = "waited_after_reboot"
        case summary
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        self.backupDir = try container.decode(String.self, forKey: .backupDir)
        self.mode = try container.decodeIfPresent(FlashPlanMode.self, forKey: .mode) ?? .patch
        self.writeStatus = try container.decodeIfPresent(String.self, forKey: .writeStatus) ?? ""
        self.writeValidated = try container.decodeIfPresent(Bool.self, forKey: .writeValidated) ?? false
        self.writeOutcome = try container.decodeIfPresent(JSONValue.self, forKey: .writeOutcome)
        self.writeResult = try container.decodeIfPresent(JSONValue.self, forKey: .writeResult)
        self.writeMayHaveModifiedDevice = Self.decodeWriteMayHaveModifiedDevice(from: writeOutcome)
        self.postWriteAction = try container.decodeIfPresent(String.self, forKey: .postWriteAction)
            ?? Self.stringValue(from: writeOutcome, key: "post_write_action")
            ?? ""
        self.rebootRequested = try container.decodeIfPresent(Bool.self, forKey: .rebootRequested)
            ?? Self.boolValue(from: writeOutcome, key: "reboot_requested")
            ?? false
        self.rebooted = try container.decodeIfPresent(Bool.self, forKey: .rebooted)
            ?? Self.boolValue(from: writeOutcome, key: "rebooted")
            ?? false
        self.waitedAfterReboot = try container.decodeIfPresent(Bool.self, forKey: .waitedAfterReboot)
            ?? Self.boolValue(from: writeOutcome, key: "waited_after_reboot")
            ?? false
        self.summary = try container.decode(String.self, forKey: .summary)
    }

    private static func decodeWriteMayHaveModifiedDevice(from value: JSONValue?) -> Bool {
        boolValue(from: value, key: "write_may_have_modified_device") ?? false
    }

    private static func stringValue(from value: JSONValue?, key: String) -> String? {
        guard let value, case .object(let values) = value, case .string(let string)? = values[key] else {
            return nil
        }
        return string
    }

    private static func boolValue(from value: JSONValue?, key: String) -> Bool? {
        guard let value, case .object(let values) = value else {
            return nil
        }
        guard case .bool(let boolValue)? = values[key] else {
            return nil
        }
        return boolValue
    }
}

struct MaintenanceResultPayload: Decodable, Equatable {
    let schemaVersion: Int
    let summary: String
    let message: String?
    let requiresReboot: Bool?
    let rebooted: Bool?
    let rebootRequested: Bool?
    let waited: Bool?
    let verified: Bool?
    let returncode: Int?
    let counts: [String: Int]?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case summary
        case message
        case requiresReboot = "requires_reboot"
        case rebooted
        case rebootRequested = "reboot_requested"
        case waited
        case verified
        case returncode
        case counts
    }
}

struct PlannedCheckPayload: Decodable, Equatable {
    let id: String
    let description: String
}

struct BackendRecoveryPayload: Decodable, Equatable {
    let title: String
    let message: String?
    let actions: [String]
    let actionIDs: [String]
    let retryable: Bool
    let suggestedOperation: String?
    let docsAnchor: String?
    let localizationKey: String?
    let localizationValues: [String: String]

    enum CodingKeys: String, CodingKey {
        case title
        case message
        case actions
        case actionIDs = "action_ids"
        case retryable
        case suggestedOperation = "suggested_operation"
        case docsAnchor = "docs_anchor"
        case localizationKey = "localization_key"
        case localizationValues = "localization_values"
    }

    init(
        title: String,
        message: String?,
        actions: [String],
        actionIDs: [String],
        retryable: Bool,
        suggestedOperation: String?,
        docsAnchor: String?,
        localizationKey: String? = nil,
        localizationValues: [String: String] = [:]
    ) {
        self.title = title
        self.message = message
        self.actions = actions
        self.actionIDs = actionIDs
        self.retryable = retryable
        self.suggestedOperation = suggestedOperation
        self.docsAnchor = docsAnchor
        self.localizationKey = localizationKey
        self.localizationValues = localizationValues
    }

    init(_ snapshot: DeviceRecoverySnapshot) {
        self.init(
            title: snapshot.title,
            message: snapshot.message,
            actions: snapshot.actions,
            actionIDs: snapshot.actionIDs,
            retryable: snapshot.retryable,
            suggestedOperation: snapshot.suggestedOperation,
            docsAnchor: snapshot.docsAnchor,
            localizationKey: snapshot.localizationKey
        )
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.title = try container.decode(String.self, forKey: .title)
        self.message = try container.decodeIfPresent(String.self, forKey: .message)
        self.actions = try container.decodeIfPresent([String].self, forKey: .actions) ?? []
        self.actionIDs = try container.decodeIfPresent([String].self, forKey: .actionIDs) ?? []
        self.retryable = try container.decode(Bool.self, forKey: .retryable)
        self.suggestedOperation = try container.decodeIfPresent(String.self, forKey: .suggestedOperation)
        self.docsAnchor = try container.decodeIfPresent(String.self, forKey: .docsAnchor)
        self.localizationKey = try container.decodeIfPresent(String.self, forKey: .localizationKey)
        self.localizationValues = try container.decodeIfPresent([String: String].self, forKey: .localizationValues) ?? [:]
    }
}

extension BackendRecoveryPayload {
    var hasGuidanceText: Bool {
        let titleText = title.normalizedRecoveryText
        if let message, !message.normalizedRecoveryText.isEmpty, message.normalizedRecoveryText != titleText {
            return true
        }
        return actions.contains { !$0.normalizedRecoveryText.isEmpty }
    }
}

private extension String {
    var normalizedRecoveryText: String {
        trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }
}
