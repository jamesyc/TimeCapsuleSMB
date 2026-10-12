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
        // These are DNS identities, so whitespace is part of the service label.
        self.bonjourName = bonjourName.flatMap { $0.isEmpty ? nil : $0 }
        self.bonjourFullname = bonjourFullname.flatMap { $0.isEmpty ? nil : $0 }
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
        DeviceEndpointPolicy.addressSummary(addresses, target: configuredSSHTarget)
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

/// The device identity an operation reported: the AirPort MAC it read with
/// the right password, and the Bonjour names Apple advertises for the device
/// before any conflict rename: the instance name from syNm and ACPd's host
/// label from syDN or syNm (services.runtime.airport_identity_fields).
struct AirportIdentityObservation: Equatable, Sendable {
    let mac: String
    let instanceName: String?
    let hostLabel: String?
    /// ACPd built the host label from syDN, which stays when syNm is renamed.
    let hostLabelFromDNSName: Bool

    init?(mac: String?, instanceName: String? = nil, hostLabel: String? = nil, hostLabelFromDNSName: Bool = false) {
        guard let mac = DeviceNetworkIdentity.normalizedAirportMAC(mac) else { return nil }
        self.mac = mac
        self.instanceName = instanceName
        self.hostLabel = hostLabel?.lowercased()
        self.hostLabelFromDNSName = hostLabel != nil && hostLabelFromDNSName
    }

    /// A result carries the identity in its payload, a failure in its debug fields.
    init?(event: BackendEvent) {
        for source in [event.payload, event.debug] {
            if let source, let mac = source.stringValue(for: "airport_mac") {
                self.init(mac: mac, instanceName: source.stringValue(for: "airport_instance_name"),
                          hostLabel: source.stringValue(for: "airport_host_label"),
                          hostLabelFromDNSName: source.stringValue(for: "airport_host_label_source") == "syDN")
                return
            }
        }
        return nil
    }

    var names: AirportNames {
        AirportNames(serviceName: instanceName, hostLabel: hostLabel, hostLabelFromDNSName: hostLabelFromDNSName)
    }
}

/// The names a device goes by on the network: its Bonjour service name and
/// the first label of its host name, and whether that label came from the
/// device's syDN (known only from the device itself, never from discovery).
struct AirportNames: Equatable {
    let serviceName: String?
    let hostLabel: String?
    var hostLabelFromDNSName = false
}

/// What a device's names say about whether it is the one a profile without
/// a MAC was saved for.
enum AirportNameEvidence: Equatable {
    /// The profile kept no name: only its address says which device it is.
    case noName
    case same
    case different
    /// Another device has or had the name, so it identifies neither.
    case ambiguous
}

extension DeviceNetworkIdentity {
    /// The Bonjour service name this profile was saved with.
    var savedServiceName: String? {
        if let fullname = bonjourFullname, !fullname.isEmpty {
            // Remove only the DNS root separator, never any part of the label.
            let trimmed = fullname.hasSuffix(".") ? String(fullname.dropLast()) : fullname
            let lowercased = trimmed.lowercased()
            for service in ["._smb._tcp.local", "._airport._tcp.local", "._adisk._tcp.local", "._device-info._tcp.local"]
            where lowercased.hasSuffix(service) {
                return String(trimmed.dropLast(service.count))
            }
        }
        guard let bonjourName, !bonjourName.isEmpty else { return nil }
        return bonjourName
    }

    /// The first label of the saved hostname, unless it is a fallback that
    /// does not name the device: v2.x advertised `timecapsule` without a
    /// kernel hostname, and ACPd falls back to base-station-<MAC>.
    var savedHostLabel: String? {
        guard let first = hostname?.split(separator: ".").first?.lowercased(), !first.isEmpty,
              first != "timecapsule", !first.hasPrefix("base-station-") else { return nil }
        return first
    }

    var savedNames: AirportNames { AirportNames(serviceName: savedServiceName, hostLabel: savedHostLabel) }

    /// Whether a device with `names` is the one this profile was saved for,
    /// judged by name alone. `visible` are the names of the other devices on
    /// the network now, `saved` those of the other saved profiles.
    ///
    /// Names stand in for a MAC only for a profile saved before MACs were, and
    /// two devices can share one: Apple renames one of them (AppleBonjourName)
    /// and they can swap names after a restart. So a name says which device
    /// this is only while nothing else has it once Apple's suffix is taken
    /// off, and a saved or advertised name with the suffix shows it was shared.
    /// Names compare as Bonjour compares them (AppleBonjourName.key).
    ///
    /// Two known service names decide. A host label decides only when one
    /// side has no service name, or when the device's label comes from its
    /// syDN, which a rename of syNm leaves alone. Otherwise it proves little:
    /// ACPd turns every non-ASCII byte into "-", so "Café" and "Cafè" are
    /// both Caf and "Office Ä" and "Office Ö" both Office, and "Office 2" and
    /// "Office-2" share Office-2. A visible device on this very host label is
    /// a conflict Apple has yet to rename, so it leaves the name ambiguous; a
    /// saved profile's label is only what it was when saved, and Apple's
    /// unsaved renames let two profiles keep the same one.
    func nameEvidence(for names: AirportNames, visible: [AirportNames], saved others: [AirportNames]) -> AirportNameEvidence {
        let saved = savedNames
        guard saved.serviceName != nil || saved.hostLabel != nil else { return .noName }
        for name in [saved.serviceName, names.serviceName].compactMap({ $0 }) where AppleBonjourName.hasConflictSuffix(name) {
            return .ambiguous
        }
        func baseKey(_ name: String) -> [UInt8] { AppleBonjourName.key(AppleBonjourName.conflictBase(name)) }
        let everyone = visible + others
        if let name = names.serviceName, let savedName = saved.serviceName {
            if AppleBonjourName.key(name) == AppleBonjourName.key(savedName) {
                let key = baseKey(name)
                let shared = everyone.contains { $0.serviceName.map(baseKey) == key }
                    || visible.contains { names.hostLabel != nil && $0.hostLabel == names.hostLabel }
                return shared ? .ambiguous : .same
            }
            guard names.hostLabelFromDNSName else { return .different }
        }
        guard let label = names.hostLabel, label == saved.hostLabel else { return .different }
        let bases = [names.serviceName, saved.serviceName].compactMap { $0 }.map(baseKey)
        let shared = everyone.contains { other in
            other.hostLabel == label || other.serviceName.map { bases.contains(baseKey($0)) } == true
        }
        return shared ? .ambiguous : .same
    }
}
