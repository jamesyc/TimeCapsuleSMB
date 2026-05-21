import SwiftUI

public struct ContentView: View {
    @StateObject private var backend: BackendClient
    @StateObject private var appReadinessStore: AppReadinessStore
    @StateObject private var connectionStore: ConnectionWorkflowStore
    @StateObject private var deployStore: DeployWorkflowStore
    @StateObject private var doctorStore: DoctorStore
    @StateObject private var maintenanceStore: MaintenanceStore
    @State private var selection: Screen = .connect
    @State private var diagnosticsPresented = false
    @State private var password = ""

    @MainActor
    public init() {
        let backend = BackendClient()
        _backend = StateObject(wrappedValue: backend)
        _appReadinessStore = StateObject(wrappedValue: AppReadinessStore(backend: backend))
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
                if case .blocked = appReadinessStore.state {
                    AppReadinessBlockedView(store: appReadinessStore) {
                        diagnosticsPresented = true
                    }
                } else {
                    AppReadinessBannerView(store: appReadinessStore) {
                        diagnosticsPresented = true
                    }
                    form
                }
                Divider()
                EventList(events: visibleEvents)
            }
            .toolbar {
                ToolbarItemGroup {
                    Button {
                        diagnosticsPresented = true
                    } label: {
                        Label("Diagnostics", systemImage: "wrench.and.screwdriver")
                    }
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
        .task {
            appReadinessStore.start()
        }
        .sheet(isPresented: $diagnosticsPresented) {
            AppDiagnosticsView(
                store: appReadinessStore,
                events: backend.events,
                helperPath: $backend.helperPath
            )
        }
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

    private var visibleEvents: [BackendEvent] {
        backend.events.filter { !["capabilities", "validate-install"].contains($0.operation) }
    }

}

private enum Screen: String, CaseIterable, Identifiable {
    case connect
    case deploy
    case doctor
    case maintenance
    case advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .connect: return L10n.string("screen.connect")
        case .deploy: return L10n.string("screen.deploy")
        case .doctor: return L10n.string("screen.doctor")
        case .maintenance: return L10n.string("screen.maintenance")
        case .advanced: return L10n.string("screen.advanced")
        }
    }

    var icon: String {
        switch self {
        case .connect: return "network"
        case .deploy: return "square.and.arrow.up"
        case .doctor: return "stethoscope"
        case .maintenance: return "wrench.and.screwdriver"
        case .advanced: return "exclamationmark.triangle"
        }
    }
}

private struct AppReadinessBannerView: View {
    @ObservedObject var store: AppReadinessStore
    let showDiagnostics: () -> Void

    var body: some View {
        switch store.state {
        case .idle, .ready:
            EmptyView()
        case .resolvingBundle, .checkingCapabilities, .validatingInstall:
            HStack(spacing: 10) {
                ProgressView()
                    .controlSize(.small)
                Text(title)
                    .font(.caption)
                if let stage = store.currentStage?.description ?? store.currentStage?.stage {
                    Text(stage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color.secondary.opacity(0.08))
        case .degraded(_, let issues):
            HStack(spacing: 10) {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(.yellow)
                Text(issues.first?.message ?? "TimeCapsuleSMB is running with warnings.")
                    .font(.caption)
                Spacer()
                Button("Diagnostics", action: showDiagnostics)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color.yellow.opacity(0.12))
        case .blocked:
            EmptyView()
        }
    }

    private var title: String {
        switch store.state.kind {
        case .resolvingBundle:
            return "Preparing app runtime"
        case .checkingCapabilities:
            return "Checking helper"
        case .validatingInstall:
            return "Validating bundled files"
        default:
            return ""
        }
    }
}

private struct AppReadinessBlockedView: View {
    @ObservedObject var store: AppReadinessStore
    let showDiagnostics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("TimeCapsuleSMB cannot start", systemImage: "exclamationmark.octagon")
                .font(.title2.weight(.semibold))
                .foregroundStyle(.red)
            if case .blocked(let issue) = store.state {
                Text(issue.message)
                Text(issue.recovery)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button {
                    store.start()
                } label: {
                    Label("Retry", systemImage: "arrow.clockwise")
                }
                .disabled(!store.canRetry)

                Button {
                    showDiagnostics()
                } label: {
                    Label("Diagnostics", systemImage: "wrench.and.screwdriver")
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct AppDiagnosticsView: View {
    @ObservedObject var store: AppReadinessStore
    let events: [BackendEvent]
    @Binding var helperPath: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Diagnostics")
                    .font(.title2.weight(.semibold))
                Spacer()
                Button("Done") {
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }

            TextField(L10n.string("field.helper"), text: $helperPath)

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow {
                    Text("State").foregroundStyle(.secondary)
                    Text(store.state.kind.rawValue)
                }
                if let capabilities = store.capabilities {
                    GridRow {
                        Text("Helper").foregroundStyle(.secondary)
                        Text(capabilities.helperVersion)
                    }
                    GridRow {
                        Text("Distribution").foregroundStyle(.secondary)
                        Text(capabilities.distributionRoot)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                if let validation = store.validation {
                    GridRow {
                        Text("Validation").foregroundStyle(.secondary)
                        Text(validation.summary)
                    }
                }
            }
            .font(.caption)

            if !store.issues.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Runtime Issues")
                        .font(.headline)
                    ForEach(store.issues) { issue in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(issue.message)
                            Text(issue.recovery)
                                .foregroundStyle(.secondary)
                        }
                        .font(.caption)
                    }
                }
            }

            Text("Backend Events")
                .font(.headline)
            EventList(events: events)
        }
        .padding()
        .frame(minWidth: 720, minHeight: 520)
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
