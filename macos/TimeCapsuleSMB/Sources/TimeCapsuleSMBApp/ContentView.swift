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
                        Label("Clear", systemImage: "trash")
                    }
                    .disabled(backend.isRunning)
                    Button {
                        backend.cancel()
                    } label: {
                        Label("Cancel", systemImage: "xmark.circle")
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
            Button("Cancel", role: .cancel) {
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
            CommandPanel(title: "Readiness") {
                TextField("Helper", text: $backend.helperPath)
                HStack {
                    runButton("Paths", icon: "folder", operation: "paths")
                    runButton("Validate", icon: "checkmark.seal", operation: "validate-install")
                }
            }
        case .connect:
            CommandPanel(title: "Discover And Connect") {
                TextField("Host", text: $host)
                SecureField("Password", text: $password)
                TextField("Bonjour timeout seconds", text: $bonjourTimeout)
                Toggle("Enable Debug Logging", isOn: $configureDebugLogging)
                HStack {
                    runButton("Discover", icon: "network", operation: "discover", params: [
                        "timeout": numberValue(bonjourTimeout, default: 6)
                    ])
                    Button {
                        var params: [String: JSONValue] = [
                            "host": .string(host),
                            "password": .string(password)
                        ]
                        if configureDebugLogging {
                            params["debug_logging"] = .bool(true)
                        }
                        backend.run(operation: "configure", params: params)
                    } label: {
                        Label("Configure", systemImage: "lock.open")
                    }
                    .disabled(backend.isRunning || password.isEmpty)
                }
            }
        case .deploy:
            CommandPanel(title: "Deploy") {
                Toggle("Enable NBNS", isOn: $nbnsEnabled)
                Toggle("No Reboot", isOn: $noReboot)
                Toggle("No Wait", isOn: $noWait)
                Toggle("Dry Run", isOn: $dryRun)
                Toggle("Force Debug Logging", isOn: $deployDebugLogging)
                TextField("Mount wait seconds", text: $mountWait)
                Button {
                    if dryRun {
                        backend.run(operation: "deploy", params: [
                            "dry_run": .bool(true),
                            "no_reboot": .bool(noReboot),
                            "no_wait": .bool(noWait),
                            "nbns_enabled": .bool(nbnsEnabled),
                            "debug_logging": .bool(deployDebugLogging),
                            "mount_wait": numberValue(mountWait, default: 30)
                        ])
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
                    Label(dryRun ? "Plan Deploy" : "Deploy", systemImage: dryRun ? "doc.text.magnifyingglass" : "square.and.arrow.up")
                }
                .disabled(backend.isRunning)
            }
        case .doctor:
            CommandPanel(title: "Doctor") {
                TextField("Bonjour timeout seconds", text: $bonjourTimeout)
                runButton("Run Doctor", icon: "stethoscope", operation: "doctor", params: [
                    "bonjour_timeout": numberValue(bonjourTimeout, default: 6)
                ])
            }
        case .maintenance:
            CommandPanel(title: "Maintenance") {
                TextField("Repair xattrs path", text: $repairPath)
                TextField("fsck volume, optional", text: $volume)
                TextField("Mount wait seconds", text: $mountWait)
                Toggle("No Reboot", isOn: $noReboot)
                Toggle("No Wait", isOn: $noWait)
                HStack {
                    Button {
                        pendingConfirmation = .activate()
                    } label: {
                        Label("Activate", systemImage: "power")
                    }
                    .disabled(backend.isRunning)
                    runButton("Uninstall Plan", icon: "xmark.bin", operation: "uninstall", params: [
                        "dry_run": .bool(true),
                        "no_reboot": .bool(noReboot),
                        "no_wait": .bool(noWait),
                        "mount_wait": numberValue(mountWait, default: 30)
                    ])
                    Button {
                        pendingConfirmation = .uninstall(
                            noReboot: noReboot,
                            mountWait: numberDouble(mountWait, default: 30),
                            noWait: noWait
                        )
                    } label: {
                        Label("Uninstall", systemImage: "xmark.bin.fill")
                    }
                    .disabled(backend.isRunning)
                }
                HStack {
                    runButton("List fsck Volumes", icon: "list.bullet.rectangle", operation: "fsck", params: [
                        "list_volumes": .bool(true),
                        "mount_wait": numberValue(mountWait, default: 30)
                    ])
                    runButton("Plan fsck", icon: "doc.text.magnifyingglass", operation: "fsck", params: [
                        "dry_run": .bool(true),
                        "no_reboot": .bool(noReboot),
                        "no_wait": .bool(noWait),
                        "mount_wait": numberValue(mountWait, default: 30),
                        "volume": .string(volume)
                    ])
                    Button {
                        pendingConfirmation = .fsck(
                            volume: volume,
                            noReboot: noReboot,
                            mountWait: numberDouble(mountWait, default: 30),
                            noWait: noWait
                        )
                    } label: {
                        Label("Run fsck", systemImage: "externaldrive.badge.checkmark")
                    }
                    .disabled(backend.isRunning)
                }
                HStack {
                    Button {
                        backend.run(operation: "repair-xattrs", params: [
                            "path": .string(repairPath),
                            "dry_run": .bool(true)
                        ])
                    } label: {
                        Label("Scan xattrs", systemImage: "wand.and.stars")
                    }
                    .disabled(backend.isRunning || repairPath.isEmpty)
                    Button {
                        pendingConfirmation = .repairXattrs(path: repairPath)
                    } label: {
                        Label("Repair xattrs", systemImage: "wand.and.stars.inverse")
                    }
                    .disabled(backend.isRunning || repairPath.isEmpty)
                }
            }
        case .advanced:
            CommandPanel(title: "Advanced") {
                Text("Flash backup, patch, and restore remain CLI-only in this version.")
                    .foregroundStyle(.secondary)
                Text("Use `.venv/bin/tcapsule flash --help` for firmware operations.")
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

    private func numberValue(_ text: String, default defaultValue: Double) -> JSONValue {
        .number(numberDouble(text, default: defaultValue))
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
        case .readiness: return "Readiness"
        case .connect: return "Connect"
        case .deploy: return "Deploy"
        case .doctor: return "Doctor"
        case .maintenance: return "Maintenance"
        case .advanced: return "Advanced"
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
