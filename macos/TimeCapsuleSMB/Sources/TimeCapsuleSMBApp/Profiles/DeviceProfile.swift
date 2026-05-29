import Foundation

public struct DeviceRuntimeContext: Equatable, Sendable {
    public let profileID: String
    public let configURL: URL

    public init(profileID: String, configURL: URL) {
        self.profileID = profileID
        self.configURL = configURL
    }
}

enum DevicePasswordState: String, Codable, CaseIterable, Equatable {
    case unknown
    case available
    case missing
    case invalid
    case keychainUnavailable

    var title: String {
        switch self {
        case .unknown:
            return L10n.string("password_state.unknown")
        case .available:
            return L10n.string("password_state.available")
        case .missing:
            return L10n.string("password_state.missing")
        case .invalid:
            return L10n.string("password_state.invalid")
        case .keychainUnavailable:
            return L10n.string("password_state.keychain_unavailable")
        }
    }
}

struct DeviceProfileSettings: Codable, Equatable {
    var nbnsEnabled: Bool
    var internalShareUseDiskRoot: Bool
    var anyProtocol: Bool
    var debugLogging: Bool
    var mountWaitSeconds: Int
    var ataIdleSeconds: Int
    var ataStandby: Int?

    static let `default` = DeviceProfileSettings(
        nbnsEnabled: true,
        internalShareUseDiskRoot: false,
        anyProtocol: false,
        debugLogging: false,
        mountWaitSeconds: 30,
        ataIdleSeconds: 300,
        ataStandby: nil
    )

    init(
        nbnsEnabled: Bool,
        internalShareUseDiskRoot: Bool = false,
        anyProtocol: Bool = false,
        debugLogging: Bool,
        mountWaitSeconds: Int,
        ataIdleSeconds: Int = 300,
        ataStandby: Int? = nil
    ) {
        self.nbnsEnabled = nbnsEnabled
        self.internalShareUseDiskRoot = internalShareUseDiskRoot
        self.anyProtocol = anyProtocol
        self.debugLogging = debugLogging
        self.mountWaitSeconds = mountWaitSeconds
        self.ataIdleSeconds = ataIdleSeconds
        self.ataStandby = ataStandby
    }

    private enum CodingKeys: String, CodingKey {
        case nbnsEnabled
        case internalShareUseDiskRoot
        case anyProtocol
        case debugLogging
        case mountWaitSeconds
        case ataIdleSeconds
        case ataStandby
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        nbnsEnabled = try container.decodeIfPresent(Bool.self, forKey: .nbnsEnabled) ?? Self.default.nbnsEnabled
        internalShareUseDiskRoot = try container.decodeIfPresent(Bool.self, forKey: .internalShareUseDiskRoot) ?? Self.default.internalShareUseDiskRoot
        anyProtocol = try container.decodeIfPresent(Bool.self, forKey: .anyProtocol) ?? Self.default.anyProtocol
        debugLogging = try container.decodeIfPresent(Bool.self, forKey: .debugLogging) ?? Self.default.debugLogging
        mountWaitSeconds = try container.decodeIfPresent(Int.self, forKey: .mountWaitSeconds) ?? Self.default.mountWaitSeconds
        ataIdleSeconds = Self.decodeNonNegativeInteger(
            from: container,
            forKey: .ataIdleSeconds,
            defaultValue: Self.default.ataIdleSeconds
        )
        ataStandby = Self.decodeOptionalNonNegativeInteger(from: container, forKey: .ataStandby)
    }

    private static func decodeNonNegativeInteger(
        from container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys,
        defaultValue: Int
    ) -> Int {
        if let value = try? container.decodeIfPresent(Int.self, forKey: key), value >= 0 {
            return value
        }
        if let text = try? container.decodeIfPresent(String.self, forKey: key),
           let parsed = ValueParsers.nonNegativeInteger(text) {
            return parsed
        }
        return defaultValue
    }

    private static func decodeOptionalNonNegativeInteger(
        from container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys
    ) -> Int? {
        if let value = try? container.decodeIfPresent(Int.self, forKey: key), value >= 0 {
            return value
        }
        if let text = try? container.decodeIfPresent(String.self, forKey: key) {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else {
                return nil
            }
            return ValueParsers.nonNegativeInteger(trimmed)
        }
        return nil
    }
}

