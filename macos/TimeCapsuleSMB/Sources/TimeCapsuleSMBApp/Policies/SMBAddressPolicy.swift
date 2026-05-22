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
        return normalizedAddressHost(profile.host)
    }

    static func credentialServerCandidates(for profile: DeviceProfile) -> [String] {
        unique([
            normalizedAddressHost(profile.hostname),
            normalizedAddressHost(profile.host)
        ])
    }

    private static func bonjourSMBServiceHost(for profile: DeviceProfile) -> String? {
        if let fullname = profile.bonjourFullname?.trimmingCharacters(in: .whitespacesAndNewlines),
           !fullname.isEmpty {
            let trimmed = fullname.trimmingCharacters(in: CharacterSet(charactersIn: "."))
            let lowercased = trimmed.lowercased()
            if lowercased.hasSuffix("._smb._tcp.local") {
                return trimmed
            }
            for service in ["._airport._tcp.local", "._adisk._tcp.local", "._device-info._tcp.local"] {
                if lowercased.hasSuffix(service) {
                    return String(trimmed.dropLast(service.count)) + "._smb._tcp.local"
                }
            }
        }

        guard let bonjourName = profile.bonjourName?.trimmingCharacters(in: .whitespacesAndNewlines),
              !bonjourName.isEmpty else {
            return nil
        }
        return "\(bonjourName)._smb._tcp.local"
    }

    private static func url(host: String, account: String?) -> URL? {
        guard let encodedHost = host.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed) else {
            return nil
        }
        let accountPrefix: String
        if let account = account?.trimmingCharacters(in: .whitespacesAndNewlines),
           !account.isEmpty,
           let encodedAccount = account.addingPercentEncoding(withAllowedCharacters: .urlUserAllowed) {
            accountPrefix = "\(encodedAccount)@"
        } else {
            accountPrefix = ""
        }
        return URL(string: "smb://\(accountPrefix)\(encodedHost)")
    }

    private static func normalizedAddressHost(_ value: String?) -> String? {
        guard var candidate = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !candidate.isEmpty else {
            return nil
        }

        if let parsedURL = URL(string: candidate), let parsedHost = parsedURL.host, !parsedHost.isEmpty {
            candidate = parsedHost
        } else {
            candidate = candidate.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
                .first
                .map(String.init) ?? candidate
            candidate = candidate.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false)
                .last
                .map(String.init) ?? candidate
        }

        let normalized = candidate
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return normalized.isEmpty ? nil : normalized
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
