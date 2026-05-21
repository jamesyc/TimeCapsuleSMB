import SwiftUI

struct ActivityCompactView: View {
    @ObservedObject var activityStore: ActivityStore
    @ObservedObject var registry: DeviceRegistryStore

    var body: some View {
        let snapshot = activityStore.snapshot
        HStack(spacing: 10) {
            Image(systemName: snapshot.isRunning ? "hourglass" : "checkmark.circle")
                .foregroundStyle(snapshot.isRunning ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(title(snapshot))
                    .font(.caption.weight(.medium))
                if let latest = snapshot.latestMessage, !latest.isEmpty {
                    Text(latest)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }
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

    private func title(_ snapshot: ActivitySnapshot) -> String {
        if case .device(let activeDeviceID) = snapshot.scope,
           let profile = registry.profile(id: activeDeviceID) {
            return "\(snapshot.operationTitle) - \(profile.title)"
        }
        return snapshot.operationTitle
    }
}
