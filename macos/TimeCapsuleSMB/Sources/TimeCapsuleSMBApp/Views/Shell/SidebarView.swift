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
            if let compatibilityBadge {
                Text(compatibilityBadge)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.12))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
            }
            Image(systemName: summary.displayStatus.systemImage)
                .foregroundStyle(statusColor)
                .help(summary.displayStatus.title)
        }
    }

    private var compatibilityBadge: String? {
        let payloadFamily = profile.payloadFamily?.lowercased() ?? ""
        let osRelease = profile.osRelease ?? ""
        if payloadFamily.contains("netbsd4") || osRelease.hasPrefix("4.") {
            return "NetBSD 4"
        }
        return nil
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
