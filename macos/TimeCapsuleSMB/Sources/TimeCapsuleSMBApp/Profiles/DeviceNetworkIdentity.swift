import Foundation

enum NetworkAddressFamily: String, Codable, Equatable {
    case ipv4
    case ipv6

    var title: String {
        switch self {
        case .ipv4:
            return "IPv4"
        case .ipv6:
            return "IPv6"
        }
    }
}

enum NetworkAddressScope: String, Codable, Equatable {
    case regular
    case linkLocal
    case loopback
}

enum DeviceAddressSource: String, Codable, Equatable {
    case bonjour
    case configured
    case manual
}

struct DeviceNetworkAddress: Codable, Equatable, Identifiable {
    var id: String { identityKey }

    var value: String
    var family: NetworkAddressFamily
    var scope: NetworkAddressScope
    var source: DeviceAddressSource

    var normalizedValue: String {
        DeviceEndpointPolicy.normalizedAddressValue(value, family: family)
    }

    var identityKey: String {
        "\(family.rawValue):\(normalizedValue)"
    }

    init?(value: String, source: DeviceAddressSource) {
        guard let host = DeviceEndpointPolicy.hostComponent(value),
              let family = DeviceEndpointPolicy.addressFamily(for: host) else {
            return nil
        }
        self.value = DeviceEndpointPolicy.normalizedAddressValue(host, family: family)
        self.family = family
        self.scope = DeviceEndpointPolicy.addressScope(value: self.value, family: family)
        self.source = source
    }
}

struct DeviceNetworkIdentity: Codable, Equatable {
    var configuredSSHTarget: String
    var hostname: String?
    var bonjourName: String?
    var bonjourFullname: String?
    var addresses: [DeviceNetworkAddress]

    init(
        configuredSSHTarget: String,
        hostname: String? = nil,
        bonjourName: String? = nil,
        bonjourFullname: String? = nil,
        addresses: [DeviceNetworkAddress] = []
    ) {
        self.configuredSSHTarget = configuredSSHTarget
        self.hostname = Self.normalizedOptional(hostname)
        self.bonjourName = Self.normalizedOptional(bonjourName)
        self.bonjourFullname = Self.normalizedOptional(bonjourFullname)
        self.addresses = DeviceEndpointPolicy.uniqueAddresses(addresses)
        appendConfiguredTargetAddress()
    }

    var configuredHost: String {
        DeviceEndpointPolicy.hostComponent(configuredSSHTarget)
            ?? configuredSSHTarget.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var normalizedConfiguredHost: String {
        DeviceEndpointPolicy.normalizedHostKey(configuredSSHTarget)
    }

    var preferredSetupTarget: String {
        DeviceEndpointPolicy.preferredSetupTarget(for: self) ?? configuredHost
    }

    var displayTarget: String {
        DeviceEndpointPolicy.displayTarget(for: self)
    }

    var addressValues: [String] {
        addresses.map(\.value)
    }

    var addressSummary: String {
        DeviceEndpointPolicy.addressSummary(addresses)
    }

    var normalizedHostname: String {
        DeviceEndpointPolicy.normalizedHostname(hostname)?.lowercased() ?? ""
    }

    var addressKeys: Set<String> {
        Set(addresses.map(\.identityKey))
    }

    func matches(_ other: DeviceNetworkIdentity) -> Bool {
        if let leftFullname = Self.normalizedOptional(bonjourFullname)?.lowercased(),
           let rightFullname = Self.normalizedOptional(other.bonjourFullname)?.lowercased(),
           leftFullname == rightFullname {
            return true
        }
        if !normalizedConfiguredHost.isEmpty && normalizedConfiguredHost == other.normalizedConfiguredHost {
            return true
        }
        if !normalizedHostname.isEmpty && normalizedHostname == other.normalizedHostname {
            return true
        }
        return !addressKeys.isDisjoint(with: other.addressKeys)
    }

    mutating func setConfiguredSSHTarget(_ target: String) {
        configuredSSHTarget = target
        appendConfiguredTargetAddress()
    }

    mutating func setAddressValues(_ values: [String], source: DeviceAddressSource = .bonjour) {
        addresses = DeviceEndpointPolicy.uniqueAddresses(values.compactMap { DeviceNetworkAddress(value: $0, source: source) })
        appendConfiguredTargetAddress()
    }

    mutating func mergeAddresses(_ newAddresses: [DeviceNetworkAddress]) {
        addresses = DeviceEndpointPolicy.uniqueAddresses(addresses + newAddresses)
    }

    private mutating func appendConfiguredTargetAddress() {
        addresses.removeAll { $0.source == .configured }
        guard let address = DeviceNetworkAddress(value: configuredSSHTarget, source: .configured) else {
            addresses = DeviceEndpointPolicy.uniqueAddresses(addresses)
            return
        }
        addresses = DeviceEndpointPolicy.uniqueAddresses(addresses + [address])
    }

    static func make(
        configuredSSHTarget: String,
        discoveredDevice: DiscoveredDevice?,
        existing: DeviceNetworkIdentity? = nil
    ) -> DeviceNetworkIdentity {
        var identity = DeviceNetworkIdentity(
            configuredSSHTarget: configuredSSHTarget,
            hostname: discoveredDevice?.hostname ?? existing?.hostname,
            bonjourName: discoveredDevice?.name ?? existing?.bonjourName,
            bonjourFullname: discoveredDevice?.fullname ?? existing?.bonjourFullname,
            addresses: existing?.addresses ?? []
        )
        if let discoveredDevice {
            identity.mergeAddresses(discoveredDevice.networkAddresses)
        }
        return identity
    }

    private static func normalizedOptional(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else {
            return nil
        }
        return trimmed
    }
}
