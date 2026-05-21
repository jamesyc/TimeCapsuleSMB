import AppKit
import SwiftUI

public struct ContentView: View {
    @StateObject private var appStore: AppStore
    @StateObject private var addDeviceStore: AddDeviceFlowStore
    @StateObject private var dashboardStore: DashboardStore
    @State private var diagnosticsPresented = false
    @State private var replacementPassword = ""
    @State private var profilePendingDeletion: DeviceProfile?
    @State private var deleteErrorMessage: String?

    @MainActor
    public init() {
        let appStore = AppStore()
        _appStore = StateObject(wrappedValue: appStore)
        _addDeviceStore = StateObject(wrappedValue: AddDeviceFlowStore(
            coordinator: appStore.operationCoordinator,
            registry: appStore.deviceRegistry,
            passwordStore: appStore.passwordStore
        ))
        _dashboardStore = StateObject(wrappedValue: DashboardStore(appStore: appStore))
    }

    public var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            VStack(spacing: 0) {
                if case .blocked = appStore.appReadinessStore.state {
                    AppReadinessBlockedView(store: appStore.appReadinessStore) {
                        diagnosticsPresented = true
                    }
                } else {
                    AppReadinessBannerView(store: appStore.appReadinessStore) {
                        diagnosticsPresented = true
                    }
                    detail
                    Divider()
                    ActivityCompactView(
                        activityStore: appStore.activityStore,
                        registry: appStore.deviceRegistry
                    )
                }
            }
            .toolbar {
                ToolbarItemGroup {
                    Button {
                        appStore.showAddDevice()
                    } label: {
                        Label("Add", systemImage: "plus")
                    }
                    Button {
                        diagnosticsPresented = true
                    } label: {
                        Label("Diagnostics", systemImage: "wrench.and.screwdriver")
                    }
                    Button {
                        if let profile = appStore.selectedProfile {
                            profilePendingDeletion = profile
                        } else {
                            appStore.operationCoordinator.clear()
                        }
                    } label: {
                        Label(appStore.selectedProfile == nil ? L10n.string("toolbar.clear") : "Forget", systemImage: "trash")
                    }
                    .disabled(appStore.backend.isRunning)
                    Button {
                        appStore.operationCoordinator.cancel()
                    } label: {
                        Label(L10n.string("toolbar.cancel"), systemImage: "xmark.circle")
                    }
                    .disabled(!appStore.backend.canCancel)
                }
            }
        }
        .frame(minWidth: 1080, minHeight: 720)
        .task {
            appStore.start()
        }
        .onChange(of: addDeviceStore.savedProfile) { profile in
            guard let profile else { return }
            appStore.select(profile)
        }
        .sheet(isPresented: $diagnosticsPresented) {
            AppDiagnosticsView(
                store: appStore.appReadinessStore,
                events: appStore.backend.events,
                helperPath: Binding(
                    get: { appStore.backend.helperPath },
                    set: { appStore.backend.helperPath = $0 }
                )
            )
        }
        .confirmationDialog(
            "Forget Time Capsule?",
            isPresented: deleteConfirmationPresented,
            presenting: profilePendingDeletion
        ) { profile in
            Button("Forget \(profile.title)", role: .destructive) {
                do {
                    try appStore.forget(profile)
                    profilePendingDeletion = nil
                } catch {
                    deleteErrorMessage = error.localizedDescription
                }
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                profilePendingDeletion = nil
            }
        } message: { profile in
            Text("Remove \(profile.title) from this Mac. This does not uninstall SMB from the Time Capsule.")
        }
        .alert("Could Not Forget Time Capsule", isPresented: deleteErrorPresented) {
            Button("OK", role: .cancel) {
                deleteErrorMessage = nil
            }
        } message: {
            Text(deleteErrorMessage ?? "")
        }
        .alert(
            appStore.backend.pendingConfirmation?.title ?? "",
            isPresented: confirmationPresented,
            presenting: appStore.backend.pendingConfirmation
        ) { confirmation in
            Button(confirmation.actionTitle, role: .destructive) {
                appStore.backend.confirmPending()
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                appStore.backend.pendingConfirmation = nil
            }
        } message: { confirmation in
            Text(confirmation.message)
        }
    }

    private var deleteConfirmationPresented: Binding<Bool> {
        Binding(
            get: { profilePendingDeletion != nil },
            set: { isPresented in
                if !isPresented {
                    profilePendingDeletion = nil
                }
            }
        )
    }

    private var deleteErrorPresented: Binding<Bool> {
        Binding(
            get: { deleteErrorMessage != nil },
            set: { isPresented in
                if !isPresented {
                    deleteErrorMessage = nil
                }
            }
        )
    }

    private var confirmationPresented: Binding<Bool> {
        Binding(
            get: { appStore.backend.pendingConfirmation != nil },
            set: { isPresented in
                if !isPresented {
                    appStore.backend.pendingConfirmation = nil
                }
            }
        )
    }

    private var sidebarSelection: Binding<String?> {
        Binding(
            get: {
                if appStore.showingAddDevice {
                    return "add"
                }
                if let selectedDeviceID = appStore.selectedDeviceID {
                    return "device:\(selectedDeviceID)"
                }
                return "all"
            },
            set: { value in
                guard let value else { return }
                if value == "add" {
                    appStore.showAddDevice()
                } else if value == "all" {
                    appStore.selectedDeviceID = nil
                    appStore.showingAddDevice = false
                } else if value.hasPrefix("device:") {
                    let id = String(value.dropFirst("device:".count))
                    if let profile = appStore.deviceRegistry.profile(id: id) {
                        appStore.select(profile)
                    }
                }
            }
        )
    }

    private var sidebar: some View {
        List(selection: sidebarSelection) {
            Label("All Time Capsules", systemImage: "externaldrive.connected.to.line.below")
                .tag("all")

            Section("Devices") {
                ForEach(appStore.deviceRegistry.profiles) { profile in
                    DeviceSidebarRow(
                        profile: profile,
                        summary: appStore.dashboardSummary(for: profile)
                    )
                        .tag("device:\(profile.id)")
                }
            }

            Section {
                Label("Add Time Capsule", systemImage: "plus.circle")
                    .tag("add")
            }
        }
        .navigationTitle("TimeCapsuleSMB")
        .navigationSplitViewColumnWidth(min: 240, ideal: 280, max: 360)
    }

    @ViewBuilder
    private var detail: some View {
        if appStore.showingAddDevice {
            AddDeviceView(store: addDeviceStore)
        } else if let profile = appStore.selectedProfile {
            DeviceDashboardView(
                profile: profile,
                dashboardStore: dashboardStore,
                appStore: appStore,
                replacementPassword: $replacementPassword,
                showDiagnostics: {
                    diagnosticsPresented = true
                }
            )
        } else {
            DeviceListOverviewView(appStore: appStore)
        }
    }
}

