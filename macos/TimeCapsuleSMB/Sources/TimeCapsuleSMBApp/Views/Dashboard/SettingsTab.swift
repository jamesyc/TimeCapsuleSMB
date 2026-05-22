import SwiftUI

struct SettingsTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    @ObservedObject var appStore: AppStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.settings"))
                .font(.title2.weight(.semibold))
            DeviceProfileEditorView(profile: profile, store: session.profileEditorStore)
            SummaryGrid(rows: [
                (L10n.string("advanced.profile_id"), profile.id),
                (L10n.string("advanced.config"), profile.configPath),
                (L10n.string("advanced.helper"), appStore.backend.helperPath.isEmpty ? L10n.string("value.auto") : appStore.backend.helperPath)
            ])
            EventList(events: session.events)
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
                GridRow {
                    Toggle(L10n.string("toggle.enable_nbns"), isOn: $store.draft.nbnsEnabled)
                    Toggle(L10n.string("toggle.internal_share_use_disk_root"), isOn: $store.draft.internalShareUseDiskRoot)
                }
                GridRow {
                    Toggle(L10n.string("toggle.any_protocol"), isOn: $store.draft.anyProtocol)
                    Toggle(L10n.string("toggle.force_debug_logging"), isOn: $store.draft.debugLogging)
                }
            }

            HStack {
                Button {
                    Task { @MainActor in
                        await store.save(profile: profile)
                    }
                } label: {
                    Label(L10n.string("profile_editor.save"), systemImage: "square.and.arrow.down")
                }
                .disabled(!store.canSave)

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
        .onAppear {
            store.sync(to: profile)
        }
        .onChange(of: profile) { _, profile in
            store.sync(to: profile)
        }
        .padding(.bottom, 8)
    }
}
