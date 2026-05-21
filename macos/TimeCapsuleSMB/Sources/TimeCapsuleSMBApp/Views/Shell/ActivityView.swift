import SwiftUI

struct ActivityCompactView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore

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
    }

    @ViewBuilder
    private func messageView(_ snapshot: ActivitySnapshot, hasLatestMessage: Bool) -> some View {
        if hasLatestMessage {
            VStack(alignment: .leading, spacing: 2) {
                Text(title(snapshot))
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                Text(snapshot.latestMessage ?? "")
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
