import Foundation

struct ManualDeviceTarget: Equatable {
    let host: String

    init(host: String) {
        self.host = host.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

enum AddDeviceTarget: Equatable {
    case discovered(DiscoveredDevice)
    case manual(ManualDeviceTarget)

    var targetHost: String {
        switch self {
        case .discovered(let device):
            return device.connectionTarget
        case .manual(let target):
            return target.host
        }
    }

    var selectedRecord: JSONValue? {
        switch self {
        case .discovered(let device):
            return device.rawRecord
        case .manual:
            return nil
        }
    }

    var discoveredDevice: DiscoveredDevice? {
        switch self {
        case .discovered(let device):
            return device
        case .manual:
            return nil
        }
    }

    var isEmpty: Bool {
        targetHost.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @MainActor
    func matchingProfile(in registry: DeviceRegistryStore) -> DeviceProfile? {
        switch self {
        case .discovered(let device):
            return registry.matchingProfile(for: device)
        case .manual:
            return registry.matchingProfile(host: targetHost, bonjourFullname: nil)
        }
    }

    func setupLaneKey(existingProfileID: DeviceProfile.ID?) -> OperationLaneKey {
        if let existingProfileID {
            return .deviceWorkflow(existingProfileID, .configure)
        }
        let normalized = DeviceEndpointPolicy.normalizedHostKey(targetHost)
        return .candidateHost(normalized.isEmpty ? targetHost : normalized)
    }
}
