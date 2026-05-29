import SwiftUI

struct DeviceListOverviewView: View {
    @ObservedObject var appStore: AppStore
    let addDiscoveredDevice: (DiscoveredDevice) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                savedDevicesSection
                discoverySection
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
    }

    @ViewBuilder
    private var savedDevicesSection: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(appStore.deviceRegistry.profiles.isEmpty
                ? L10n.string("overview.empty.title")
                : L10n.string("overview.saved_devices.title"))
            .font(.title2.weight(.semibold))

            if appStore.deviceRegistry.profiles.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text(L10n.string("overview.empty.message"))
                        .foregroundStyle(.secondary)
                    Button {
                        appStore.showAddDevice()
                    } label: {
                        Label(L10n.string("sidebar.add_airport_device"), systemImage: "plus.circle")
                    }
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
                                Text(profile.addressSummary.isEmpty ? profile.displayTarget : profile.addressSummary)
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
    }

    private var discoverySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(L10n.string("overview.discovery.title"))
                    .font(.headline)
                Spacer()
                Text(appStore.deviceDiscovery.state.title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button {
                    appStore.deviceDiscovery.refresh()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .disabled(appStore.backend.isRunning)
                .help(L10n.string("overview.discovery.refresh"))
            }

            discoveryContent
        }
    }

    @ViewBuilder
    private var discoveryContent: some View {
        switch appStore.deviceDiscovery.state {
        case .idle, .waitingForReadiness:
            Text(L10n.string("overview.discovery.waiting"))
                .foregroundStyle(.secondary)
        case .discovering:
            ProgressView(L10n.string("overview.discovery.discovering"))
        case .paused:
            Text(L10n.string("overview.discovery.paused"))
                .foregroundStyle(.secondary)
        case .readinessBlocked:
            Text(L10n.string("overview.discovery.readiness_blocked"))
                .foregroundStyle(.secondary)
        case .failed:
            VStack(alignment: .leading, spacing: 6) {
                Text(appStore.deviceDiscovery.error?.message ?? L10n.string("overview.discovery.failed"))
                    .foregroundStyle(.red)
                Button(L10n.string("overview.discovery.refresh")) {
                    appStore.deviceDiscovery.refresh()
                }
            }
        case .empty:
            Text(L10n.string("overview.discovery.empty"))
                .foregroundStyle(.secondary)
        case .ready:
            let unsaved = appStore.deviceDiscovery.unsavedDevices
            let saved = appStore.deviceDiscovery.savedDevices
            if unsaved.isEmpty && saved.isEmpty {
                Text(L10n.string("overview.discovery.empty"))
                    .foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(unsaved) { device in
                        OverviewDiscoveredDeviceRow(
                            device: device,
                            statusText: L10n.string("overview.discovery.unsaved"),
                            actionTitle: L10n.string("overview.discovery.add")
                        ) {
                            addDiscoveredDevice(device)
                        }
                        Divider()
                    }
                    ForEach(saved) { device in
                        OverviewDiscoveredDeviceRow(
                            device: device,
                            statusText: L10n.string("overview.discovery.saved"),
                            actionTitle: nil,
                            action: nil
                        )
                        Divider()
                    }
                }
            }
        }
    }
}

private struct OverviewDiscoveredDeviceRow: View {
    let device: DiscoveredDevice
    let statusText: String
    let actionTitle: String?
    let action: (() -> Void)?

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: "antenna.radiowaves.left.and.right")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(device.name)
                    .font(.body.weight(.medium))
                HStack(spacing: 6) {
                    Text(device.addressSummary.isEmpty ? device.connectionTarget : device.addressSummary)
                    if !device.discoveryModelText.isEmpty {
                        Text(device.discoveryModelText)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            Text(statusText)
                .font(.caption)
                .foregroundStyle(.secondary)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
            }
        }
        .padding(.vertical, 8)
    }
}