private struct DeviceListOverviewView: View {
    @ObservedObject var appStore: AppStore

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(appStore.deviceRegistry.profiles.isEmpty ? "No Time Capsules Saved" : "All Time Capsules")
                .font(.title2.weight(.semibold))
            if appStore.deviceRegistry.profiles.isEmpty {
                Text("Add a Time Capsule to configure SMB, run checkups, and manage maintenance tasks.")
                    .foregroundStyle(.secondary)
                Button {
                    appStore.showAddDevice()
                } label: {
                    Label("Add Time Capsule", systemImage: "plus.circle")
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

private struct AddDeviceView: View {
    @ObservedObject var store: AddDeviceFlowStore

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Add Time Capsule")
                    .font(.title2.weight(.semibold))
                Spacer()
                Picker("Connection Method", selection: Binding(
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
                    Text(store.currentStage?.description ?? "Browse for AirPort Bonjour services")
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

            if store.entryMode == .discover && !store.devices.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Discovered Devices")
                        .font(.headline)
                    ForEach(store.devices) { device in
                        Button {
                            store.select(device)
                        } label: {
                            DeviceCandidateRow(device: device, selected: store.selectedDeviceID == device.id)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }

            HStack {
                TextField("Host or IP", text: Binding(
                    get: { store.hostFieldText },
                    set: { store.manualHost = $0 }
                ))
                .disabled(!store.isHostFieldEditable)
                SecureField("Time Capsule password", text: $store.password)
            }

            HStack {
                Button {
                    store.runConfigure()
                } label: {
                    Label("Save Device", systemImage: "checkmark.circle")
                }
                .disabled(!store.canConfigure)

                Button {
                    store.reset()
                } label: {
                    Label("Reset", systemImage: "arrow.counterclockwise")
                }
                .disabled(store.isRunning)
            }

            if let profile = store.savedProfile {
                Label("Saved \(profile.title)", systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
            }

            if let error = store.error {
                ErrorBlock(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var statusIcon: String {
        switch store.state {
        case .idle, .manualEntry, .passwordEntry:
            return "circle"
        case .discovering, .configuring, .savingProfile:
            return "hourglass"
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
                Text([device.host, device.hostname].filter { !$0.isEmpty }.joined(separator: "  "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(device.model ?? device.syap ?? "")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 6)
    }
}

private struct DeviceDashboardView: View {
    let profile: DeviceProfile
    @ObservedObject var dashboardStore: DashboardStore
    @ObservedObject var appStore: AppStore
    @Binding var replacementPassword: String
    let showDiagnostics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("", selection: $dashboardStore.selectedTab) {
                ForEach(DeviceDashboardTab.allCases) { tab in
                    Text(tab.title).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding()

            Divider()

            ScrollView {
                Group {
                    switch dashboardStore.selectedTab {
                    case .overview:
                        OverviewTab(profile: profile, dashboardStore: dashboardStore, appStore: appStore, replacementPassword: $replacementPassword)
                    case .install:
                        InstallTab(profile: profile, dashboardStore: dashboardStore, showDiagnostics: showDiagnostics)
                    case .checkup:
                        CheckupTab(profile: profile, dashboardStore: dashboardStore, showDiagnostics: showDiagnostics)
                    case .maintenance:
                        MaintenanceTab(profile: profile, dashboardStore: dashboardStore, showDiagnostics: showDiagnostics)
                    case .advanced:
                        AdvancedTab(profile: profile, appStore: appStore)
                    }
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct OverviewTab: View {
    let profile: DeviceProfile
    @ObservedObject var dashboardStore: DashboardStore
    @ObservedObject var appStore: AppStore
    @Binding var replacementPassword: String

    var body: some View {
        let summary = dashboardStore.summary(for: profile)
        VStack(alignment: .leading, spacing: 16) {
            if let warning = summary.hostWarning {
                WarningBanner(warning: warning)
            }

            Text(profile.title)
                .font(.title2.weight(.semibold))

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow { Text("Status").foregroundStyle(.secondary); Text(summary.displayStatus.title) }
                GridRow { Text("Host").foregroundStyle(.secondary); Text(profile.host) }
                GridRow { Text("Model").foregroundStyle(.secondary); Text(profile.model ?? "Unknown") }
                GridRow { Text("Generation").foregroundStyle(.secondary); Text(profile.deviceGeneration ?? "Unknown") }
                GridRow { Text("Payload").foregroundStyle(.secondary); Text(profile.payloadFamily ?? "Unknown") }
                GridRow { Text("Password").foregroundStyle(.secondary); Text(summary.passwordState.rawValue) }
                GridRow { Text("Last Checkup").foregroundStyle(.secondary); Text(profile.lastCheckup?.summary ?? "Never") }
                GridRow { Text("Last Install").foregroundStyle(.secondary); Text(profile.lastDeploy?.summary ?? "Never") }
            }

            HStack {
                Button(primaryActionTitle(summary.primaryAction)) {
                    runPrimary(summary.primaryAction)
                }
                .buttonStyle(.borderedProminent)

                Button {
                    dashboardStore.runCheckup(profile: profile)
                } label: {
                    Label("Run Checkup", systemImage: "stethoscope")
                }
            }

            HStack {
                SecureField("Replacement password", text: $replacementPassword)
                Button {
                    try? appStore.savePassword(replacementPassword, for: profile)
                    replacementPassword = ""
                } label: {
                    Label("Save Password", systemImage: "key")
                }
                .disabled(replacementPassword.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }

            if let passwordError = dashboardStore.passwordError {
                Text(passwordError)
                    .foregroundStyle(.red)
            }
        }
    }

    private func primaryActionTitle(_ action: DashboardPrimaryAction) -> String {
        switch action {
        case .addDevice:
            return "Add Time Capsule"
        case .replacePassword:
            return "Replace Password"
        case .runCheckup:
            return "Run Checkup"
        case .installSMB:
            return "Install SMB"
        case .viewCheckup:
            return "View Checkup"
        case .openSMB:
            return "Open SMB Address"
        }
    }

    private func runPrimary(_ action: DashboardPrimaryAction) {
        switch action {
        case .replacePassword:
            replacementPassword = ""
        case .runCheckup:
            dashboardStore.runCheckup(profile: profile)
        case .viewCheckup:
            dashboardStore.selectedTab = .checkup
        case .openSMB:
            openSMBAddress()
        case .installSMB:
            dashboardStore.runInstallPlan(profile: profile)
        case .addDevice:
            appStore.showAddDevice()
        }
    }

    private func openSMBAddress() {
        let host = profile.host
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: #"^.*@"#, with: "", options: .regularExpression)
        guard !host.isEmpty, let url = URL(string: "smb://\(host)") else {
            return
        }
        NSWorkspace.shared.open(url)
    }
}

private struct InstallTab: View {
    let profile: DeviceProfile
    @ObservedObject var dashboardStore: DashboardStore
    let showDiagnostics: () -> Void

    var body: some View {
        let store = dashboardStore.deployStore
        VStack(alignment: .leading, spacing: 12) {
            Text("Install / Update")
                .font(.title2.weight(.semibold))
            HStack {
                Toggle(L10n.string("toggle.enable_nbns"), isOn: $dashboardStore.deployStore.nbnsEnabled)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $dashboardStore.deployStore.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $dashboardStore.deployStore.noWait)
                Toggle(L10n.string("toggle.force_debug_logging"), isOn: $dashboardStore.deployStore.debugLogging)
                TextField(L10n.string("field.mount_wait"), text: $dashboardStore.deployStore.mountWait)
                    .frame(width: 150)
            }
            HStack {
                Button {
                    dashboardStore.runInstallPlan(profile: profile)
                } label: {
                    Label("Plan Install", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(store.isRunning || store.mountWaitValue == nil)
                Button {
                    dashboardStore.runInstall(profile: profile)
                } label: {
                    Label("Install SMB", systemImage: "square.and.arrow.up")
                }
                .disabled(!store.canDeploy)
                Label(store.state.title, systemImage: "circle")
            }
            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let plan = store.plan {
                let presentation = DeployPlanPresentation(
                    plan: plan,
                    profile: profile,
                    hostWarning: HostCompatibilityPolicy.warning()
                )
                Text(presentation.title)
                    .font(.headline)
                SummaryGrid(rows: presentation.summaryRows.map { ($0.label, $0.value) })
                ForEach(presentation.warnings, id: \.self) { warning in
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.yellow)
                }
                DisclosureGroup("Advanced Plan Details") {
                    SummaryGrid(rows: presentation.advancedRows.map { ($0.label, $0.value) })
                        .padding(.top, 6)
                }
            }
            if let result = store.result {
                SummaryGrid(rows: [
                    ("Verified", result.verified == true ? "yes" : "no"),
                    ("Reboot Requested", result.rebootRequested == true ? "yes" : "no"),
                    ("Message", result.message ?? "Install completed.")
                ])
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { action in
                    handleRecovery(action: action, error: error)
                }
            }
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = dashboardStore.handleRecoveryAction(action, error: error, profile: profile)
    }
}

private struct CheckupTab: View {
    let profile: DeviceProfile
    @ObservedObject var dashboardStore: DashboardStore
    let showDiagnostics: () -> Void

    var body: some View {
        let store = dashboardStore.doctorStore
        VStack(alignment: .leading, spacing: 12) {
            Text("Checkup")
                .font(.title2.weight(.semibold))
            HStack {
                TextField(L10n.string("field.bonjour_timeout"), text: $dashboardStore.doctorStore.bonjourTimeout)
                    .frame(width: 180)
                Button {
                    dashboardStore.runCheckup(profile: profile)
                } label: {
                    Label("Run Checkup", systemImage: "stethoscope")
                }
                .disabled(store.isRunning || store.bonjourTimeoutValue == nil)
                Label(store.state.title, systemImage: "circle")
            }
            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let summary = store.summary {
                let presentation = CheckupPresentation(summary: summary, state: store.state)
                Text(presentation.headline)
                    .font(.headline)
                SummaryGrid(rows: presentation.summaryRows.map { ($0.label, $0.value) })
                ForEach(presentation.groups) { group in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(group.domain).font(.headline)
                        ForEach(Array(group.checks.enumerated()), id: \.offset) { _, check in
                            HStack {
                                Text(check.status)
                                    .font(.system(.caption, design: .monospaced))
                                    .frame(width: 44, alignment: .leading)
                                Text(check.message)
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { action in
                    handleRecovery(action: action, error: error)
                }
            }
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = dashboardStore.handleRecoveryAction(action, error: error, profile: profile)
    }
}

private struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var dashboardStore: DashboardStore
    let showDiagnostics: () -> Void

    var body: some View {
        let store = dashboardStore.maintenanceStore
        let presentation = MaintenanceWorkflowPresentation.presentation(for: store.selectedWorkflow)
        VStack(alignment: .leading, spacing: 12) {
            Text("Maintenance")
                .font(.title2.weight(.semibold))
            Picker("Maintenance", selection: $dashboardStore.maintenanceStore.selectedWorkflow) {
                Text("NetBSD4 Activation").tag(MaintenanceWorkflow.activate)
                Text("Uninstall").tag(MaintenanceWorkflow.uninstall)
                Text("Disk Repair").tag(MaintenanceWorkflow.fsck)
                Text("File Metadata Repair").tag(MaintenanceWorkflow.repairXattrs)
            }
            .pickerStyle(.segmented)

            VStack(alignment: .leading, spacing: 4) {
                Text(presentation.title)
                    .font(.headline)
                Text(presentation.subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Label(presentation.risk, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack {
                TextField(L10n.string("field.mount_wait"), text: $dashboardStore.maintenanceStore.mountWait)
                    .frame(width: 150)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $dashboardStore.maintenanceStore.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $dashboardStore.maintenanceStore.noWait)
            }

            maintenanceControls(store: store)
            FlashBootHookSection(profile: profile)

            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { action in
                    handleRecovery(action: action, error: error)
                }
            }
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = dashboardStore.handleRecoveryAction(action, error: error, profile: profile)
    }

    @ViewBuilder
    private func maintenanceControls(store: MaintenanceStore) -> some View {
        switch store.selectedWorkflow {
        case .activate:
            HStack {
                Button("Plan Start SMB") {
                    if let password = dashboardStore.maintenancePassword(for: profile) {
                        store.planActivation(password: password, profile: profile)
                    }
                }
                Button("Start SMB") {
                    if let password = dashboardStore.maintenancePassword(for: profile) {
                        store.runActivation(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunActivation)
                Label(store.activateState.title, systemImage: "circle")
            }
        case .uninstall:
            HStack {
                Button("Plan Uninstall") {
                    if let password = dashboardStore.maintenancePassword(for: profile) {
                        store.planUninstall(password: password, profile: profile)
                    }
                }
                Button("Uninstall") {
                    if let password = dashboardStore.maintenancePassword(for: profile) {
                        store.runUninstall(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunUninstall)
                Label(store.uninstallState.title, systemImage: "circle")
            }
        case .fsck:
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button("Find Volumes") {
                        if let password = dashboardStore.maintenancePassword(for: profile) {
                            store.refreshFsckTargets(password: password, profile: profile)
                        }
                    }
                    Button("Plan Disk Repair") {
                        if let password = dashboardStore.maintenancePassword(for: profile) {
                            store.planFsck(password: password, profile: profile)
                        }
                    }
                    .disabled(!store.canPlanFsck)
                    Button("Run Disk Repair") {
                        if let password = dashboardStore.maintenancePassword(for: profile) {
                            store.runFsck(password: password, profile: profile)
                        }
                    }
                    .disabled(!store.canRunFsck)
                    Label(store.fsckState.title, systemImage: "circle")
                }
                ForEach(store.fsckTargets) { target in
                    Button {
                        store.selectedFsckTargetID = target.id
                    } label: {
                        HStack {
                            Image(systemName: store.selectedFsckTargetID == target.id ? "checkmark.circle.fill" : "circle")
                            Text(target.name ?? target.device)
                            Text(target.mountpoint).foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        case .repairXattrs:
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField(L10n.string("field.repair_xattrs_path"), text: $dashboardStore.maintenanceStore.repairPath)
                    Button {
                        chooseRepairPath(store: store)
                    } label: {
                        Label("Choose Folder", systemImage: "folder")
                    }
                }
                HStack {
                    Button("Scan Metadata") {
                        store.scanRepairXattrs()
                    }
                    Button("Repair Metadata") {
                        store.runRepairXattrs()
                    }
                    .disabled(!store.canRepairXattrs)
                    Label(store.repairState.title, systemImage: "circle")
                }
                if let scan = store.repairScan {
                    Text("\(scan.repairableCount) repairable item(s)")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func chooseRepairPath(store: MaintenanceStore) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        if panel.runModal() == .OK, let url = panel.url {
            store.repairPath = url.path
        }
    }
}

private struct AdvancedTab: View {
    let profile: DeviceProfile
    @ObservedObject var appStore: AppStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Advanced")
                .font(.title2.weight(.semibold))
            SummaryGrid(rows: [
                ("Profile ID", profile.id),
                ("Config", profile.configPath),
                ("Helper", appStore.backend.helperPath.isEmpty ? "Auto" : appStore.backend.helperPath)
            ])
            EventList(events: appStore.backend.events)
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
