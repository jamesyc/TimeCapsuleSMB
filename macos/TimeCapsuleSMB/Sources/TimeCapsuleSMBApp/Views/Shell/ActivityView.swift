import SwiftUI

struct ActivityCompactView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore
    let context: ActivityDisplayContext

    var body: some View {
        let status = activityStore.compactStatus(for: context)
        HStack(spacing: 10) {
            Image(systemName: icon(for: status))
                .foregroundStyle(iconColor(for: status))
            messageView(status)
            Spacer()
            if let latestTimelineTitle = status.latestTimelineTitle {
                Text(latestTimelineTitle)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(Color.secondary.opacity(0.06))
    }

    @ViewBuilder
    private func messageView(_ status: ActivityCompactStatus) -> some View {
        if let latestMessage = status.latestMessage, !latestMessage.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text(title(status))
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                AnimatedProgressText(message: latestMessage, isRunning: status.isRunning)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(height: 30, alignment: .center)
        } else {
            Text(title(status))
                .font(.caption.weight(.medium))
                .lineLimit(1)
                .frame(height: 30, alignment: .center)
        }
    }

    private func title(_ status: ActivityCompactStatus) -> String {
        if case .device(let activeDeviceID) = status.scope,
           let profile = registry.profile(id: activeDeviceID) {
            return "\(status.operationTitle) - \(profile.title)"
        }
        return status.operationTitle
    }

    private func icon(for status: ActivityCompactStatus) -> String {
        if status.requiresAttention {
            return "exclamationmark.triangle"
        }
        return status.isRunning ? "hourglass" : "checkmark.circle"
    }

    private func iconColor(for status: ActivityCompactStatus) -> Color {
        if status.requiresAttention {
            return .yellow
        }
        return status.isRunning ? Color.accentColor : Color.secondary
    }
}

struct ActivityDetailView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center, spacing: 12) {
                    Image(systemName: activityStore.hasActiveActivity ? "hourglass" : "clock")
                        .font(.title2)
                        .foregroundStyle(activityStore.hasActiveActivity ? Color.accentColor : Color.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(L10n.string("sidebar.activity"))
                            .font(.title2.weight(.semibold))
                        Text(activeActivityMessage)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }

                if activityStore.activeLaneSnapshots.isEmpty && activityStore.recentLaneSnapshots.isEmpty {
                    Text(L10n.string("activity.timeline.empty"))
                        .foregroundStyle(.secondary)
                } else {
                    if !activityStore.activeLaneSnapshots.isEmpty {
                        ActivityLaneSection(
                            title: L10n.string("activity.active"),
                            snapshots: activityStore.activeLaneSnapshots,
                            registry: registry
                        )
                    }
                    if !activityStore.recentLaneSnapshots.isEmpty {
                        ActivityLaneSection(
                            title: L10n.string("activity.recent"),
                            snapshots: activityStore.recentLaneSnapshots,
                            registry: registry
                        )
                    }
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func title(_ snapshot: ActivitySnapshot) -> String {
        if case .device(let activeDeviceID) = snapshot.scope,
           let profile = registry.profile(id: activeDeviceID) {
            return "\(snapshot.operationTitle) - \(profile.title)"
        }
        return snapshot.operationTitle
    }

    private var activeActivityMessage: String {
        guard activityStore.hasActiveActivity else {
            return L10n.string("activity.no_active_operation")
        }
        let count = activityStore.activeLaneSnapshots.count
        return count == 1
            ? L10n.string("activity.one_active")
            : L10n.format("activity.multiple_active", count)
    }
}

private struct ActivityLaneSection: View {
    let title: String
    let snapshots: [ActivityLaneSnapshot]
    @ObservedObject var registry: DeviceRegistryStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
            ForEach(snapshots) { laneSnapshot in
                ActivityLaneCard(laneSnapshot: laneSnapshot, registry: registry)
            }
        }
    }
}

private struct ActivityLaneCard: View {
    let laneSnapshot: ActivityLaneSnapshot
    @ObservedObject var registry: DeviceRegistryStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 8) {
                headerIcon
                VStack(alignment: .leading, spacing: 2) {
                    Text(title(laneSnapshot.snapshot))
                        .font(.body.weight(.medium))
                    if let latestMessage = laneSnapshot.snapshot.latestMessage, !latestMessage.isEmpty {
                        AnimatedProgressText(message: latestMessage, isRunning: laneSnapshot.snapshot.isRunning)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
            }

            VStack(alignment: .leading, spacing: 4) {
                if laneSnapshot.snapshot.timeline.isEmpty {
                    Text(L10n.string("activity.timeline.empty"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(laneSnapshot.snapshot.timeline) { item in
                        OperationTimelineRow(item: item, showsBackground: false)
                    }
                }
            }
            .padding(.leading, 26)
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    @ViewBuilder
    private var headerIcon: some View {
        if laneSnapshot.snapshot.isRunning && !laneSnapshot.isPendingConfirmation {
            OperationTimelineStateIcon(state: .running)
                .frame(width: 18)
        } else {
            Image(systemName: icon)
                .foregroundStyle(iconColor)
                .frame(width: 18)
        }
    }

    private var icon: String {
        if laneSnapshot.isPendingConfirmation {
            return "exclamationmark.triangle"
        }
        return laneSnapshot.snapshot.isRunning ? "hourglass" : "clock"
    }

    private var iconColor: Color {
        if laneSnapshot.isPendingConfirmation {
            return .yellow
        }
        return laneSnapshot.snapshot.isRunning ? Color.accentColor : Color.secondary
    }

    private func title(_ snapshot: ActivitySnapshot) -> String {
        if case .device(let activeDeviceID) = snapshot.scope,
           let profile = registry.profile(id: activeDeviceID) {
            return "\(snapshot.operationTitle) - \(profile.title)"
        }
        return snapshot.operationTitle
    }
}
