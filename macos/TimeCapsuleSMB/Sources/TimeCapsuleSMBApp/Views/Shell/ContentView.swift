import SwiftUI

public struct ContentView: View {
    @StateObject private var appStore: AppStore
    @ObservedObject private var appReadinessStore: AppReadinessStore
    @ObservedObject private var appSettingsStore: AppSettingsStore
    @ObservedObject private var deviceRegistry: DeviceRegistryStore
    @ObservedObject private var operationCoordinator: OperationCoordinator
    @ObservedObject private var activityStore: ActivityStore
    @ObservedObject private var deviceDiscovery: DeviceDiscoveryStore
    @ObservedObject private var appBackend: BackendClient
    @StateObject private var addDeviceStore: AddDeviceFlowStore
    @StateObject private var appSettingsEditorStore: AppSettingsEditorStore
    @StateObject private var dashboardStore: DashboardStore
    @State private var diagnosticsPresented = false
    @State private var diagnosticsShowBackendEvents = true
    @State private var profilePendingDeletion: DeviceProfile?
    @State private var deleteErrorMessage: String?
    @State private var systemColorScheme = SystemAppearance.currentColorScheme
    private let startsAutomatically: Bool

    @MainActor
    public init() {
        self.init(composition: .production())
    }

    @MainActor
    init(composition: AppViewComposition, startsAutomatically: Bool = true) {
        _appStore = StateObject(wrappedValue: composition.appStore)
        _appReadinessStore = ObservedObject(wrappedValue: composition.appStore.appReadinessStore)
        _appSettingsStore = ObservedObject(wrappedValue: composition.appStore.appSettingsStore)
        _deviceRegistry = ObservedObject(wrappedValue: composition.appStore.deviceRegistry)
        _operationCoordinator = ObservedObject(wrappedValue: composition.appStore.operationCoordinator)
        _activityStore = ObservedObject(wrappedValue: composition.appStore.activityStore)
        _deviceDiscovery = ObservedObject(wrappedValue: composition.appStore.deviceDiscovery)
        _appBackend = ObservedObject(wrappedValue: composition.appStore.backend)
        _appSettingsEditorStore = StateObject(wrappedValue: composition.appSettingsEditorStore)
        _addDeviceStore = StateObject(wrappedValue: composition.addDeviceStore)
        _dashboardStore = StateObject(wrappedValue: composition.dashboardStore)
        self.startsAutomatically = startsAutomatically
    }

