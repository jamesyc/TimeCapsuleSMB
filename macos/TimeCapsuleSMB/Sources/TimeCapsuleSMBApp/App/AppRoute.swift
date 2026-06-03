import Foundation

enum AppRoute: Equatable, Hashable, Identifiable {
    case allDevices
    case activity
    case appSettings
    case addDevice
    case device(DeviceProfile.ID)

    var id: String {
        switch self {
        case .allDevices:
            return "all"
        case .activity:
            return "activity"
        case .appSettings:
            return "settings"
        case .addDevice:
            return "add"
        case .device(let profileID):
            return "device:\(profileID)"
        }
    }

    var selectedDeviceID: DeviceProfile.ID? {
        guard case .device(let profileID) = self else {
            return nil
        }
        return profileID
    }
}
