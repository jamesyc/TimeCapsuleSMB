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
    // Confirmed waMA survives app launches and DHCP changes in the existing profile.
    var airportMAC: String?

    init(
        configuredSSHTarget: String,
        hostname: String? = nil,
        bonjourName: String? = nil,
        bonjourFullname: String? = nil,
        addresses: [DeviceNetworkAddress] = [],
        airportMAC: String? = nil
    ) {
        self.configuredSSHTarget = configuredSSHTarget
        self.hostname = Self.normalizedOptional(hostname)
        self.bonjourName = Self.normalizedOptional(bonjourName)
        self.bonjourFullname = Self.normalizedOptional(bonjourFullname)
        self.airportMAC = Self.normalizedAirportMAC(airportMAC)
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

    var matchableAddressKeys: Set<String> {
        Set(addresses.filter { $0.scope == .regular }.map(\.identityKey))
    }

    func matches(_ other: DeviceNetworkIdentity) -> Bool {
        if let left = airportMAC, let right = other.airportMAC {
            return left == right
        }
        if !normalizedConfiguredHost.isEmpty && normalizedConfiguredHost == other.normalizedConfiguredHost {
            return true
        }
        // This identity is the saved profile; only its configured endpoint can
        // associate an observation. Historical Bonjour addresses can be reused.
        return DeviceEndpointPolicy.addressFamily(for: configuredHost) != nil
            && other.matchableAddressKeys.contains(normalizedConfiguredHost)
    }

    func sharesName(with other: DeviceNetworkIdentity) -> Bool {
        if let left = airportMAC, let right = other.airportMAC, left != right { return false }
        return (!normalizedHostname.isEmpty && normalizedHostname == other.normalizedHostname)
            || (bonjourFullname?.lowercased() != nil && bonjourFullname?.lowercased() == other.bonjourFullname?.lowercased())
    }

    static func normalizedAirportMAC(_ value: String?) -> String? {
        guard let value else { return nil }
        let parts = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            .replacingOccurrences(of: "-", with: ":").split(separator: ":", omittingEmptySubsequences: false)
        guard parts.count == 6, parts.allSatisfy({ part in
                  part.count == 2 && part.allSatisfy { "0123456789abcdef".contains($0) }
              }),
              let first = UInt8(parts[0], radix: 16), first & 1 == 0,
              parts.contains(where: { $0 != "00" }) else { return nil }
        return parts.joined(separator: ":")
    }

    mutating func setConfiguredSSHTarget(_ target: String) {
        configuredSSHTarget = target
        appendConfiguredTargetAddress()
    }

    mutating func setAddressValues(_ values: [String], source: DeviceAddressSource = .bonjour) {
        addresses = DeviceEndpointPolicy.uniqueAddresses(values.compactMap { DeviceNetworkAddress(value: $0, source: source) })
        appendConfiguredTargetAddress()
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
        existing: DeviceNetworkIdentity? = nil,
        airportMAC: String? = nil
    ) -> DeviceNetworkIdentity {
        let identity = DeviceNetworkIdentity(
            configuredSSHTarget: configuredSSHTarget,
            hostname: discoveredDevice?.hostname ?? existing?.hostname,
            bonjourName: discoveredDevice?.name ?? existing?.bonjourName,
            bonjourFullname: discoveredDevice?.fullname ?? existing?.bonjourFullname,
            addresses: discoveredDevice?.networkAddresses
                ?? (existing?.normalizedConfiguredHost == DeviceEndpointPolicy.normalizedHostKey(configuredSSHTarget) ? existing?.addresses ?? [] : []),
            airportMAC: airportMAC ?? existing?.airportMAC
        )
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
