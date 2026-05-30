import Foundation

enum DashboardPrimaryAction: String, Equatable {
    case replacePassword
    case runCheckup
    case installSMB
    case viewCheckup
    case openSMB

    var title: String {
        switch self {
        case .replacePassword:
            return L10n.string("dashboard.action.replace_password")
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .installSMB:
            return L10n.string("dashboard.action.install_update_smb")
        case .viewCheckup:
            return L10n.string("dashboard.action.view_checkup")
        case .openSMB:
            return L10n.string("dashboard.action.open_smb")
        }
    }

    var systemImage: String {
        switch self {
        case .replacePassword:
            return "key"
        case .runCheckup:
            return "stethoscope"
        case .installSMB:
            return "square.and.arrow.down.on.square"
        case .viewCheckup:
            return "list.bullet.clipboard"
        case .openSMB:
            return "folder"
        }
    }
}

struct DeviceDashboardSummary: Equatable {
    let profile: DeviceProfile
    let passwordState: DevicePasswordState
    let displayStatus: DeviceDisplayStatus
    let primaryAction: DashboardPrimaryAction
    let hostWarning: HostCompatibilityWarning?
}

struct PresentationRow: Equatable, Identifiable {
    var id: String {
        "\(label):\(value)"
    }

    let label: String
    let value: String
}
