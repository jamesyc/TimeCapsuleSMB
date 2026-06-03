import Foundation

@MainActor
struct AppViewComposition {
    let appStore: AppStore
    let addDeviceStore: AddDeviceFlowStore
    let appSettingsEditorStore: AppSettingsEditorStore
    let dashboardStore: DashboardStore

    static func production() -> AppViewComposition {
        let appStore = AppStore()
        return AppViewComposition(appStore: appStore)
    }

    init(appStore: AppStore) {
        self.appStore = appStore
        self.addDeviceStore = AddDeviceFlowStore(
            coordinator: appStore.operationCoordinator,
            registry: appStore.deviceRegistry,
            passwordStore: appStore.passwordStore,
            profilePersistence: appStore.profilePersistence,
            discovery: appStore.deviceDiscovery
        )
        self.appSettingsEditorStore = AppSettingsEditorStore(settings: appStore.appSettingsStore.settings)
        self.dashboardStore = DashboardStore(appStore: appStore)
    }

    init(
        appStore: AppStore,
        addDeviceStore: AddDeviceFlowStore,
        appSettingsEditorStore: AppSettingsEditorStore,
        dashboardStore: DashboardStore
    ) {
        self.appStore = appStore
        self.addDeviceStore = addDeviceStore
        self.appSettingsEditorStore = appSettingsEditorStore
        self.dashboardStore = dashboardStore
    }
}
