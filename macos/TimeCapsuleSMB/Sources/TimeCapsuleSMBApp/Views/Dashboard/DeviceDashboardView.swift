import SwiftUI

struct DeviceDashboardView: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let appStore: AppStore
    @ObservedObject var appSettingsStore: AppSettingsStore
    @ObservedObject var reachabilityStore: DeviceReachabilityStore
    @ObservedObject var operationCoordinator: OperationCoordinator
    @ObservedObject var backend: BackendClient
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

            Group {
                switch session.selectedTab {
                case .overview:
                    OverviewTab(profile: profile, session: session, reachabilityStore: reachabilityStore)
                case .install:
                    InstallTab(
                        profile: profile,
                        session: session,
                        operationCoordinator: operationCoordinator,
                        appSettings: appSettingsStore.settings,
                        showDiagnostics: showDiagnostics,
                        diagnosticsText: diagnosticsText
                    )
                case .checkup:
                    CheckupTab(
                        profile: profile,
                        session: session,
                        operationCoordinator: operationCoordinator,
                        appSettings: appSettingsStore.settings,
                        showDiagnostics: showDiagnostics,
                        diagnosticsText: diagnosticsText
                    )
                case .maintenance:
                    MaintenanceTab(
                        profile: profile,
                        session: session,
                        showDiagnostics: showDiagnostics,
                        diagnosticsText: diagnosticsText
                    )
                case .settings:
                    ScrollView {
                        SettingsTab(
                            profile: profile,
                            session: session,
                            appStore: appStore,
                            backend: backend
                        )
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .alert(
            session.flashStore.manualPowerCycleNotice?.title ?? "",
            isPresented: manualPowerCycleNoticePresented,
            presenting: session.flashStore.manualPowerCycleNotice
        ) { notice in
            Button(notice.viewCheckupActionTitle) {
                session.viewCheckupAfterFlashNotice()
            }
            Button(notice.actionTitle, role: .cancel) {
                session.flashStore.dismissManualPowerCycleNotice()
            }
        } message: { notice in
            Text(notice.message)
        }
    }

    private var manualPowerCycleNoticePresented: Binding<Bool> {
        Binding(
            get: { session.flashStore.manualPowerCycleNotice != nil },
            set: { isPresented in
                if !isPresented {
                    session.flashStore.dismissManualPowerCycleNotice()
                }
            }
        )
    }

    private func diagnosticsText() -> String {
        DiagnosticsExportBuilder().build(context: appStore.diagnosticsExportContext(includeBackendEvents: true))
    }
}