struct DeviceCheckupSnapshot: Codable, Equatable {
    var checkedAt: Date
    var state: DoctorWorkflowState
    var passCount: Int
    var warnCount: Int
    var failCount: Int
    var summary: String

    var localizedSummary: String {
        L10n.format("summary.checkup_counts", passCount, warnCount, failCount)
    }
}

struct DeviceRecoverySnapshot: Codable, Equatable {
    var title: String
    var message: String?
    var actions: [String]
    var actionIDs: [String]
    var retryable: Bool
    var suggestedOperation: String?
    var docsAnchor: String?

    init(
        title: String,
        message: String?,
        actions: [String],
        actionIDs: [String],
        retryable: Bool,
        suggestedOperation: String?,
        docsAnchor: String?
    ) {
        self.title = title
        self.message = message
        self.actions = actions
        self.actionIDs = actionIDs
        self.retryable = retryable
        self.suggestedOperation = suggestedOperation
        self.docsAnchor = docsAnchor
    }

    init(_ recovery: BackendRecoveryPayload) {
        self.init(
            title: recovery.title,
            message: recovery.message,
            actions: recovery.actions,
            actionIDs: recovery.actionIDs,
            retryable: recovery.retryable,
            suggestedOperation: recovery.suggestedOperation,
            docsAnchor: recovery.docsAnchor
        )
    }
}

enum DeviceDeployStateStatus: String, Codable, Equatable, CaseIterable {
    case deploying
    case awaitingConfirmation
    case succeeded
    case failed
    case interrupted

    var isInProgress: Bool {
        switch self {
        case .deploying, .awaitingConfirmation:
            return true
        case .succeeded, .failed, .interrupted:
            return false
        }
    }

    var isFailure: Bool {
        switch self {
        case .failed, .interrupted:
            return true
        case .deploying, .awaitingConfirmation, .succeeded:
            return false
        }
    }
}

struct DeviceDeployStateSnapshot: Codable, Equatable {
    var operationID: String?
    var startedAt: Date
    var updatedAt: Date
    var finishedAt: Date?
    var status: DeviceDeployStateStatus
    var stage: String?
    var payloadFamily: String?
    var rebootRequested: Bool?
    var verified: Bool?
    var summary: String
    var errorCode: String?
    var errorMessage: String?
    var recovery: DeviceRecoverySnapshot?

    var localizedSummary: String {
        switch status {
        case .succeeded:
            let trimmed = summary.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                return BackendSummaryLocalization.localized(trimmed, operation: "deploy")
            }
            return L10n.string("deploy.result.default_message")
        case .failed:
            let trimmed = (errorMessage ?? summary).trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? L10n.string("install.state.deploy_failed") : trimmed
        case .interrupted:
            let trimmed = (errorMessage ?? summary).trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? L10n.string("install.state.deploy_interrupted") : trimmed
        case .deploying:
            return L10n.string("install.state.deploying")
        case .awaitingConfirmation:
            return L10n.string("install.state.awaiting_confirmation")
        }
    }

    init(
        operationID: String?,
        startedAt: Date,
        updatedAt: Date,
        finishedAt: Date?,
        status: DeviceDeployStateStatus,
        stage: String?,
        payloadFamily: String?,
        rebootRequested: Bool?,
        verified: Bool?,
        summary: String,
        errorCode: String?,
        errorMessage: String?,
        recovery: DeviceRecoverySnapshot?
    ) {
        self.operationID = operationID
        self.startedAt = startedAt
        self.updatedAt = updatedAt
        self.finishedAt = finishedAt
        self.status = status
        self.stage = stage
        self.payloadFamily = payloadFamily
        self.rebootRequested = rebootRequested
        self.verified = verified
        self.summary = summary
        self.errorCode = errorCode
        self.errorMessage = errorMessage
        self.recovery = recovery
    }
}

enum DeviceRuntimeState: String, Codable, Equatable, CaseIterable {
    case unknown
    case installing
    case installedUnverified
    case installedVerified
    case installFailed
    case installInterrupted
    case activationNeeded
    case unhealthy

    var isInstalled: Bool {
        switch self {
        case .installedUnverified, .installedVerified, .activationNeeded:
            return true
        case .unknown, .installing, .installFailed, .installInterrupted, .unhealthy:
            return false
        }
    }

