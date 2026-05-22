import SwiftUI

struct ActivityCompactView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore
    @State private var messageAnimationPhase = 0

    private let messageAnimationTimer = Timer.publish(
        every: ActivityProgressTextAnimator.frameInterval,
        on: .main,
        in: .common
    ).autoconnect()

    var body: some View {
        let snapshot = activityStore.snapshot
        let hasLatestMessage = hasLatestMessage(snapshot)
        HStack(spacing: 10) {
            Image(systemName: snapshot.isRunning ? "hourglass" : "checkmark.circle")
                .foregroundStyle(snapshot.isRunning ? Color.accentColor : Color.secondary)
            messageView(snapshot, hasLatestMessage: hasLatestMessage)
            Spacer()
            if let last = snapshot.timeline.last {
                Text(last.title)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(Color.secondary.opacity(0.06))
        .onChange(of: ActivityProgressTextAnimator.animationIdentity(for: snapshot)) { _ in
            messageAnimationPhase = 0
        }
        .onReceive(messageAnimationTimer) { _ in
            advanceMessageAnimation(for: snapshot)
        }
    }

    @ViewBuilder
    private func messageView(_ snapshot: ActivitySnapshot, hasLatestMessage: Bool) -> some View {
        if hasLatestMessage {
            VStack(alignment: .leading, spacing: 2) {
                Text(title(snapshot))
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                Text(latestMessage(snapshot))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(height: 30, alignment: .center)
        } else {
            Text(title(snapshot))
                .font(.caption.weight(.medium))
                .lineLimit(1)
                .frame(height: 30, alignment: .center)
        }
    }

    private func latestMessage(_ snapshot: ActivitySnapshot) -> String {
        ActivityProgressTextAnimator.message(
            snapshot.latestMessage,
            isRunning: snapshot.isRunning,
            phase: messageAnimationPhase
        ) ?? ""
    }

    private func advanceMessageAnimation(for snapshot: ActivitySnapshot) {
        guard ActivityProgressTextAnimator.animationIdentity(for: snapshot) != nil else {
            if messageAnimationPhase != 0 {
                messageAnimationPhase = 0
            }
            return
        }
        messageAnimationPhase = ActivityProgressTextAnimator.nextPhase(after: messageAnimationPhase)
    }

    private func title(_ snapshot: ActivitySnapshot) -> String {
        if case .device(let activeDeviceID) = snapshot.scope,
           let profile = registry.profile(id: activeDeviceID) {
            return "\(snapshot.operationTitle) - \(profile.title)"
        }
        return snapshot.operationTitle
    }

    private func hasLatestMessage(_ snapshot: ActivitySnapshot) -> Bool {
        guard let latestMessage = snapshot.latestMessage else {
            return false
        }
        return !latestMessage.isEmpty
    }
}

struct ActivityDetailView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore

    var body: some View {
        let snapshot = activityStore.snapshot
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .center, spacing: 12) {
                    Image(systemName: snapshot.isRunning ? "hourglass" : "clock")
                        .font(.title2)
                        .foregroundStyle(snapshot.isRunning ? Color.accentColor : Color.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(title(snapshot))
                            .font(.title2.weight(.semibold))
                        if let latestMessage = snapshot.latestMessage, !latestMessage.isEmpty {
                            Text(latestMessage)
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text(L10n.string("activity.timeline"))
                        .font(.headline)
                    if snapshot.timeline.isEmpty {
                        Text(L10n.string("activity.timeline.empty"))
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(snapshot.timeline) { item in
                            ActivityTimelineRow(item: item)
                        }
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
}

private struct ActivityTimelineRow: View {
    let item: OperationTimelineItem

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: itemIcon)
                .foregroundStyle(itemColor)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.body.weight(.medium))
                if let detail = item.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
        .padding(.vertical, 4)
    }

    private var itemIcon: String {
        switch item.state {
        case .pending:
            return "circle"
        case .running:
            return "arrow.right.circle"
        case .succeeded:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.triangle"
        case .failed:
            return "xmark.octagon"
        }
    }

    private var itemColor: Color {
        switch item.state {
        case .pending:
            return .secondary
        case .running:
            return .accentColor
        case .succeeded:
            return .green
        case .warning:
            return .yellow
        case .failed:
            return .red
        }
    }
}
