import Foundation

struct DiscoveredDevice: Identifiable, Equatable {
    let id: String
    let name: String
    let host: String
    let hostname: String
    let addresses: [String]
    let syap: String?
    let model: String?
    let rawRecord: JSONValue

    init(payload: DiscoveredDevicePayload, index: Int) {
        self.id = payload.id.isEmpty ? "discovered-\(index)" : payload.id
        self.name = payload.name.isEmpty ? (payload.hostname.isEmpty ? "AirPort Device" : payload.hostname) : payload.name
        self.host = payload.host
        self.hostname = payload.hostname
        self.addresses = payload.addresses.isEmpty ? payload.ipv4 + payload.ipv6 : payload.addresses
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

        self.id = stableID.isEmpty ? "discovered-\(index)" : stableID
        self.name = record.name.isEmpty ? (record.hostname.isEmpty ? "AirPort Device" : record.hostname) : record.name
        self.hostname = record.hostname
        self.addresses = record.ipv4 + record.ipv6
        self.host = Self.displayHost(record)
        self.syap = Self.nonEmpty(record.properties["syAP"] ?? record.properties["syap"])
        self.model = Self.nonEmpty(record.properties["model"] ?? record.properties["am"])
        self.rawRecord = record.jsonValue
    }

    var discoveryModelText: String {
        Self.nonEmpty(model) ?? ""
    }

    private static func displayHost(_ record: BonjourResolvedServicePayload) -> String {
        if let address = record.ipv4.first ?? record.ipv6.first {
            return address
        }
        return record.hostname
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