    var isFailure: Bool {
        switch self {
        case .installFailed, .installInterrupted, .unhealthy:
            return true
        case .unknown, .installing, .installedUnverified, .installedVerified, .activationNeeded:
            return false
        }
    }
}

enum DeviceRuntimeEvidenceSource: String, Codable, Equatable, CaseIterable {
    case deploy
    case doctor
    case appRecovery
}

struct DeviceRuntimeStateSnapshot: Codable, Equatable {
    var state: DeviceRuntimeState
    var source: DeviceRuntimeEvidenceSource
    var stage: String?
    var payloadFamily: String?
    var verified: Bool?
    var summary: String
    var errorCode: String?
    var errorMessage: String?
    var recovery: DeviceRecoverySnapshot?

    var localizedSummary: String {
        switch state {
        case .unknown:
            return L10n.string("runtime.state.unknown")
        case .installing:
            return L10n.string("install.state.deploying")
        case .installedVerified:
            let trimmed = summary.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                return source == .doctor
                    ? BackendSummaryLocalization.localized(trimmed, operation: "doctor")
                    : BackendSummaryLocalization.localized(trimmed, operation: "deploy")
            }
            return source == .doctor
                ? L10n.string("summary.install_verified_by_checkup")
                : L10n.string("deploy.result.default_message")
        case .installedUnverified:
            let trimmed = summary.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty
                ? L10n.string("deploy.result.default_message")
                : BackendSummaryLocalization.localized(trimmed, operation: "deploy")
        case .installFailed:
            let trimmed = (errorMessage ?? summary).trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? L10n.string("install.state.deploy_failed") : trimmed
        case .installInterrupted:
            let trimmed = (errorMessage ?? summary).trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? L10n.string("install.state.deploy_interrupted") : trimmed
        case .activationNeeded:
            return L10n.string("dashboard.health.runtime.activation_needed")
        case .unhealthy:
            let trimmed = (errorMessage ?? summary).trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? L10n.string("runtime.state.unhealthy") : trimmed
        }
    }

    init(
        state: DeviceRuntimeState,
        source: DeviceRuntimeEvidenceSource,
        stage: String?,
        payloadFamily: String?,
        verified: Bool?,
        summary: String,
        errorCode: String?,
        errorMessage: String?,
        recovery: DeviceRecoverySnapshot?
    ) {
        self.state = state
        self.source = source
        self.stage = stage
        self.payloadFamily = payloadFamily
        self.verified = verified
        self.summary = summary
        self.errorCode = errorCode
        self.errorMessage = errorMessage
        self.recovery = recovery
    }
}

struct DeviceProfile: Codable, Equatable, Identifiable {
    typealias ID = String

    var id: ID
    var displayName: String
    var network: DeviceNetworkIdentity
    var syap: String?
    var model: String?
    var osName: String?
    var osRelease: String?
    var arch: String?
    var elfEndianness: String?
    var payloadFamily: String?
    var deviceGeneration: String?
    var configPath: String
    var keychainAccount: String
    var createdAt: Date
    var updatedAt: Date
    var lastCheckup: DeviceCheckupSnapshot?
    var lastDeployState: DeviceDeployStateSnapshot?
    var runtimeState: DeviceRuntimeStateSnapshot?
    var settings: DeviceProfileSettings
    var passwordState: DevicePasswordState

    var host: String {
        get { network.configuredSSHTarget }
        set { network.setConfiguredSSHTarget(newValue) }
    }

    var bonjourName: String? {
        get { network.bonjourName }
        set { network.bonjourName = newValue }
    }

    var bonjourFullname: String? {
        get { network.bonjourFullname }
        set { network.bonjourFullname = newValue }
    }

    var hostname: String? {
        get { network.hostname }
        set { network.hostname = newValue }
    }

    var addresses: [String] {
        get { network.addressValues }
        set { network.setAddressValues(newValue) }
    }

    var connectionTarget: String {
        network.preferredSetupTarget
    }

    var displayTarget: String {
        network.displayTarget
    }

    var addressSummary: String {
        network.addressSummary
    }

    var title: String {
        let trimmedName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedName.isEmpty {
            return trimmedName
        }
        if let bonjourName = bonjourName?.trimmingCharacters(in: .whitespacesAndNewlines), !bonjourName.isEmpty {
            return bonjourName
        }
        if let model = model?.trimmingCharacters(in: .whitespacesAndNewlines), !model.isEmpty {
            return model
        }
        return displayTarget.isEmpty ? "Time Capsule" : displayTarget
    }

