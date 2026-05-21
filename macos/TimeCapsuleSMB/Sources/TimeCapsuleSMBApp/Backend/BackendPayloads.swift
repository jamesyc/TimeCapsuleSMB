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

struct PathsPayload: Decodable, Equatable {
    let schemaVersion: Int
    let distributionRoot: String
    let configPath: String
    let stateDir: String
    let packageRoot: String
    let artifactManifest: String
    let artifacts: [ArtifactPayload]
    let counts: [String: Int]
    let summary: String

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case distributionRoot = "distribution_root"
        case configPath = "config_path"
        case stateDir = "state_dir"
        case packageRoot = "package_root"
        case artifactManifest = "artifact_manifest"
        case artifacts
        case counts
        case summary
    }
}

struct ArtifactPayload: Decodable, Equatable {
    let name: String
    let repoRelativePath: String
    let absolutePath: String
    let sha256: String
    let ok: Bool
    let message: String

    enum CodingKeys: String, CodingKey {
        case name
        case repoRelativePath = "repo_relative_path"
        case absolutePath = "absolute_path"
        case sha256
        case ok
        case message
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

struct DeployPlanPayload: Decodable, Equatable {
    let schemaVersion: Int
    let host: String
    let volumeRoot: String?
    let payloadDir: String
    let payloadFamily: String?
    let netbsd4: Bool
    let requiresReboot: Bool
    let rebootRequired: Bool?
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

    enum CodingKeys: String, CodingKey {
        case title
        case message
        case actions
        case actionIDs = "action_ids"
        case retryable
        case suggestedOperation = "suggested_operation"
        case docsAnchor = "docs_anchor"
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
    }
}
