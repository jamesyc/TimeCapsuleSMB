import SwiftUI

public struct ContentView: View {
    @StateObject private var appStore: AppStore
    @StateObject private var addDeviceStore: AddDeviceFlowStore
    @StateObject private var appSettingsEditorStore: AppSettingsEditorStore
    @StateObject private var dashboardStore: DashboardStore
    @State private var diagnosticsPresented = false
    @State private var diagnosticsShowBackendEvents = true
    @State private var profilePendingDeletion: DeviceProfile?
    @State private var deleteErrorMessage: String?

    @MainActor
    public init() {
        let appStore = AppStore()
        _appStore = StateObject(wrappedValue: appStore)
        _appSettingsEditorStore = StateObject(wrappedValue: AppSettingsEditorStore(settings: appStore.appSettingsStore.settings))
        _addDeviceStore = StateObject(wrappedValue: AddDeviceFlowStore(
            coordinator: appStore.operationCoordinator,
            registry: appStore.deviceRegistry,
            passwordStore: appStore.passwordStore,
            profilePersistence: appStore.profilePersistence,
            discovery: appStore.deviceDiscovery
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
                        registry: appStore.deviceRegistry,
                        context: activityDisplayContext
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
                        disabled: selectedProfileIsBusy
                    ) {
                        guard let profile = appStore.selectedProfile else {
                            return
                        }
                        profilePendingDeletion = profile
                    }
                    ToolbarIconButton(
                        title: L10n.string("toolbar.cancel"),
                        systemImage: "xmark.circle",
                        disabled: !appStore.operationCoordinator.canCancel
                    ) {
                        appStore.operationCoordinator.cancel()
                    }
                }
            }
        }
        .frame(minWidth: 1080, minHeight: 720)
        .background(WindowCloseGuardInstaller())
        .onAppear {
            configureCloseGuard()
        }
        .task {
            await appStore.start()
            addDeviceStore.applyAppSettings(appStore.appSettingsStore.settings)
            appSettingsEditorStore.sync(settings: appStore.appSettingsStore.settings)
        }
        .onChange(of: addDeviceStore.savedProfile) { _, profile in
            guard let profile else { return }
            appStore.select(profile)
        }
        .onChange(of: appStore.appSettingsStore.settings) { _, settings in
            addDeviceStore.applyAppSettings(settings)
            appSettingsEditorStore.sync(settings: settings)
        }
        .onChange(of: diagnosticsPresented) { _, isPresented in
            guard isPresented else { return }
            diagnosticsShowBackendEvents = appStore.appSettingsStore.settings.showRawBackendEventsByDefault
        }
        .sheet(isPresented: $diagnosticsPresented) {
            AppDiagnosticsView(
                store: appStore.appReadinessStore,
                exportContext: { includeBackendEvents in
                    appStore.diagnosticsExportContext(includeBackendEvents: includeBackendEvents)
                },
                showBackendEvents: $diagnosticsShowBackendEvents,
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
            appStore.operationCoordinator.pendingConfirmation?.title ?? "",
            isPresented: confirmationPresented,
            presenting: appStore.operationCoordinator.pendingConfirmation
        ) { confirmation in
            Button(confirmation.actionTitle, role: .destructive) {
                appStore.operationCoordinator.confirmPending()
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                appStore.operationCoordinator.cancelPendingConfirmation()
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
            get: { appStore.operationCoordinator.pendingConfirmation != nil },
            set: { isPresented in
                if !isPresented {
                    appStore.operationCoordinator.cancelPendingConfirmation()
                }
            }
        )
    }

    private var selectedProfileIsBusy: Bool {
        guard let profile = appStore.selectedProfile else {
            return true
        }
        return appStore.operationCoordinator.lane(for: profile).isBusy
    }

    private func configureCloseGuard() {
        AppCloseGuard.shared.configure { [weak appStore] in
            appStore?.operationCoordinator.hasActiveWork ?? false
        }
    }

    private var sidebarSelection: Binding<String?> {
        Binding(
            get: {
                if appStore.showingActivity {
                    return "activity"
                }
                if appStore.showingAppSettings {
                    return "settings"
                }
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
                } else if value == "activity" {
                    appStore.showActivity()
                } else if value == "settings" {
                    appStore.showAppSettings()
                } else if value == "all" {
                    appStore.selectedDeviceID = nil
                    appStore.showingAddDevice = false
                    appStore.showingActivity = false
                    appStore.showingAppSettings = false
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
            Label(L10n.string("sidebar.activity"), systemImage: appStore.activityStore.hasActiveActivity ? "hourglass" : "clock")
                .tag("activity")
            Label(L10n.string("sidebar.settings"), systemImage: "gearshape")
                .tag("settings")

            Section(L10n.string("sidebar.devices")) {
                ForEach(appStore.deviceRegistry.profiles) { profile in
                    let summary = appStore.dashboardSummary(for: profile)
                    DeviceSidebarRow(
                        profile: profile,
                        summary: summary,
                        lastSeenText: appStore.deviceDiscovery.lastSeenText(for: profile)
                    )
                        .contextMenu {
                            DeviceSidebarContextMenu(
                                presentation: sidebarContextMenuPresentation(for: profile, summary: summary)
                            ) { action in
                                performSidebarContextMenuAction(action, profile: profile)
                            }
                        }
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

    private func sidebarContextMenuPresentation(
        for profile: DeviceProfile,
        summary: DeviceDashboardSummary
    ) -> DeviceSidebarContextMenuPresentation {
        DeviceSidebarContextMenuPresentation(
            profile: profile,
            summary: summary,
            isDeviceBusy: appStore.operationCoordinator.lane(for: profile).isBusy
        )
    }

    private func performSidebarContextMenuAction(
        _ action: DeviceSidebarContextMenuAction,
        profile: DeviceProfile
    ) {
        switch action {
        case .openOverview:
            openDashboard(profile, tab: .overview)
        case .openFinder:
            dashboardStore.session(for: profile).performSecondaryAction(.openFinder, profile: profile)
        case .runCheckup:
            guard !appStore.operationCoordinator.lane(for: profile).isBusy else {
                return
            }
            appStore.select(profile)
            dashboardStore.session(for: profile).performSecondaryAction(.runCheckup, profile: profile)
        case .viewCheckup:
            openDashboard(profile, tab: .checkup)
        case .refreshStatus:
            guard !appStore.operationCoordinator.lane(for: profile).isBusy else {
                return
            }
            dashboardStore.session(for: profile).performSecondaryAction(.refreshStatus, profile: profile)
        case .settings:
            openDashboard(profile, tab: .settings)
        case .copySMBAddress, .copyHostname, .copyIPAddress:
            copySidebarValue(action, profile: profile)
        case .removeFromThisMac:
            guard !appStore.operationCoordinator.lane(for: profile).isBusy else {
                return
            }
            profilePendingDeletion = profile
        }
    }

    private func openDashboard(_ profile: DeviceProfile, tab: DeviceDashboardTab) {
        let session = dashboardStore.session(for: profile)
        session.selectedTab = tab
        appStore.select(profile)
    }

    private func copySidebarValue(_ action: DeviceSidebarContextMenuAction, profile: DeviceProfile) {
        let summary = appStore.dashboardSummary(for: profile)
        let presentation = sidebarContextMenuPresentation(for: profile, summary: summary)
        guard let value = presentation.clipboardValue(for: action) else {
            return
        }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(value, forType: .string)
    }

    private var activityDisplayContext: ActivityDisplayContext {
        ActivityDisplayContext(
            selectedDeviceID: appStore.selectedDeviceID,
            showingAddDevice: appStore.showingAddDevice,
            showingActivity: appStore.showingActivity
        )
    }

    @ViewBuilder
    private var detail: some View {
        if appStore.showingActivity {
            ActivityDetailView(
                activityStore: appStore.activityStore,
                registry: appStore.deviceRegistry
            )
        } else if appStore.showingAppSettings {
            AppSettingsView(
                appStore: appStore,
                editor: appSettingsEditorStore
            )
        } else if appStore.showingAddDevice {
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
            DeviceListOverviewView(
                appStore: appStore,
                addDiscoveredDevice: { device in
                    addDeviceStore.select(device)
                    appStore.showAddDevice()
                }
            )
        }
    }
}
