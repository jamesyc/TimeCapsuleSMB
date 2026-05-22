import SwiftUI

struct DeviceDashboardView: View {
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

            Group {
                switch session.selectedTab {
                case .overview:
                    OverviewTab(profile: profile, session: session, appStore: appStore)
                case .install:
                    InstallTab(
                        profile: profile,
                        session: session,
                        appSettings: appStore.appSettingsStore.settings,
                        showDiagnostics: showDiagnostics
                    )
                case .checkup:
                    CheckupTab(
                        profile: profile,
                        session: session,
                        appSettings: appStore.appSettingsStore.settings,
                        showDiagnostics: showDiagnostics
                    )
                case .maintenance:
                    MaintenanceTab(profile: profile, session: session, showDiagnostics: showDiagnostics)
                case .settings:
                    ScrollView {
                        SettingsTab(profile: profile, session: session, appStore: appStore)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }
}
