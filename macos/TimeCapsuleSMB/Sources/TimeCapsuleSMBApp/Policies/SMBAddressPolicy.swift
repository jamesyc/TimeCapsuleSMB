import Foundation

enum SMBAddressPolicy {
    static func url(for profile: DeviceProfile, account: String? = nil) -> URL? {
        guard let host = preferredHost(for: profile) else {
            return nil
        }
        return url(host: host, account: account)
    }

    static func preferredHost(for profile: DeviceProfile) -> String? {
        if let serviceHost = bonjourSMBServiceHost(for: profile) {
            return serviceHost
        }
        if let hostname = normalizedAddressHost(profile.hostname) {
            return hostname
        }
        if let address = profile.network.addresses.first(where: { $0.scope == .regular }) {
            return address.value
        }
        if let address = profile.network.addresses.first {
            return address.value
        }
        return normalizedAddressHost(profile.host)
    }

    static func credentialServerCandidates(for profile: DeviceProfile) -> [String] {
        unique([
            normalizedAddressHost(profile.hostname),
            normalizedAddressHost(profile.host)
        ] + profile.network.addresses.map { normalizedAddressHost($0.value) })
    }

    static func reachabilityHostCandidates(for profile: DeviceProfile) -> [String] {
        unique([
            preferredHost(for: profile),
            normalizedAddressHost(profile.hostname),
            normalizedAddressHost(profile.host)
        ] + profile.network.addresses.map { normalizedAddressHost($0.value) })
    }

    private static func bonjourSMBServiceHost(for profile: DeviceProfile) -> String? {
        guard let label = bonjourLabel(for: profile) else {
            return nil
        }
        // macOS's SMB client splits the service host at unescaped dots, so the
        // label's own dots and backslashes need DNS escapes: Time.Capsule
        // connects only as Time%5C.Capsule (NetBSD 4 LE, 2026-10-03).
        let escaped = label
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: ".", with: "\\.")
        return "\(escaped)._smb._tcp.local"
    }

    private static func bonjourLabel(for profile: DeviceProfile) -> String? {
        if let fullname = profile.bonjourFullname,
           !fullname.isEmpty {
            // Remove only the DNS root separator, never any part of the label.
            let trimmed = fullname.hasSuffix(".") ? String(fullname.dropLast()) : fullname
            let lowercased = trimmed.lowercased()
            for service in ["._smb._tcp.local", "._airport._tcp.local", "._adisk._tcp.local", "._device-info._tcp.local"]
            where lowercased.hasSuffix(service) {
                return String(trimmed.dropLast(service.count))
            }
        }

        guard let bonjourName = profile.bonjourName,
              !bonjourName.isEmpty else {
            return nil
        }
        return bonjourName
    }

    private static func url(host: String, account: String?) -> URL? {
        DeviceEndpointPolicy.smbURL(host: host, account: account)
    }

    private static func normalizedAddressHost(_ value: String?) -> String? {
        DeviceEndpointPolicy.hostComponent(value)
    }

    private static func unique(_ values: [String?]) -> [String] {
        var seen: Set<String> = []
        var ordered: [String] = []
        for value in values {
            guard let value else { continue }
            let key = value.lowercased()
            if seen.insert(key).inserted {
                ordered.append(value)
            }
        }
        return ordered
    }
}