    public var body: some View {
        NavigationSplitView {
            sidebar
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
                    detail
                    Divider()
                    ActivityCompactView(
                        activityStore: activityStore,
                        registry: deviceRegistry,
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
                        disabled: cancelButtonDisabled
                    ) {
                        cancelSelectedOperation()
                    }
                }
            }
        }
        .frame(minWidth: 1080, minHeight: 720)
        .preferredColorScheme(appSettingsStore.settings.appearance.preferredColorScheme(systemColorScheme: systemColorScheme))
        .background(WindowCloseGuardInstaller())
        .onAppear {
            configureCloseGuard()
            systemColorScheme = SystemAppearance.currentColorScheme
        }
        .task {
            if startsAutomatically {
                await appStore.start()
            }
            addDeviceStore.applyAppSettings(appSettingsStore.settings)
            appSettingsEditorStore.sync(settings: appSettingsStore.settings)
        }
        .onChange(of: addDeviceStore.savedProfile) { _, profile in
            guard let profile else { return }
            appStore.select(profile)
        }
        .onChange(of: appSettingsStore.settings) { _, settings in
            systemColorScheme = SystemAppearance.currentColorScheme
            addDeviceStore.applyAppSettings(settings)
            appSettingsEditorStore.sync(settings: settings)
        }
        .onReceive(DistributedNotificationCenter.default().publisher(for: SystemAppearance.didChangeNotification)) { _ in
            systemColorScheme = SystemAppearance.currentColorScheme
        }
        .onChange(of: diagnosticsPresented) { _, isPresented in
            guard isPresented else { return }
            diagnosticsShowBackendEvents = appSettingsStore.settings.showRawBackendEventsByDefault
        }
        .sheet(isPresented: $diagnosticsPresented) {
            AppDiagnosticsView(
                store: appReadinessStore,
                exportContext: { includeBackendEvents in
                    appStore.diagnosticsExportContext(includeBackendEvents: includeBackendEvents)
                },
                showBackendEvents: $diagnosticsShowBackendEvents,
                helperPath: Binding(
                    get: { appBackend.helperPath },
                    set: { appBackend.helperPath = $0 }
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
            operationCoordinator.pendingConfirmation?.title ?? "",
            isPresented: confirmationPresented,
            presenting: operationCoordinator.pendingConfirmation
        ) { confirmation in
            Button(confirmation.actionTitle, role: .destructive) {
                operationCoordinator.confirmPending()
            }
            Button(L10n.string("action.cancel"), role: .cancel) {
                operationCoordinator.cancelPendingConfirmation()
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
            get: { operationCoordinator.pendingConfirmation != nil },
            set: { isPresented in
                if !isPresented {
                    operationCoordinator.cancelPendingConfirmation()
                }
            }
        )
    }

    private var selectedProfileIsBusy: Bool {
        guard let profile = appStore.selectedProfile else {
            return true
        }
        return operationCoordinator.isDeviceBusy(profile)
    }

    private var cancelButtonDisabled: Bool {
        if let selectedDeviceID = appStore.selectedDeviceID,
           operationCoordinator.isDeviceBusy(selectedDeviceID) {
            return !operationCoordinator.canCancel(profileID: selectedDeviceID)
        }
        return !operationCoordinator.canCancel
    }

    private func cancelSelectedOperation() {
        if let selectedDeviceID = appStore.selectedDeviceID,
           operationCoordinator.isDeviceBusy(selectedDeviceID) {
            operationCoordinator.cancel(profileID: selectedDeviceID)
            return
        }
        operationCoordinator.cancel()
    }

    private func configureCloseGuard() {
        AppCloseGuard.shared.configure { [weak operationCoordinator] in
            operationCoordinator?.hasActiveWork ?? false
        }
    }

    private var sidebarSelection: Binding<AppRoute?> {
        Binding(
            get: {
                appStore.route
            },
            set: { value in
                guard let value else { return }
                appStore.navigate(to: value)
            }
        )
    }

    private var sidebar: some View {
        List(selection: sidebarSelection) {
            Label(L10n.string("sidebar.all_airport_devices"), systemImage: "externaldrive.connected.to.line.below")
                .tag(AppRoute.allDevices)
            Label(L10n.string("sidebar.activity"), systemImage: activityStore.hasActiveActivity ? "hourglass" : "clock")
                .tag(AppRoute.activity)
            Label(L10n.string("sidebar.settings"), systemImage: "gearshape")
                .tag(AppRoute.appSettings)

            Section(L10n.string("sidebar.devices")) {
                ForEach(deviceRegistry.profiles) { profile in
                    let summary = appStore.dashboardSummary(for: profile)
                    DeviceSidebarRow(
                        profile: profile,
                        summary: summary,
                        lastSeenText: deviceDiscovery.lastSeenText(for: profile)
                    )
                        .contextMenu {
                            DeviceSidebarContextMenu(
                                presentation: sidebarContextMenuPresentation(for: profile, summary: summary)
                            ) { action in
                                performSidebarContextMenuAction(action, profile: profile)
                            }
                        }
                        .tag(AppRoute.device(profile.id))
                }
            }

            Section {
                Label(L10n.string("sidebar.add_airport_device"), systemImage: "plus.circle")
                    .tag(AppRoute.addDevice)
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
            isDeviceBusy: operationCoordinator.isDeviceBusy(profile)
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
            guard !operationCoordinator.isDeviceBusy(profile) else {
                return
            }
            appStore.select(profile)
            dashboardStore.session(for: profile).performSecondaryAction(.runCheckup, profile: profile)
        case .viewCheckup:
            openDashboard(profile, tab: .checkup)
        case .refreshStatus:
            guard !operationCoordinator.isDeviceBusy(profile) else {
                return
            }
            dashboardStore.session(for: profile).performSecondaryAction(.refreshStatus, profile: profile)
        case .settings:
            openDashboard(profile, tab: .settings)
        case .copySMBAddress, .copyHostname, .copyIPAddress:
            copySidebarValue(action, profile: profile)
        case .removeFromThisMac:
            guard !operationCoordinator.isDeviceBusy(profile) else {
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
        switch appStore.route {
        case .activity:
            ActivityDetailView(
                activityStore: activityStore,
                registry: deviceRegistry
            )
        case .appSettings:
            AppSettingsView(
                appStore: appStore,
                appSettingsStore: appSettingsStore,
                appUpdateStore: appStore.appUpdateStore,
                editor: appSettingsEditorStore
            )
        case .addDevice:
            AddDeviceView(store: addDeviceStore)
        case .device(let profileID):
            if let profile = deviceRegistry.profile(id: profileID) {
                DeviceDashboardView(
                    profile: profile,
                    session: dashboardStore.session(for: profile),
                    appStore: appStore,
                    appSettingsStore: appSettingsStore,
                    reachabilityStore: appStore.reachabilityStore,
                    sshAccessStore: appStore.sshAccessStore,
                    operationCoordinator: operationCoordinator,
                    backend: appBackend,
                    showDiagnostics: {
                        diagnosticsPresented = true
                    }
                )
            } else {
                DeviceListOverviewView(
                    appStore: appStore,
                    deviceRegistry: deviceRegistry,
                    deviceDiscovery: deviceDiscovery,
                    backend: appBackend,
                    addDiscoveredDevice: { device in
                        addDeviceStore.select(device)
                        appStore.showAddDevice()
                    }
                )
            }
        case .allDevices:
            DeviceListOverviewView(
                appStore: appStore,
                deviceRegistry: deviceRegistry,
                deviceDiscovery: deviceDiscovery,
                backend: appBackend,
                addDiscoveredDevice: { device in
                    addDeviceStore.select(device)
                    appStore.showAddDevice()
                }
            )
        }
    }
}

private extension AppAppearance {
    func preferredColorScheme(systemColorScheme: ColorScheme) -> ColorScheme? {
        switch self {
        case .system:
            return systemColorScheme
        case .light:
            return .light
        case .dark:
            return .dark
        }
    }
}

private enum SystemAppearance {
    static let didChangeNotification = Notification.Name("AppleInterfaceThemeChangedNotification")

    static var currentColorScheme: ColorScheme {
        UserDefaults.standard.string(forKey: "AppleInterfaceStyle") == "Dark" ? .dark : .light
    }
}
