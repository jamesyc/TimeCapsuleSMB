import Foundation

/// The SMB address the app hands Finder. Our own connections never come from
/// here: every one goes through `DeviceProfile.connectionTarget`, the address
/// configure verified (TC_HOST for the helper). Finder gets the Bonjour
/// service name, as its own Bonjour browsing does: Finder resolves it itself
/// and it follows the device to a new IP address.
enum SMBAddressPolicy {
    static func url(for profile: DeviceProfile, account: String? = nil) -> URL? {
        guard let host = preferredHost(for: profile) else {
            return nil
        }
        return url(host: host, account: account)
    }

    static func preferredHost(for profile: DeviceProfile) -> String? {
        // A saved name Apple renamed for a conflict ("Name (2)") can belong to
        // the other device after a restart, and so can its host name.
        if let label = profile.network.savedServiceName, AppleBonjourName.hasConflictSuffix(label) {
            return normalizedAddressHost(profile.connectionTarget)
        }
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

    /// Names the SMB password may be saved under in the keychain: a lookup, not a connection.
    static func credentialServerCandidates(for profile: DeviceProfile) -> [String] {
        unique([
            normalizedAddressHost(profile.hostname),
            normalizedAddressHost(profile.host)
        ] + profile.network.addresses.map { normalizedAddressHost($0.value) })
    }

    private static func bonjourSMBServiceHost(for profile: DeviceProfile) -> String? {
        guard let label = profile.network.savedServiceName else {
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
