import Darwin
import Foundation

enum DeviceEndpointPolicy {
    static func rootSSHTarget(_ target: String) -> String {
        let trimmed = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let endpoint = parseEndpoint(trimmed),
              !endpoint.host.isEmpty else {
            return trimmed
        }
        if trimmed.contains("@"), endpoint.user.isEmpty {
            return trimmed
        }
        let user = endpoint.user.isEmpty ? "root" : endpoint.user
        return "\(user)@\(renderSSHHost(endpoint))"
    }

    static func hostComponent(_ value: String?) -> String? {
        guard let endpoint = parseEndpoint(value),
              !endpoint.host.isEmpty else {
            return nil
        }
        return endpoint.host
    }

    static func normalizedHostKey(_ value: String?) -> String {
        guard let host = hostComponent(value) else {
            return ""
        }
        if let address = DeviceNetworkAddress(value: host, source: .configured) {
            return "\(address.family.rawValue):\(address.normalizedValue)"
        }
        return "hostname:\(host.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased())"
    }

    static func preferredSetupTarget(for identity: DeviceNetworkIdentity) -> String? {
        if let address = identity.addresses.first(where: { $0.family == .ipv4 && $0.scope == .regular }) {
            return address.value
        }
        if let address = identity.addresses.first(where: { $0.family == .ipv6 && $0.scope == .regular }) {
            return address.value
        }
        if let address = identity.addresses.first(where: { $0.family == .ipv6 }) {
            return address.value
        }
        if let hostname = normalizedHostname(identity.hostname) {
            return hostname
        }
        if let address = identity.addresses.first(where: { $0.family == .ipv4 }) {
            return address.value
        }
        return hostComponent(identity.configuredSSHTarget)
    }

    static func displayTarget(for identity: DeviceNetworkIdentity) -> String {
        if let hostname = normalizedHostname(identity.hostname) {
            return hostname
        }
        if let target = preferredSetupTarget(for: identity) {
            return target
        }
        return hostComponent(identity.configuredSSHTarget)
            ?? identity.configuredSSHTarget.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func normalizedHostname(_ value: String?) -> String? {
        guard let host = hostComponent(value),
              addressFamily(for: host) == nil else {
            return nil
        }
        let normalized = host.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return normalized.isEmpty ? nil : normalized
    }

    static func addressFamily(for value: String) -> NetworkAddressFamily? {
        if inetPton(AF_INET, value) {
            return .ipv4
        }
        if inetPton(AF_INET6, ipv6LiteralForParsing(value)) {
            return .ipv6
        }
        return nil
    }

    static func addressScope(value: String, family: NetworkAddressFamily) -> NetworkAddressScope {
        switch family {
        case .ipv4:
            if value.hasPrefix("169.254.") {
                return .linkLocal
            }
            if value.hasPrefix("127.") {
                return .loopback
            }
            return .regular
        case .ipv6:
            let literal = ipv6LiteralForParsing(value).lowercased()
            if literal == "::1" {
                return .loopback
            }
            let firstHextet = literal.split(separator: ":", maxSplits: 1).first.map(String.init) ?? ""
            if let value = Int(firstHextet, radix: 16), (value & 0xffc0) == 0xfe80 {
                return .linkLocal
            }
            return .regular
        }
    }

    static func normalizedAddressValue(_ value: String, family: NetworkAddressFamily) -> String {
        switch family {
        case .ipv4:
            return value
        case .ipv6:
            return value.lowercased()
        }
    }

    static func uniqueAddresses(_ addresses: [DeviceNetworkAddress]) -> [DeviceNetworkAddress] {
        var seen: Set<String> = []
        var ordered: [DeviceNetworkAddress] = []
        for address in addresses {
            if seen.insert(address.identityKey).inserted {
                ordered.append(address)
            }
        }
        return ordered
    }

    static func addressSummary(_ addresses: [DeviceNetworkAddress]) -> String {
        let regular = addresses.filter { $0.scope == .regular }
        let prioritized = regular.isEmpty ? addresses : regular
        return prioritized
            .map { "\($0.family.title) \($0.value)" + ($0.scope == .linkLocal ? " link-local" : "") }
            .joined(separator: "  ")
    }

    static func smbURL(host: String, account: String?) -> URL? {
        let renderedHost: String
        if addressFamily(for: host) == .ipv6 {
            renderedHost = "[\(host)]"
        } else if let encodedHost = host.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed) {
            renderedHost = encodedHost
        } else {
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
        return URL(string: "smb://\(accountPrefix)\(renderedHost)")
    }

    private static func ipv6LiteralForParsing(_ value: String) -> String {
        value.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
            .split(separator: "%", maxSplits: 1, omittingEmptySubsequences: false)
            .first
            .map(String.init) ?? value
    }

    private struct Endpoint {
        var user: String
        var host: String
        var port: Int?
        var invalidPort: String?
    }

    private static func parseEndpoint(_ value: String?) -> Endpoint? {
        guard var candidate = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !candidate.isEmpty else {
            return nil
        }

        if let url = URLComponents(string: candidate), let host = url.host, !host.isEmpty {
            return Endpoint(
                user: url.user ?? "",
                host: normalizedEndpointHost(host),
                port: url.port,
                invalidPort: nil
            )
        }

        candidate = candidate.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
            .first
            .map(String.init) ?? candidate
        var user = ""
        if let at = candidate.lastIndex(of: "@") {
            user = String(candidate[..<at])
            candidate = String(candidate[candidate.index(after: at)...])
        }

        var host = candidate
        var port: Int?
        var invalidPort: String?
        if candidate.hasPrefix("["),
           let end = candidate.firstIndex(of: "]") {
            host = String(candidate[candidate.index(after: candidate.startIndex)..<end])
            let suffix = String(candidate[candidate.index(after: end)...])
            if suffix.hasPrefix(":") {
                let portText = String(suffix.dropFirst())
                if let parsedPort = Int(portText) {
                    port = parsedPort
                } else if !portText.isEmpty {
                    invalidPort = portText
                }
            } else if !suffix.isEmpty {
                invalidPort = suffix
            }
        } else if candidate.filter({ $0 == ":" }).count == 1,
                  let colon = candidate.lastIndex(of: ":") {
            let hostPart = String(candidate[..<colon])
            let portText = String(candidate[candidate.index(after: colon)...])
            if let parsedPort = Int(portText) {
                host = hostPart
                port = parsedPort
            } else if !portText.isEmpty {
                invalidPort = portText
            }
        }

        return Endpoint(
            user: user,
            host: normalizedEndpointHost(host),
            port: port,
            invalidPort: invalidPort
        )
    }

    private static func normalizedEndpointHost(_ value: String) -> String {
        var candidate = value
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if addressFamily(for: candidate) == nil {
            candidate = candidate.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        }
        return candidate
    }

    private static func renderSSHHost(_ endpoint: Endpoint) -> String {
        guard let portText = endpoint.port.map(String.init) ?? endpoint.invalidPort,
              endpoint.port != 22 else {
            return endpoint.host
        }
        let renderedHost = addressFamily(for: endpoint.host) == .ipv6 ? "[\(endpoint.host)]" : endpoint.host
        return "\(renderedHost):\(portText)"
    }

    private static func inetPton(_ family: Int32, _ value: String) -> Bool {
        value.withCString { cString in
            switch family {
            case AF_INET:
                var address = in_addr()
                return inet_pton(AF_INET, cString, &address) == 1
            case AF_INET6:
                var address = in6_addr()
                return inet_pton(AF_INET6, cString, &address) == 1
            default:
                return false
            }
        }
    }
}
