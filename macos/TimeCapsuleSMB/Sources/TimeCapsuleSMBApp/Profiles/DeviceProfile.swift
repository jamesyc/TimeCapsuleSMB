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
}

struct DeviceDeploySnapshot: Codable, Equatable {
    var deployedAt: Date
    var state: DeployWorkflowState
    var payloadFamily: String?
    var rebootRequested: Bool?
    var verified: Bool?
    var summary: String
}

struct DeviceProfile: Codable, Equatable, Identifiable {
    typealias ID = String

    var id: ID
    var displayName: String
    var host: String
    var bonjourName: String?
    var bonjourFullname: String?
    var hostname: String?
    var addresses: [String]
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
    var lastDeploy: DeviceDeploySnapshot?
    var settings: DeviceProfileSettings
    var passwordState: DevicePasswordState

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
        return normalizedHost.isEmpty ? "Time Capsule" : normalizedHost
    }

    var normalizedHost: String {
        Self.normalizedHost(host)
    }

    var runtimeContext: DeviceRuntimeContext {
        DeviceRuntimeContext(profileID: id, configURL: URL(fileURLWithPath: configPath))
    }

    static func configURL(for id: ID, applicationSupportURL: URL) -> URL {
        applicationSupportURL
            .appendingPathComponent("Devices", isDirectory: true)
            .appendingPathComponent(id, isDirectory: true)
            .appendingPathComponent(".env")
    }

    static func normalizedHost(_ host: String) -> String {
        let trimmed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        let withoutUser = trimmed.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false).last.map(String.init) ?? trimmed
        return withoutUser
            .trimmingCharacters(in: CharacterSet(charactersIn: "."))
            .lowercased()
    }

    static func matches(_ left: DeviceProfile, _ right: DeviceProfile) -> Bool {
        if let leftFullname = normalizedOptional(left.bonjourFullname),
           let rightFullname = normalizedOptional(right.bonjourFullname),
           leftFullname == rightFullname {
            return true
        }
        let leftHost = left.normalizedHost
        let rightHost = right.normalizedHost
        return !leftHost.isEmpty && leftHost == rightHost
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
            host: configuredDevice.host,
            bonjourName: discoveredDevice?.name ?? existing?.bonjourName,
            bonjourFullname: discoveredDevice?.fullname ?? existing?.bonjourFullname,
            hostname: discoveredDevice?.hostname ?? existing?.hostname,
            addresses: discoveredDevice?.addresses ?? existing?.addresses ?? [],
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
            lastDeploy: existing?.lastDeploy,
            settings: existing?.settings ?? .default,
            passwordState: existing?.passwordState ?? .unknown
        )
    }

    private static func normalizedOptional(_ value: String?) -> String? {
        guard let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
              !normalized.isEmpty else {
            return nil
        }
        return normalized
    }
}

extension DiscoveredDevice {
    var fullname: String? {
        guard case .object(let object) = rawRecord,
              case .string(let value)? = object["fullname"] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
