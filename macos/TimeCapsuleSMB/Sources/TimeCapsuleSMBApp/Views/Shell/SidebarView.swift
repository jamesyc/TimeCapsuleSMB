import SwiftUI

struct DeviceSidebarRow: View {
    let profile: DeviceProfile
    let summary: DeviceDashboardSummary
    var lastSeenText: String?

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "externaldrive")
            VStack(alignment: .leading, spacing: 2) {
                Text(profile.title)
                    .lineLimit(1)
                HStack(spacing: 4) {
                    Text(profile.displayTarget)
                        .lineLimit(1)
                    if let lastSeenText {
                        Text("- \(lastSeenText)")
                            .lineLimit(1)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
            Spacer(minLength: 6)
            Image(systemName: summary.displayStatus.systemImage)
                .foregroundStyle(statusColor)
                .help(summary.displayStatus.title)
        }
    }

    private var statusColor: Color {
        switch summary.displayStatus {
        case .healthy:
            return .green
        case .warning, .activationNeeded:
            return .yellow
        case .failed, .passwordInvalid, .keychainUnavailable, .offline, .unsupported:
            return .red
        case .installing, .checking, .maintaining, .readyToInstall:
            return .accentColor
        default:
            return .secondary
        }
    }
}

struct DeviceSidebarContextMenu: View {
    let presentation: DeviceSidebarContextMenuPresentation
    let performAction: (DeviceSidebarContextMenuAction) -> Void

    var body: some View {
        ForEach(presentation.navigationItems) { item in
            menuButton(item)
        }

        Divider()

        ForEach(presentation.clipboardItems) { item in
            menuButton(item)
        }

        Divider()

        ForEach(presentation.destructiveItems) { item in
            menuButton(item, role: .destructive)
        }
    }

    private func menuButton(
        _ item: DeviceSidebarContextMenuItem,
        role: ButtonRole? = nil
    ) -> some View {
        Button(role: role) {
            performAction(item.action)
        } label: {
            Label(item.title, systemImage: item.systemImage)
        }
        .disabled(!item.isEnabled)
    }
}
