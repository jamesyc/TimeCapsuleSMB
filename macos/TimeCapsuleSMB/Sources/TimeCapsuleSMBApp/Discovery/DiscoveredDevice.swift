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

    init(
        id: String,
        name: String,
        connectionTarget: String,
        sshHost: String?,
        hostname: String,
        networkAddresses: [DeviceNetworkAddress],
        syap: String?,
        model: String?,
        rawRecord: JSONValue
    ) {
        self.id = id
        self.name = name
        self.connectionTarget = connectionTarget
        self.sshHost = sshHost
        self.hostname = hostname
        self.networkAddresses = networkAddresses
        self.syap = syap
        self.model = model
        self.rawRecord = rawRecord
    }

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

    var fullname: String? {
        guard case .object(let object) = rawRecord,
              case .string(let value)? = object["fullname"] else {
            return nil
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
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
