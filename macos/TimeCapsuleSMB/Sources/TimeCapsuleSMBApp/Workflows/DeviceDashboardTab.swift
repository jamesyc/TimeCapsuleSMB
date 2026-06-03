import Foundation

enum DeviceDashboardTab: String, CaseIterable, Equatable, Identifiable {
    case overview
    case install
    case checkup
    case maintenance
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview:
            return L10n.string("dashboard.tab.overview")
        case .install:
            return L10n.string("dashboard.tab.install")
        case .checkup:
            return L10n.string("dashboard.tab.checkup")
        case .maintenance:
            return L10n.string("dashboard.tab.maintenance")
        case .settings:
            return L10n.string("dashboard.tab.settings")
        }
    }
}
