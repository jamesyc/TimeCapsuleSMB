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
                if appStore.showingActivity {
                    return "activity"
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
                } else if value == "all" {
                    appStore.selectedDeviceID = nil
                    appStore.showingAddDevice = false
                    appStore.showingActivity = false
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
            Label(L10n.string("sidebar.activity"), systemImage: appStore.activityStore.snapshot.isRunning ? "hourglass" : "clock")
                .tag("activity")

            Section(L10n.string("sidebar.devices")) {
                ForEach(appStore.deviceRegistry.profiles) { profile in
                    DeviceSidebarRow(
                        profile: profile,
                        summary: appStore.dashboardSummary(for: profile),
                        lastSeenText: appStore.discoveryMonitor.lastSeenText(for: profile)
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
        if appStore.showingActivity {
            ActivityDetailView(
                activityStore: appStore.activityStore,
                registry: appStore.deviceRegistry
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
                    addDeviceStore.stageDiscoveredDevices(appStore.discoveryMonitor.devices, selected: device)
                    appStore.showAddDevice()
                }
            )
        }
    }
}