    var normalizedHost: String {
        Self.normalizedHost(host)
    }

    var runtimeContext: DeviceRuntimeContext {
        DeviceRuntimeContext(profileID: id, configURL: URL(fileURLWithPath: configPath))
    }

    var configURL: URL {
        URL(fileURLWithPath: configPath)
    }

    static func configURL(for id: ID, applicationSupportURL: URL) -> URL {
        applicationSupportURL
            .appendingPathComponent("Devices", isDirectory: true)
            .appendingPathComponent(id, isDirectory: true)
            .appendingPathComponent(".env")
    }

    static func normalizedHost(_ host: String) -> String {
        DeviceEndpointPolicy.normalizedHostKey(host)
    }

    static func matches(_ left: DeviceProfile, _ right: DeviceProfile) -> Bool {
        left.network.matches(right.network)
    }

    static func make(
        id: ID = UUID().uuidString.lowercased(),
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        applicationSupportURL: URL,
        existing: DeviceProfile? = nil,
        date: Date = Date()
    ) -> DeviceProfile {
        let resolvedID = existing?.id ?? id
        let compatibility = configuredDevice.compatibility
        return DeviceProfile(
            id: resolvedID,
            displayName: existing?.displayName ?? discoveredDevice?.name ?? configuredDevice.model ?? "Time Capsule",
            network: DeviceNetworkIdentity.make(
                configuredSSHTarget: configuredDevice.host,
                discoveredDevice: discoveredDevice,
                existing: existing?.network
            ),
            syap: configuredDevice.syap ?? existing?.syap,
            model: configuredDevice.model ?? existing?.model,
            osName: compatibility?.osName ?? existing?.osName,
            osRelease: compatibility?.osRelease ?? existing?.osRelease,
            arch: compatibility?.arch ?? existing?.arch,
            elfEndianness: compatibility?.elfEndianness ?? existing?.elfEndianness,
            payloadFamily: compatibility?.payloadFamily ?? existing?.payloadFamily,
            deviceGeneration: compatibility?.deviceGeneration ?? existing?.deviceGeneration,
            configPath: Self.configURL(for: resolvedID, applicationSupportURL: applicationSupportURL).path,
            keychainAccount: resolvedID,
            createdAt: existing?.createdAt ?? date,
            updatedAt: date,
            lastCheckup: existing?.lastCheckup,
            lastDeployState: existing?.lastDeployState,
            runtimeState: existing?.runtimeState,
            settings: existing?.settings ?? .default,
            passwordState: existing?.passwordState ?? .unknown
        )
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case displayName
        case network
        case syap
        case model
        case osName
        case osRelease
        case arch
        case elfEndianness
        case payloadFamily
        case deviceGeneration
        case configPath
        case keychainAccount
        case createdAt
        case updatedAt
        case lastCheckup
        case lastDeployState
        case runtimeState
        case settings
        case passwordState
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(ID.self, forKey: .id)
        displayName = try container.decode(String.self, forKey: .displayName)
        network = try container.decode(DeviceNetworkIdentity.self, forKey: .network)
        syap = try container.decodeIfPresent(String.self, forKey: .syap)
        model = try container.decodeIfPresent(String.self, forKey: .model)
        osName = try container.decodeIfPresent(String.self, forKey: .osName)
        osRelease = try container.decodeIfPresent(String.self, forKey: .osRelease)
        arch = try container.decodeIfPresent(String.self, forKey: .arch)
        elfEndianness = try container.decodeIfPresent(String.self, forKey: .elfEndianness)
        payloadFamily = try container.decodeIfPresent(String.self, forKey: .payloadFamily)
        deviceGeneration = try container.decodeIfPresent(String.self, forKey: .deviceGeneration)
        configPath = try container.decodeIfPresent(String.self, forKey: .configPath) ?? ""
        keychainAccount = try container.decodeIfPresent(String.self, forKey: .keychainAccount) ?? id
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
        lastCheckup = try container.decodeIfPresent(DeviceCheckupSnapshot.self, forKey: .lastCheckup)
        lastDeployState = try container.decodeIfPresent(DeviceDeployStateSnapshot.self, forKey: .lastDeployState)
        runtimeState = try container.decodeIfPresent(DeviceRuntimeStateSnapshot.self, forKey: .runtimeState)
        settings = try container.decodeIfPresent(DeviceProfileSettings.self, forKey: .settings) ?? .default
        passwordState = try container.decodeIfPresent(DevicePasswordState.self, forKey: .passwordState) ?? .unknown
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(displayName, forKey: .displayName)
        try container.encode(network, forKey: .network)
        try container.encodeIfPresent(syap, forKey: .syap)
        try container.encodeIfPresent(model, forKey: .model)
        try container.encodeIfPresent(osName, forKey: .osName)
        try container.encodeIfPresent(osRelease, forKey: .osRelease)
        try container.encodeIfPresent(arch, forKey: .arch)
        try container.encodeIfPresent(elfEndianness, forKey: .elfEndianness)
        try container.encodeIfPresent(payloadFamily, forKey: .payloadFamily)
        try container.encodeIfPresent(deviceGeneration, forKey: .deviceGeneration)
        try container.encode(createdAt, forKey: .createdAt)
        try container.encode(updatedAt, forKey: .updatedAt)
        try container.encodeIfPresent(lastCheckup, forKey: .lastCheckup)
        try container.encodeIfPresent(lastDeployState, forKey: .lastDeployState)
        try container.encodeIfPresent(runtimeState, forKey: .runtimeState)
        try container.encode(settings, forKey: .settings)
        try container.encode(passwordState, forKey: .passwordState)
    }

