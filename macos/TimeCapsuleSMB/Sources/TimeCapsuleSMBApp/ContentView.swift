import AppKit
import SwiftUI

public struct ContentView: View {
    @StateObject private var appStore: AppStore
    @StateObject private var addDeviceStore: AddDeviceFlowStore
    @StateObject private var dashboardStore: DashboardStore
    @State private var diagnosticsPresented = false
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
                    ToolbarIconButton(
                        title: L10n.string("toolbar.add"),
                        systemImage: "plus"
                    ) {
                        appStore.showAddDevice()
                    }
                    ToolbarIconButton(
                        title: L10n.string("toolbar.diagnostics"),
                        systemImage: "wrench.and.screwdriver"
                    ) {
                        diagnosticsPresented = true
                    }
                    ToolbarIconButton(
                        title: L10n.string("toolbar.forget"),
                        systemImage: "trash",
                        disabled: appStore.selectedProfile == nil || appStore.backend.isRunning
                    ) {
                        guard let profile = appStore.selectedProfile else {
                            return
                        }
                        profilePendingDeletion = profile
                    }
                    ToolbarIconButton(
                        title: L10n.string("toolbar.cancel"),
                        systemImage: "xmark.circle",
                        disabled: !appStore.backend.canCancel
                    ) {
                        appStore.operationCoordinator.cancel()
                    }
                }
            }
        }
        .frame(minWidth: 1080, minHeight: 720)
        .task {
            await appStore.start()
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
            L10n.string("dialog.forget.title"),
            isPresented: deleteConfirmationPresented,
            presenting: profilePendingDeletion
        ) { profile in
            Button(L10n.format("dialog.forget.action", profile.title), role: .destructive) {
                Task { @MainActor in
                    do {
                        try await appStore.forget(profile)
                        profilePendingDeletion = nil
                    } catch {
                        deleteErrorMessage = error.localizedDescription
                    }
                }
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                profilePendingDeletion = nil
            }
        } message: { profile in
            Text(L10n.format("dialog.forget.message", profile.title))
        }
        .alert(L10n.string("dialog.forget.error_title"), isPresented: deleteErrorPresented) {
            Button(L10n.string("action.ok"), role: .cancel) {
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
            Label(L10n.string("sidebar.all_time_capsules"), systemImage: "externaldrive.connected.to.line.below")
                .tag("all")

            Section(L10n.string("sidebar.devices")) {
                ForEach(appStore.deviceRegistry.profiles) { profile in
                    DeviceSidebarRow(
                        profile: profile,
                        summary: appStore.dashboardSummary(for: profile)
                    )
                        .tag("device:\(profile.id)")
                }
            }

            Section {
                Label(L10n.string("sidebar.add_time_capsule"), systemImage: "plus.circle")
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
                session: dashboardStore.session(for: profile),
                appStore: appStore,
                showDiagnostics: {
                    diagnosticsPresented = true
                }
            )
        } else {
            DeviceListOverviewView(appStore: appStore)
        }
    }
}

private struct ToolbarIconButton: View {
    let title: String
    let systemImage: String
    var disabled = false
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button {
            guard !disabled else {
                return
            }
            action()
        } label: {
            Image(systemName: systemImage)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(disabled ? Color.secondary.opacity(0.5) : Color.primary)
                .frame(width: 28, height: 28)
                .background {
                    Circle()
                        .fill(isHovered && !disabled ? Color.primary.opacity(0.10) : Color.clear)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(disabled ? L10n.string("toolbar.disabled") : "")
        .onHover { hovering in
            isHovered = hovering
        }
    }
}

private struct DeviceListOverviewView: View {
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

private struct AddDeviceView: View {
    @ObservedObject var store: AddDeviceFlowStore

