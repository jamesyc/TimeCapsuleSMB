import SwiftUI

struct DeviceListOverviewView: View {
    @ObservedObject var appStore: AppStore

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(appStore.deviceRegistry.profiles.isEmpty ? L10n.string("overview.empty.title") : L10n.string("sidebar.all_time_capsules"))
                .font(.title2.weight(.semibold))
            if appStore.deviceRegistry.profiles.isEmpty {
                Text(L10n.string("overview.empty.message"))
                    .foregroundStyle(.secondary)
                Button {
                    appStore.showAddDevice()
                } label: {
                    Label(L10n.string("sidebar.add_time_capsule"), systemImage: "plus.circle")
                }
            } else {
                ForEach(appStore.deviceRegistry.profiles) { profile in
                    let summary = appStore.dashboardSummary(for: profile)
                    Button {
                        appStore.select(profile)
                    } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(profile.title)
                                    .font(.body.weight(.medium))
                                Text(profile.host)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Label(summary.displayStatus.title, systemImage: summary.displayStatus.systemImage)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                    Divider()
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
