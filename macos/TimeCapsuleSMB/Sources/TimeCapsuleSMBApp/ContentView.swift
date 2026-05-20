import SwiftUI

public struct ContentView: View {
    @StateObject private var backend = BackendClient()
    @State private var selection: Screen = .readiness
    @State private var host = "root@192.168.x.x"
    @State private var password = ""
    @State private var repairPath = ""
    @State private var volume = ""
    @State private var nbnsEnabled = true
    @State private var noReboot = false
    @State private var dryRun = true
    @State private var configureDebugLogging = false
    @State private var deployDebugLogging = false
    @State private var mountWait = "30"
    @State private var bonjourTimeout = "6"
    @State private var noWait = false
    @State private var pendingConfirmation: PendingConfirmation?

    public init() {}

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
                        backend.clear()
                    } label: {
                        Label(L10n.string("toolbar.clear"), systemImage: "trash")
                    }
                    .disabled(backend.isRunning)
                    Button {
                        backend.cancel()
                    } label: {
                        Label(L10n.string("toolbar.cancel"), systemImage: "xmark.circle")
                    }
                    .disabled(!backend.isRunning)
                }
            }
        }
        .frame(minWidth: 980, minHeight: 680)
        .alert(
            pendingConfirmation?.title ?? "",
            isPresented: confirmationPresented,
            presenting: pendingConfirmation
        ) { confirmation in
            Button(confirmation.actionTitle, role: .destructive) {
                backend.run(operation: confirmation.operation, params: confirmation.params)
                pendingConfirmation = nil
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                pendingConfirmation = nil
            }
        } message: { confirmation in
            Text(confirmation.message)
        }
    }

    private var confirmationPresented: Binding<Bool> {
        Binding(
            get: { pendingConfirmation != nil },
            set: { isPresented in
                if !isPresented {
                    pendingConfirmation = nil
                }
            }
        )
    }

    @ViewBuilder
    private var form: some View {
        switch selection {
        case .readiness:
            CommandPanel(title: L10n.string("screen.readiness")) {
                TextField(L10n.string("field.helper"), text: $backend.helperPath)
                HStack {
                    runButton(L10n.string("button.paths"), icon: "folder", operation: "paths")
                    runButton(L10n.string("button.validate"), icon: "checkmark.seal", operation: "validate-install")
                }
            }
        case .connect:
            CommandPanel(title: L10n.string("panel.connect")) {
                TextField(L10n.string("field.host"), text: $host)
                SecureField(L10n.string("field.password"), text: $password)
                TextField(L10n.string("field.bonjour_timeout"), text: $bonjourTimeout)
                Toggle(L10n.string("toggle.enable_debug_logging"), isOn: $configureDebugLogging)
                HStack {
                    runButton(
                        L10n.string("button.discover"),
                        icon: "network",
                        operation: "discover",
                        params: OperationParams.discover(timeout: numberDouble(bonjourTimeout, default: 6))
                    )
                    Button {
                        backend.run(
                            operation: "configure",
                            params: OperationParams.configure(
                                host: host,
                                password: password,
                                debugLogging: configureDebugLogging
                            )
                        )
                    } label: {
                        Label(L10n.string("button.configure"), systemImage: "lock.open")
                    }
                    .disabled(backend.isRunning || password.isEmpty)
                }
            }
        case .deploy:
            CommandPanel(title: L10n.string("screen.deploy")) {
                Toggle(L10n.string("toggle.enable_nbns"), isOn: $nbnsEnabled)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $noWait)
                Toggle(L10n.string("toggle.dry_run"), isOn: $dryRun)
                Toggle(L10n.string("toggle.force_debug_logging"), isOn: $deployDebugLogging)
                TextField(L10n.string("field.mount_wait"), text: $mountWait)
                Button {
                    if dryRun {
                        backend.run(
                            operation: "deploy",
                            params: OperationParams.deployPlan(
                                noReboot: noReboot,
                                noWait: noWait,
                                nbnsEnabled: nbnsEnabled,
                                debugLogging: deployDebugLogging,
                                mountWait: numberDouble(mountWait, default: 30)
                            )
                        )
                    } else {
                        pendingConfirmation = .deploy(
                            noReboot: noReboot,
                            nbnsEnabled: nbnsEnabled,
                            debugLogging: deployDebugLogging,
                            mountWait: numberDouble(mountWait, default: 30),
                            noWait: noWait
                        )
                    }
                } label: {
                    Label(
                        dryRun ? L10n.string("button.plan_deploy") : L10n.string("button.deploy"),
                        systemImage: dryRun ? "doc.text.magnifyingglass" : "square.and.arrow.up"
                    )
                }
                .disabled(backend.isRunning)
            }
        case .doctor:
            CommandPanel(title: L10n.string("screen.doctor")) {
                TextField(L10n.string("field.bonjour_timeout"), text: $bonjourTimeout)
                runButton(
                    L10n.string("button.run_doctor"),
                    icon: "stethoscope",
                    operation: "doctor",
                    params: OperationParams.doctor(bonjourTimeout: numberDouble(bonjourTimeout, default: 6))
                )
            }
        case .maintenance:
            CommandPanel(title: L10n.string("screen.maintenance")) {
                TextField(L10n.string("field.repair_xattrs_path"), text: $repairPath)
                TextField(L10n.string("field.fsck_volume"), text: $volume)
                TextField(L10n.string("field.mount_wait"), text: $mountWait)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $noWait)
                HStack {
                    Button {
                        pendingConfirmation = .activate()
                    } label: {
                        Label(L10n.string("button.activate"), systemImage: "power")
                    }
                    .disabled(backend.isRunning)
                    runButton(
                        L10n.string("button.uninstall_plan"),
                        icon: "xmark.bin",
                        operation: "uninstall",
                        params: OperationParams.uninstallPlan(
                            noReboot: noReboot,
                            noWait: noWait,
                            mountWait: numberDouble(mountWait, default: 30)
                        )
                    )
                    Button {
                        pendingConfirmation = .uninstall(
                            noReboot: noReboot,
                            mountWait: numberDouble(mountWait, default: 30),
                            noWait: noWait
                        )
                    } label: {
                        Label(L10n.string("button.uninstall"), systemImage: "xmark.bin.fill")
                    }
                    .disabled(backend.isRunning)
                }
                HStack {
                    runButton(
                        L10n.string("button.list_fsck_volumes"),
                        icon: "list.bullet.rectangle",
                        operation: "fsck",
                        params: OperationParams.fsckList(mountWait: numberDouble(mountWait, default: 30))
                    )
                    runButton(
                        L10n.string("button.plan_fsck"),
                        icon: "doc.text.magnifyingglass",
                        operation: "fsck",
                        params: OperationParams.fsckPlan(
                            volume: volume,
                            noReboot: noReboot,
                            noWait: noWait,
                            mountWait: numberDouble(mountWait, default: 30)
                        )
                    )
                    Button {
                        pendingConfirmation = .fsck(
                            volume: volume,
                            noReboot: noReboot,
                            mountWait: numberDouble(mountWait, default: 30),
                            noWait: noWait
                        )
                    } label: {
                        Label(L10n.string("button.run_fsck"), systemImage: "externaldrive.badge.checkmark")
                    }
                    .disabled(backend.isRunning)
                }
                HStack {
                    Button {
                        backend.run(
                            operation: "repair-xattrs",
                            params: OperationParams.repairXattrsScan(path: repairPath)
                        )
                    } label: {
                        Label(L10n.string("button.scan_xattrs"), systemImage: "wand.and.stars")
                    }
                    .disabled(backend.isRunning || repairPath.isEmpty)
                    Button {
                        pendingConfirmation = .repairXattrs(path: repairPath)
                    } label: {
                        Label(L10n.string("button.repair_xattrs"), systemImage: "wand.and.stars.inverse")
                    }
                    .disabled(backend.isRunning || repairPath.isEmpty)
                }
            }
        case .advanced:
            CommandPanel(title: L10n.string("screen.advanced")) {
                Text(L10n.string("advanced.flash_cli_only"))
                    .foregroundStyle(.secondary)
                Text(L10n.string("advanced.flash_help"))
                    .font(.system(.body, design: .monospaced))
            }
        }
    }

    private func runButton(
        _ title: String,
        icon: String,
        operation: String,
        params: [String: JSONValue] = [:]
    ) -> some View {
        Button {
            backend.run(operation: operation, params: params)
        } label: {
            Label(title, systemImage: icon)
        }
        .disabled(backend.isRunning)
    }

    private func numberDouble(_ text: String, default defaultValue: Double) -> Double {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return Double(trimmed) ?? defaultValue
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