    var body: some View {
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
                    Text(store.currentStage?.description ?? L10n.string("add_device.discover.placeholder"))
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

private struct DeviceDashboardView: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore
    let showDiagnostics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("", selection: $session.selectedTab) {
                ForEach(DeviceDashboardTab.allCases) { tab in
                    Text(tab.title).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding()

            Divider()

            ScrollView {
                Group {
                    switch session.selectedTab {
                    case .overview:
                        OverviewTab(profile: profile, session: session, appStore: appStore)
                    case .install:
                        InstallTab(profile: profile, session: session, showDiagnostics: showDiagnostics)
                    case .checkup:
                        CheckupTab(profile: profile, session: session, showDiagnostics: showDiagnostics)
                    case .maintenance:
                        MaintenanceTab(profile: profile, session: session, showDiagnostics: showDiagnostics)
                    case .advanced:
                        AdvancedTab(profile: profile, session: session, appStore: appStore)
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
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore

    var body: some View {
        let summary = session.summary(for: profile)
        VStack(alignment: .leading, spacing: 16) {
            if let warning = summary.hostWarning {
                WarningBanner(warning: warning)
            }

            Text(profile.title)
                .font(.title2.weight(.semibold))

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow { Text(L10n.string("dashboard.overview.status")).foregroundStyle(.secondary); Text(summary.displayStatus.title) }
                GridRow { Text(L10n.string("dashboard.overview.host")).foregroundStyle(.secondary); Text(profile.host) }
                GridRow { Text(L10n.string("dashboard.overview.model")).foregroundStyle(.secondary); Text(profile.model ?? L10n.string("value.unknown")) }
                GridRow { Text(L10n.string("dashboard.overview.generation")).foregroundStyle(.secondary); Text(profile.deviceGeneration ?? L10n.string("value.unknown")) }
                GridRow { Text(L10n.string("dashboard.overview.payload")).foregroundStyle(.secondary); Text(profile.payloadFamily ?? L10n.string("value.unknown")) }
                GridRow { Text(L10n.string("dashboard.overview.password")).foregroundStyle(.secondary); Text(summary.passwordState.title) }
                GridRow { Text(L10n.string("dashboard.overview.last_checkup")).foregroundStyle(.secondary); Text(profile.lastCheckup?.summary ?? L10n.string("value.never")) }
                GridRow { Text(L10n.string("dashboard.overview.last_install")).foregroundStyle(.secondary); Text(profile.lastDeploy?.summary ?? L10n.string("value.never")) }
            }

            HStack {
                Button(primaryActionTitle(summary.primaryAction)) {
                    runPrimary(summary.primaryAction)
                }
                .buttonStyle(.borderedProminent)

                Button {
                    session.runCheckup(profile: profile)
                } label: {
                    Label(L10n.string("dashboard.action.run_checkup"), systemImage: "stethoscope")
                }
            }

            HStack {
                SecureField(L10n.string("dashboard.replacement_password"), text: $session.replacementPassword)
                Button {
                    Task { @MainActor in
                        try? await appStore.savePassword(session.replacementPassword, for: profile)
                        session.replacementPassword = ""
                    }
                } label: {
                    Label(L10n.string("dashboard.action.save_password"), systemImage: "key")
                }
                .disabled(session.replacementPassword.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }

            if let passwordError = session.passwordError {
                Text(passwordError)
                    .foregroundStyle(.red)
            }
        }
    }

    private func primaryActionTitle(_ action: DashboardPrimaryAction) -> String {
        switch action {
        case .addDevice:
            return L10n.string("sidebar.add_time_capsule")
        case .replacePassword:
            return L10n.string("dashboard.action.replace_password")
        case .runCheckup:
            return L10n.string("dashboard.action.run_checkup")
        case .installSMB:
            return L10n.string("dashboard.action.install_smb")
        case .viewCheckup:
            return L10n.string("dashboard.action.view_checkup")
        case .openSMB:
            return L10n.string("dashboard.action.open_smb")
        }
    }

    private func runPrimary(_ action: DashboardPrimaryAction) {
        switch action {
        case .replacePassword:
            session.replacementPassword = ""
        case .runCheckup:
            session.runCheckup(profile: profile)
        case .viewCheckup:
            session.selectedTab = .checkup
        case .openSMB:
            openSMBAddress()
        case .installSMB:
            session.runInstallPlan(profile: profile)
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
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.deployStore
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.install"))
                .font(.title2.weight(.semibold))
            HStack {
                Toggle(L10n.string("toggle.enable_nbns"), isOn: $session.deployStore.nbnsEnabled)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $session.deployStore.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $session.deployStore.noWait)
                Toggle(L10n.string("toggle.force_debug_logging"), isOn: $session.deployStore.debugLogging)
                TextField(L10n.string("field.mount_wait"), text: $session.deployStore.mountWait)
                    .frame(width: 150)
            }
            HStack {
                Button {
                    session.runInstallPlan(profile: profile)
                } label: {
                    Label(L10n.string("deploy.action.plan_install"), systemImage: "doc.text.magnifyingglass")
                }
                .disabled(store.isRunning || store.mountWaitValue == nil)
                Button {
                    session.runInstall(profile: profile)
                } label: {
                    Label(L10n.string("dashboard.action.install_smb"), systemImage: "square.and.arrow.up")
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
                DisclosureGroup(L10n.string("deploy.advanced_plan_details")) {
                    SummaryGrid(rows: presentation.advancedRows.map { ($0.label, $0.value) })
                        .padding(.top, 6)
                }
            }
            if let result = store.result {
                SummaryGrid(rows: [
                    (L10n.string("deploy.result.verified"), result.verified == true ? L10n.string("value.yes") : L10n.string("value.no")),
                    (L10n.string("deploy.result.reboot_requested"), result.rebootRequested == true ? L10n.string("value.yes") : L10n.string("value.no")),
                    (L10n.string("deploy.result.message"), result.message ?? L10n.string("deploy.result.default_message"))
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
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }
}

private struct CheckupTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.doctorStore
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.checkup"))
                .font(.title2.weight(.semibold))
            HStack {
                TextField(L10n.string("field.bonjour_timeout"), text: $session.doctorStore.bonjourTimeout)
                    .frame(width: 180)
                Button {
                    session.runCheckup(profile: profile)
                } label: {
                    Label(L10n.string("dashboard.action.run_checkup"), systemImage: "stethoscope")
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
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }
}

private struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.maintenanceStore
        let presentation = MaintenanceWorkflowPresentation.presentation(for: store.selectedWorkflow)
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.maintenance"))
                .font(.title2.weight(.semibold))
            Picker(L10n.string("dashboard.tab.maintenance"), selection: $session.maintenanceStore.selectedWorkflow) {
                Text(L10n.string("maintenance.workflow.activate")).tag(MaintenanceWorkflow.activate)
                Text(L10n.string("maintenance.workflow.uninstall")).tag(MaintenanceWorkflow.uninstall)
                Text(L10n.string("maintenance.workflow.fsck")).tag(MaintenanceWorkflow.fsck)
                Text(L10n.string("maintenance.workflow.repair_xattrs")).tag(MaintenanceWorkflow.repairXattrs)
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
                TextField(L10n.string("field.mount_wait"), text: $session.maintenanceStore.mountWait)
                    .frame(width: 150)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $session.maintenanceStore.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $session.maintenanceStore.noWait)
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
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }

    @ViewBuilder
    private func maintenanceControls(store: MaintenanceStore) -> some View {
        switch store.selectedWorkflow {
        case .activate:
            HStack {
                Button(L10n.string("maintenance.action.plan_start_smb")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.planActivation(password: password, profile: profile)
                    }
                }
                Button(L10n.string("maintenance.action.start_smb")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.runActivation(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunActivation)
                Label(store.activateState.title, systemImage: "circle")
            }
        case .uninstall:
            HStack {
                Button(L10n.string("maintenance.action.plan_uninstall")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.planUninstall(password: password, profile: profile)
                    }
                }
                Button(L10n.string("maintenance.action.uninstall")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.runUninstall(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunUninstall)
                Label(store.uninstallState.title, systemImage: "circle")
            }
        case .fsck:
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button(L10n.string("maintenance.action.find_volumes")) {
                        if let password = session.maintenancePassword(for: profile) {
                            store.refreshFsckTargets(password: password, profile: profile)
                        }
                    }
                    Button(L10n.string("maintenance.action.plan_disk_repair")) {
                        if let password = session.maintenancePassword(for: profile) {
                            store.planFsck(password: password, profile: profile)
                        }
                    }
                    .disabled(!store.canPlanFsck)
                    Button(L10n.string("maintenance.action.run_disk_repair")) {
                        if let password = session.maintenancePassword(for: profile) {
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
                    TextField(L10n.string("field.repair_xattrs_path"), text: $session.maintenanceStore.repairPath)
                    Button {
                        chooseRepairPath(store: store)
                    } label: {
                        Label(L10n.string("maintenance.action.choose_folder"), systemImage: "folder")
                    }
                }
                HStack {
                    Button(L10n.string("maintenance.action.scan_metadata")) {
                        store.scanRepairXattrs()
                    }
                    Button(L10n.string("maintenance.action.repair_metadata")) {
                        store.runRepairXattrs()
                    }
                    .disabled(!store.canRepairXattrs)
                    Label(store.repairState.title, systemImage: "circle")
                }
                if let scan = store.repairScan {
                    Text(L10n.format("maintenance.repairable_count", scan.repairableCount))
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
        panel.prompt = L10n.string("maintenance.action.choose")
        if panel.runModal() == .OK, let url = panel.url {
            store.repairPath = url.path
        }
    }
}

private struct AdvancedTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.advanced"))
                .font(.title2.weight(.semibold))
            DeviceProfileEditorView(profile: profile, store: session.profileEditorStore)
            SummaryGrid(rows: [
                (L10n.string("advanced.profile_id"), profile.id),
                (L10n.string("advanced.config"), profile.configPath),
                (L10n.string("advanced.helper"), appStore.backend.helperPath.isEmpty ? L10n.string("value.auto") : appStore.backend.helperPath)
            ])
            EventList(events: appStore.backend.events)
        }
    }
}

private struct DeviceProfileEditorView: View {
    let profile: DeviceProfile
    @ObservedObject var store: DeviceProfileEditorStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(L10n.string("profile_editor.title"))
                .font(.headline)

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Text(L10n.string("profile_editor.display_name"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("profile_editor.display_name"), text: $store.draft.displayName)
                        .frame(maxWidth: 360)
                }
                GridRow {
                    Text(L10n.string("dashboard.overview.host"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("dashboard.overview.host"), text: $store.draft.host)
                        .frame(maxWidth: 360)
                }
                GridRow {
                    Text(L10n.string("field.mount_wait"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("field.mount_wait"), text: $store.draft.mountWaitSeconds)
                        .frame(width: 160)
                }
            }

            HStack {
                Toggle(L10n.string("toggle.enable_nbns"), isOn: $store.draft.nbnsEnabled)
                Toggle(L10n.string("toggle.force_debug_logging"), isOn: $store.draft.debugLogging)
            }

            HStack {
                Button {
                    Task { @MainActor in
                        await store.save(profile: profile)
                    }
                } label: {
                    Label(L10n.string("profile_editor.save"), systemImage: "square.and.arrow.down")
                }
                .disabled(!store.canSave(profile: profile))

                Button {
                    store.reset(to: profile)
                } label: {
                    Label(L10n.string("profile_editor.reset"), systemImage: "arrow.counterclockwise")
                }
                .disabled(store.isRunning)

                Label(store.state.title, systemImage: "circle")
                    .foregroundStyle(.secondary)
            }

            ForEach(store.validationErrors, id: \.self) { validationError in
                Text(validationError.localizedDescription)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { _ in }
            }
        }
        .padding(.bottom, 8)
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
                Text(issues.first?.message ?? L10n.string("readiness.warning.default"))
                    .font(.caption)
                Spacer()
                Button(L10n.string("toolbar.diagnostics"), action: showDiagnostics)
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
            return L10n.string("readiness.state.resolving_bundle")
        case .checkingCapabilities:
            return L10n.string("readiness.state.checking_capabilities")
        case .validatingInstall:
            return L10n.string("readiness.state.validating_install")
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
            Label(L10n.string("readiness.blocked.title"), systemImage: "exclamationmark.octagon")
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
                    Label(L10n.string("recovery.action.retry"), systemImage: "arrow.clockwise")
                }
                .disabled(!store.canRetry)

                Button {
                    showDiagnostics()
                } label: {
                    Label(L10n.string("toolbar.diagnostics"), systemImage: "wrench.and.screwdriver")
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
                Text(L10n.string("diagnostics.title"))
                    .font(.title2.weight(.semibold))
                Spacer()
                Button(L10n.string("action.done")) {
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }

            TextField(L10n.string("field.helper"), text: $helperPath)

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow {
                    Text(L10n.string("diagnostics.state")).foregroundStyle(.secondary)
                    Text(store.state.kind.title)
                }
                if let capabilities = store.capabilities {
                    GridRow {
                        Text(L10n.string("diagnostics.helper")).foregroundStyle(.secondary)
                        Text(capabilities.helperVersion)
                    }
                    GridRow {
                        Text(L10n.string("diagnostics.distribution")).foregroundStyle(.secondary)
                        Text(capabilities.distributionRoot)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                if let validation = store.validation {
                    GridRow {
                        Text(L10n.string("diagnostics.validation")).foregroundStyle(.secondary)
                        Text(validation.summary)
                    }
                }
            }
            .font(.caption)

            if !store.issues.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text(L10n.string("diagnostics.runtime_issues"))
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

            Text(L10n.string("diagnostics.backend_events"))
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