    init(
        id: ID,
        displayName: String,
        network: DeviceNetworkIdentity,
        syap: String?,
        model: String?,
        osName: String?,
        osRelease: String?,
        arch: String?,
        elfEndianness: String?,
        payloadFamily: String?,
        deviceGeneration: String?,
        configPath: String,
        keychainAccount: String,
        createdAt: Date,
        updatedAt: Date,
        lastCheckup: DeviceCheckupSnapshot?,
        lastDeployState: DeviceDeployStateSnapshot? = nil,
        runtimeState: DeviceRuntimeStateSnapshot? = nil,
        settings: DeviceProfileSettings,
        passwordState: DevicePasswordState
    ) {
        self.id = id
        self.displayName = displayName
        self.network = network
        self.syap = syap
        self.model = model
        self.osName = osName
        self.osRelease = osRelease
        self.arch = arch
        self.elfEndianness = elfEndianness
        self.payloadFamily = payloadFamily
        self.deviceGeneration = deviceGeneration
        self.configPath = configPath
        self.keychainAccount = keychainAccount
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.lastCheckup = lastCheckup
        self.lastDeployState = lastDeployState
        self.runtimeState = runtimeState
        self.settings = settings
        self.passwordState = passwordState
    }

    init(
        id: ID,
        displayName: String,
        host: String,
        bonjourName: String?,
        bonjourFullname: String?,
        hostname: String?,
        addresses: [String],
        syap: String?,
        model: String?,
        osName: String?,
        osRelease: String?,
        arch: String?,
        elfEndianness: String?,
        payloadFamily: String?,
        deviceGeneration: String?,
        configPath: String,
        keychainAccount: String,
        createdAt: Date,
        updatedAt: Date,
        lastCheckup: DeviceCheckupSnapshot?,
        lastDeployState: DeviceDeployStateSnapshot? = nil,
        runtimeState: DeviceRuntimeStateSnapshot? = nil,
        settings: DeviceProfileSettings,
        passwordState: DevicePasswordState
    ) {
        self.init(
            id: id,
            displayName: displayName,
            network: DeviceNetworkIdentity(
                configuredSSHTarget: host,
                hostname: hostname,
                bonjourName: bonjourName,
                bonjourFullname: bonjourFullname,
                addresses: addresses.compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }
            ),
            syap: syap,
            model: model,
            osName: osName,
            osRelease: osRelease,
            arch: arch,
            elfEndianness: elfEndianness,
            payloadFamily: payloadFamily,
            deviceGeneration: deviceGeneration,
            configPath: configPath,
            keychainAccount: keychainAccount,
            createdAt: createdAt,
            updatedAt: updatedAt,
            lastCheckup: lastCheckup,
            lastDeployState: lastDeployState,
            runtimeState: runtimeState,
            settings: settings,
            passwordState: passwordState
        )
    }

}
