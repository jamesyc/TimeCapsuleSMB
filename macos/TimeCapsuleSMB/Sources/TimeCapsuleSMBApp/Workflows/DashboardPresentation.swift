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
            return "square.and.arrow.up"
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

struct MaintenanceWorkflowPresentation: Equatable {
    let title: String
    let subtitle: String
    let primaryAction: String
    let risk: String

    static func presentation(for workflow: MaintenanceWorkflow) -> MaintenanceWorkflowPresentation {
        switch workflow {
        case .activate:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.activate.title"),
                subtitle: L10n.string("maintenance.presentation.activate.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.activate.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.remote_write")
            )
        case .uninstall:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.uninstall.title"),
                subtitle: L10n.string("maintenance.presentation.uninstall.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.uninstall.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.destructive")
            )
        case .fsck:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.fsck.title"),
                subtitle: L10n.string("maintenance.presentation.fsck.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.fsck.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.destructive")
            )
        case .repairXattrs:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.repair_xattrs.title"),
                subtitle: L10n.string("maintenance.presentation.repair_xattrs.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.repair_xattrs.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.local_destructive")
            )
        }
    }
}
