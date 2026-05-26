import Foundation

struct DiscoveredDevice: Identifiable, Equatable {
    let id: String
    let name: String
    let connectionTarget: String
    let sshHost: String?
    let hostname: String
    let networkAddresses: [DeviceNetworkAddress]
    let syap: String?
    let model: String?
    let rawRecord: JSONValue

    var host: String { connectionTarget }
    var addresses: [String] { networkAddresses.map(\.value) }
    var addressSummary: String { DeviceEndpointPolicy.addressSummary(networkAddresses) }

    init(payload: DiscoveredDevicePayload, index: Int) {
        let addresses = Self.networkAddresses(ipv4: payload.ipv4, ipv6: payload.ipv6, fallback: payload.addresses)
        let sshHost = Self.nonEmpty(payload.sshHost)
        let backendTarget = sshHost.flatMap(DeviceEndpointPolicy.hostComponent)
            ?? DeviceEndpointPolicy.hostComponent(payload.host)
        let identity = DeviceNetworkIdentity(
            configuredSSHTarget: backendTarget ?? "",
            hostname: payload.hostname,
            bonjourName: payload.name,
            bonjourFullname: payload.fullname,
            addresses: addresses
        )

        self.id = payload.id.isEmpty ? "discovered-\(index)" : payload.id
        self.name = payload.name.isEmpty ? (payload.hostname.isEmpty ? "AirPort Device" : payload.hostname) : payload.name
        self.connectionTarget = backendTarget ?? identity.preferredSetupTarget
        self.sshHost = sshHost
        self.hostname = payload.hostname
        self.networkAddresses = identity.addresses
        self.syap = Self.nonEmpty(payload.syap)
        self.model = Self.nonEmpty(payload.model) ?? Self.recordProperty(payload.selectedRecord, keys: ["model", "am"])
        self.rawRecord = payload.selectedRecord
    }

    init(record: BonjourResolvedServicePayload, index: Int) {
        let stableParts = [
            record.fullname,
            record.serviceType,
            record.name,
            record.hostname,
            record.ipv4.joined(separator: ","),
            record.ipv6.joined(separator: ",")
        ]
        let stableID = stableParts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: "|")

        let resolvedName = record.name.isEmpty ? (record.hostname.isEmpty ? "AirPort Device" : record.hostname) : record.name
        self.id = stableID.isEmpty ? "discovered-\(index)" : stableID
        self.name = resolvedName
        self.hostname = record.hostname
        let addresses = Self.networkAddresses(ipv4: record.ipv4, ipv6: record.ipv6, fallback: [])
        self.networkAddresses = addresses
        let identity = DeviceNetworkIdentity(
            configuredSSHTarget: "",
            hostname: record.hostname,
            bonjourName: resolvedName,
            bonjourFullname: record.fullname,
            addresses: addresses
        )
        self.connectionTarget = identity.preferredSetupTarget
        self.sshHost = DeviceEndpointPolicy.rootSSHTarget(connectionTarget)
        self.syap = Self.nonEmpty(record.properties["syAP"] ?? record.properties["syap"])
        self.model = Self.nonEmpty(record.properties["model"] ?? record.properties["am"])
        self.rawRecord = record.jsonValue
    }

    var discoveryModelText: String {
        Self.nonEmpty(model) ?? ""
    }

    private static func recordProperty(_ record: JSONValue, keys: [String]) -> String? {
        guard case .object(let values) = record, case .object(let properties)? = values["properties"] else {
            return nil
        }
        for key in keys {
            if case .string(let value)? = properties[key], let trimmed = nonEmpty(value) {
                return trimmed
            }
        }
        return nil
    }

    private static func nonEmpty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return nil
        }
        return trimmed
    }

    private static func networkAddresses(ipv4: [String], ipv6: [String], fallback: [String]) -> [DeviceNetworkAddress] {
        var addresses = ipv4.compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }
        addresses += ipv6.compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }
        if addresses.isEmpty {
            addresses = fallback.compactMap { DeviceNetworkAddress(value: $0, source: .bonjour) }
        }
        return DeviceEndpointPolicy.uniqueAddresses(addresses)
    }
}

struct ConfiguredDeviceState: Equatable {
    let host: String
    let configPath: String
    let configureId: String
    let sshAuthenticated: Bool
    let syap: String?
    let model: String?
    let compatibility: DeviceCompatibilityPayload?

    init(payload: ConfigurePayload) {
        self.host = payload.host
        self.configPath = payload.configPath
        self.configureId = payload.configureId
        self.sshAuthenticated = payload.sshAuthenticated
        self.syap = payload.deviceSyap ?? payload.device?.syap
        self.model = payload.deviceModel ?? payload.device?.model
        self.compatibility = payload.compatibility
    }
}
