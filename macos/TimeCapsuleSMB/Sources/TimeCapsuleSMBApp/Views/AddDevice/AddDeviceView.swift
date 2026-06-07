import SwiftUI

struct AddDeviceView: View {
    @ObservedObject var store: AddDeviceFlowStore

    var body: some View {
        ZStack {
            content
            if let progress = AddDeviceProgressPresentation(state: store.state, currentStage: store.currentStage) {
                BlockingProgressOverlay(progress: progress)
            }
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 14) {
            topSection
            if store.entryMode == .manual {
                connectionControls
                Spacer(minLength: 0)
            } else {
                deviceResultsSection
                connectionControls
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .disabled(AddDeviceProgressPresentation(state: store.state, currentStage: store.currentStage) != nil)
    }

    private var topSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text(L10n.string("add_device.title"))
                    .font(.title2.weight(.semibold))
                Spacer()
                Picker(L10n.string("add_device.connection_method"), selection: Binding(
                    get: { store.entryMode },
                    set: { store.setEntryMode($0) }
                )) {
                    ForEach(AddDeviceEntryMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 360)
            }

            HStack {
                if store.entryMode == .discover {
                    Text(discoveryStatusText)
                        .foregroundStyle(.secondary)
                    Button {
                        store.runDiscover()
                    } label: {
                        Label(L10n.string("button.discover"), systemImage: "network")
                    }
                    .disabled(store.isRunning || store.bonjourTimeoutValue == nil)
                }
                Label(store.state.title, systemImage: statusIcon)
                    .foregroundStyle(statusColor)
            }
            .frame(minHeight: 28, alignment: .center)
        }
    }

    private var discoveryStatusText: String {
        guard let stage = store.currentStage else {
            return L10n.string("add_device.discover.placeholder")
        }
        return OperationTimelineBuilder.stageDetail(for: stage.operation, stage: stage.stage, fallback: nil)
            ?? OperationTimelineBuilder.stageTitle(for: stage.operation, stage: stage.stage)
    }

    private var deviceResultsSection: some View {
        Group {
            if store.entryMode == .discover && !store.devices.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text(L10n.string("add_device.discovered_devices"))
                        .font(.headline)

                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(store.devices) { device in
                                Button {
                                    store.select(device)
                                } label: {
                                    DeviceCandidateRow(device: device, selected: store.selectedDeviceID == device.id)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .scrollIndicators(.visible)
                    .frame(maxWidth: .infinity)
                }
            } else {
                Spacer(minLength: 24)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var connectionControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TextField(L10n.string("add_device.host_or_ip"), text: Binding(
                    get: { store.hostFieldText },
                    set: { store.manualHost = $0 }
                ))
                .disabled(!store.isHostFieldEditable)
                SecureField(L10n.string("add_device.password"), text: $store.password)
                    .onSubmit {
                        guard store.canConfigure else {
                            return
                        }
                        store.runConfigure()
                    }
            }

            Toggle(L10n.string("toggle.smb_browse_compatibility"), isOn: $store.smbBrowseCompatibility)

            HStack {
                Button {
                    store.runConfigure()
                } label: {
                    Label(L10n.string("add_device.save_device"), systemImage: "checkmark.circle")
                }
                .disabled(!store.canConfigure)

                Button {
                    store.reset()
                } label: {
                    Label(L10n.string("add_device.reset"), systemImage: "arrow.counterclockwise")
                }
                .disabled(store.isRunning)
            }

            if let profile = store.savedProfile {
                Label(L10n.format("add_device.saved", profile.title), systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
            }

            if let error = store.error {
                ErrorBlock(error: error)
            }
        }
    }

    private var statusIcon: String {
        switch store.state {
        case .idle, .manualEntry, .passwordEntry:
            return "circle"
        case .discovering, .configuring, .savingProfile:
            return "hourglass"
        case .awaitingConfirmation:
            return "questionmark.circle"
        case .discoveryReady, .saved:
            return "checkmark.circle"
        case .discoveryEmpty:
            return "magnifyingglass"
        case .authFailed, .unsupported, .failed:
            return "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch store.state {
        case .discoveryReady, .saved:
            return .green
        case .awaitingConfirmation:
            return .yellow
        case .authFailed, .unsupported, .failed:
            return .red
        default:
            return .secondary
        }
    }
}

private struct DeviceCandidateRow: View {
    let device: DiscoveredDevice
    let selected: Bool

    var body: some View {
        HStack {
            Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(selected ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading) {
                Text(device.name)
                Text([device.hostname, device.addressSummary].filter { !$0.isEmpty }.joined(separator: "  "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(L10n.format("add_device.setup_target", device.connectionTarget))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer()
            if !device.discoveryModelText.isEmpty {
                Text(device.discoveryModelText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 6)
    }
}
