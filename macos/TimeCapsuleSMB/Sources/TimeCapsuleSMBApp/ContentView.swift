import SwiftUI

public struct ContentView: View {
    @StateObject private var backend: BackendClient
    @StateObject private var readinessStore: ReadinessStore
    @StateObject private var connectionStore: ConnectionWorkflowStore
    @StateObject private var deployStore: DeployWorkflowStore
    @StateObject private var doctorStore: DoctorStore
    @StateObject private var maintenanceStore: MaintenanceStore
    @State private var selection: Screen = .readiness
    @State private var password = ""

    @MainActor
    public init() {
        let backend = BackendClient()
        _backend = StateObject(wrappedValue: backend)
        _readinessStore = StateObject(wrappedValue: ReadinessStore(backend: backend))
        _connectionStore = StateObject(wrappedValue: ConnectionWorkflowStore(backend: backend))
        _deployStore = StateObject(wrappedValue: DeployWorkflowStore(backend: backend))
        _doctorStore = StateObject(wrappedValue: DoctorStore(backend: backend))
        _maintenanceStore = StateObject(wrappedValue: MaintenanceStore(backend: backend))
    }

    public var body: some View {
        NavigationSplitView {
            List(Screen.allCases, selection: $selection) { screen in
                Label(screen.title, systemImage: screen.icon)
                    .tag(screen)
            }
            .navigationTitle("TimeCapsuleSMB")
        } detail: {
            VStack(spacing: 0) {
                form
                Divider()
                EventList(events: backend.events)
            }
            .toolbar {
                ToolbarItemGroup {
                    Button {
                        clearActive()
                    } label: {
                        Label(L10n.string("toolbar.clear"), systemImage: "trash")
                    }
                    .disabled(backend.isRunning)
                    Button {
                        backend.cancel()
                    } label: {
                        Label(L10n.string("toolbar.cancel"), systemImage: "xmark.circle")
                    }
                    .disabled(!backend.canCancel)
                }
            }
        }
        .frame(minWidth: 980, minHeight: 680)
        .alert(
            backend.pendingConfirmation?.title ?? "",
            isPresented: confirmationPresented,
            presenting: backend.pendingConfirmation
        ) { confirmation in
            Button(confirmation.actionTitle, role: .destructive) {
                backend.confirmPending()
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                backend.pendingConfirmation = nil
            }
        } message: { confirmation in
            Text(confirmation.message)
        }
    }

    private var confirmationPresented: Binding<Bool> {
        Binding(
            get: { backend.pendingConfirmation != nil },
            set: { isPresented in
                if !isPresented {
                    backend.pendingConfirmation = nil
                }
            }
        )
    }

    @ViewBuilder
    private var form: some View {
        switch selection {
        case .readiness:
            ReadinessView(store: readinessStore, helperPath: $backend.helperPath)
        case .connect:
            ConnectView(store: connectionStore, password: $password)
        case .deploy:
            DeployView(store: deployStore, password: $password)
        case .doctor:
            DoctorView(store: doctorStore, password: $password)
        case .maintenance:
            MaintenanceView(store: maintenanceStore, password: $password)
        case .advanced:
            CommandPanel(title: L10n.string("screen.advanced")) {
                Text(L10n.string("advanced.flash_cli_only"))
                    .foregroundStyle(.secondary)
                Text(L10n.string("advanced.flash_help"))
                    .font(.system(.body, design: .monospaced))
            }
        }
    }

    private func clearActive() {
        switch selection {
        case .readiness:
            readinessStore.clear()
        case .connect:
            connectionStore.clear()
        case .deploy:
            deployStore.clear()
        case .doctor:
            doctorStore.clear()
        case .maintenance:
            maintenanceStore.clear()
        default:
            backend.clear()
        }
    }

}

private enum Screen: String, CaseIterable, Identifiable {
    case readiness
    case connect
    case deploy
    case doctor
    case maintenance
    case advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .readiness: return L10n.string("screen.readiness")
        case .connect: return L10n.string("screen.connect")
        case .deploy: return L10n.string("screen.deploy")
        case .doctor: return L10n.string("screen.doctor")
        case .maintenance: return L10n.string("screen.maintenance")
        case .advanced: return L10n.string("screen.advanced")
        }
    }

    var icon: String {
        switch self {
        case .readiness: return "checklist"
        case .connect: return "network"
        case .deploy: return "square.and.arrow.up"
        case .doctor: return "stethoscope"
        case .maintenance: return "wrench.and.screwdriver"
        case .advanced: return "exclamationmark.triangle"
        }
    }
}

private struct CommandPanel<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.title2.weight(.semibold))
            content
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct EventList: View {
    let events: [BackendEvent]

    var body: some View {
        List(events) { event in
            VStack(alignment: .leading, spacing: 4) {
                Text(event.summary)
                    .font(.body)
                if let payload = event.payload, event.type == "result" {
                    Text(payload.displayText)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(6)
                }
            }
            .padding(.vertical, 3)
        }
    }
}
