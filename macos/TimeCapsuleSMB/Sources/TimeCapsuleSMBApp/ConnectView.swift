import SwiftUI

struct ConnectView: View {
    @ObservedObject var store: ConnectionWorkflowStore
    @Binding var password: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("panel.connect"))
                .font(.title2.weight(.semibold))

            HStack {
                TextField(L10n.string("field.host"), text: $store.manualHost)
                SecureField(L10n.string("field.password"), text: $password)
                TextField(L10n.string("field.bonjour_timeout"), text: $store.bonjourTimeout)
                    .frame(width: 180)
            }

            Toggle(L10n.string("toggle.enable_debug_logging"), isOn: $store.debugLogging)

            HStack {
                Button {
                    store.runDiscover()
                } label: {
                    Label(L10n.string("button.discover"), systemImage: "network")
                }
                .disabled(store.isRunning || store.bonjourTimeoutValue == nil)

                Button {
                    store.runConfigure(password: password)
                } label: {
                    Label(L10n.string("button.configure"), systemImage: "lock.open")
                }
                .disabled(!store.canConfigure(password: password))

                Label(store.state.title, systemImage: statusIcon)
                    .foregroundStyle(statusColor)
            }

            if let stage = store.currentStage {
                HStack(spacing: 8) {
                    Text(stage.stage)
                        .font(.system(.caption, design: .monospaced))
                    if let description = stage.description {
                        Text(description)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            if !store.devices.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(store.devices) { device in
                        Button {
                            store.select(device)
                        } label: {
                            DeviceRow(
                                device: device,
                                selected: store.selectedDeviceID == device.id
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
            }

            if let configuredDevice = store.configuredDevice {
                ConfiguredDeviceView(device: configuredDevice)
            }

            if let error = store.error {
                ErrorRecoveryView(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusIcon: String {
        switch store.state {
        case .idle:
            return "circle"
        case .discovering, .configuring:
            return "hourglass"
        case .discoveryReady, .configured:
            return "checkmark.circle"
        case .discoveryEmpty:
            return "magnifyingglass"
        case .discoveryFailed, .configureFailed:
            return "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch store.state {
        case .discoveryReady, .configured:
            return .green
        case .discoveryFailed, .configureFailed:
            return .red
        default:
            return .secondary
        }
    }
}

private struct DeviceRow: View {
    let device: DiscoveredDevice
    let selected: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(selected ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(device.name)
                    .font(.body.weight(.medium))
                HStack(spacing: 8) {
                    if !device.host.isEmpty {
                        Text(device.host)
                    }
                    if !device.hostname.isEmpty {
                        Text(device.hostname)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            if let model = device.model {
                Text(model)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let syap = device.syap {
                Text("syAP \(syap)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 8)
        .background(selected ? Color.accentColor.opacity(0.12) : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct ConfiguredDeviceView: View {
    let device: ConfiguredDeviceState

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow {
                Text("Configured Host")
                    .foregroundStyle(.secondary)
                Text(device.host)
            }
            GridRow {
                Text("Config")
                    .foregroundStyle(.secondary)
                Text(device.configPath)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            if let model = device.model {
                GridRow {
                    Text("Model")
                        .foregroundStyle(.secondary)
                    Text(model)
                }
            }
            if let syap = device.syap {
                GridRow {
                    Text("syAP")
                        .foregroundStyle(.secondary)
                    Text(syap)
                }
            }
            if let compatibility = device.compatibility {
                GridRow {
                    Text("Payload")
                        .foregroundStyle(.secondary)
                    Text(compatibility.payloadFamily ?? "unknown")
                }
            }
        }
        .font(.caption)
    }
}

private struct ErrorRecoveryView: View {
    let error: BackendErrorViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(error.recovery?.title ?? error.code)
                .font(.body.weight(.medium))
            Text(error.message)
                .font(.caption)
            if let recovery = error.recovery, !recovery.actions.isEmpty {
                ForEach(recovery.actions, id: \.self) { action in
                    Text(action)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .foregroundStyle(.red)
    }
}
