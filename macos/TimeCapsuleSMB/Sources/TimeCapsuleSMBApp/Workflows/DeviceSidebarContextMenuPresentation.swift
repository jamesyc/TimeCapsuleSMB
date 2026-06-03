import Foundation

enum DeviceSidebarContextMenuAction: String, Equatable, Hashable, Identifiable {
    case openOverview
    case openFinder
    case runCheckup
    case viewCheckup
    case refreshStatus
    case settings
    case copySMBAddress
    case copyHostname
    case copyIPAddress
    case removeFromThisMac

    var id: String { rawValue }

    var title: String {
        switch self {
        case .openOverview:
            return L10n.string("sidebar.menu.open_overview")
        case .openFinder:
            return L10n.string("dashboard.action.open_finder")
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .viewCheckup:
            return L10n.string("dashboard.action.view_checkup")
        case .refreshStatus:
            return L10n.string("dashboard.action.refresh_status")
        case .settings:
            return L10n.string("dashboard.action.settings")
        case .copySMBAddress:
            return L10n.string("sidebar.menu.copy_smb_address")
        case .copyHostname:
            return L10n.string("sidebar.menu.copy_hostname")
        case .copyIPAddress:
            return L10n.string("sidebar.menu.copy_ip_address")
        case .removeFromThisMac:
            return L10n.string("sidebar.menu.remove_from_this_mac")
        }
    }

    var systemImage: String {
        switch self {
        case .openOverview:
            return "rectangle.grid.1x2"
        case .openFinder:
            return "folder"
        case .runCheckup:
            return "stethoscope"
        case .viewCheckup:
            return "list.bullet.clipboard"
        case .refreshStatus:
            return "arrow.clockwise"
        case .settings:
            return "gearshape"
        case .copySMBAddress:
            return "link"
        case .copyHostname:
            return "network"
        case .copyIPAddress:
            return "number"
        case .removeFromThisMac:
            return "trash"
        }
    }
}

struct DeviceSidebarContextMenuItem: Equatable, Identifiable {
    let action: DeviceSidebarContextMenuAction
    let isEnabled: Bool

    var id: DeviceSidebarContextMenuAction { action }
    var title: String { action.title }
    var systemImage: String { action.systemImage }
}

struct DeviceSidebarContextMenuPresentation: Equatable {
    let navigationItems: [DeviceSidebarContextMenuItem]
    let clipboardItems: [DeviceSidebarContextMenuItem]
    let destructiveItems: [DeviceSidebarContextMenuItem]
    private let clipboardValues: [DeviceSidebarContextMenuAction: String]

    init(profile: DeviceProfile, summary: DeviceDashboardSummary, isDeviceBusy: Bool) {
        var navigationItems = [
            DeviceSidebarContextMenuItem(action: .openOverview, isEnabled: true)
        ]
        let smbAddress = SMBAddressPolicy.url(for: profile)?.absoluteString
        let hostname = Self.hostname(for: profile)
        let ipAddress = Self.ipAddress(for: profile)
        navigationItems.append(DeviceSidebarContextMenuItem(action: .openFinder, isEnabled: smbAddress != nil))
        navigationItems.append(Self.checkupItem(summary: summary, isDeviceBusy: isDeviceBusy))
        navigationItems.append(DeviceSidebarContextMenuItem(
            action: .refreshStatus,
            isEnabled: !isDeviceBusy && DashboardActionPolicy.isEnabled(.refreshStatus, for: summary)
        ))
        navigationItems.append(DeviceSidebarContextMenuItem(action: .settings, isEnabled: true))
        self.navigationItems = navigationItems

        self.clipboardItems = [
            DeviceSidebarContextMenuItem(action: .copySMBAddress, isEnabled: smbAddress != nil),
            DeviceSidebarContextMenuItem(action: .copyHostname, isEnabled: hostname != nil),
            DeviceSidebarContextMenuItem(action: .copyIPAddress, isEnabled: ipAddress != nil)
        ]
        self.clipboardValues = [
            .copySMBAddress: smbAddress,
            .copyHostname: hostname,
            .copyIPAddress: ipAddress
        ].compactMapValues { $0 }

        self.destructiveItems = [
            DeviceSidebarContextMenuItem(action: .removeFromThisMac, isEnabled: !isDeviceBusy)
        ]
    }

    func clipboardValue(for action: DeviceSidebarContextMenuAction) -> String? {
        clipboardValues[action]
    }

    private static func checkupItem(
        summary: DeviceDashboardSummary,
        isDeviceBusy: Bool
    ) -> DeviceSidebarContextMenuItem {
        if summary.displayStatus == .checking {
            return DeviceSidebarContextMenuItem(action: .viewCheckup, isEnabled: true)
        }
        return DeviceSidebarContextMenuItem(
            action: .runCheckup,
            isEnabled: summary.passwordState == .available
                && !isDeviceBusy
                && DashboardActionPolicy.isEnabled(DashboardSecondaryAction.runCheckup, for: summary)
        )
    }

    private static func hostname(for profile: DeviceProfile) -> String? {
        [
            profile.hostname,
            profile.host
        ]
            .compactMap(DeviceEndpointPolicy.normalizedHostname)
            .first
    }

    private static func ipAddress(for profile: DeviceProfile) -> String? {
        let regular = profile.network.addresses.filter { $0.scope == .regular }
        return regular.first { $0.family == .ipv4 }?.value
            ?? regular.first { $0.family == .ipv6 }?.value
            ?? profile.network.addresses.first { $0.family == .ipv4 }?.value
            ?? profile.network.addresses.first { $0.family == .ipv6 }?.value
    }
}
