import SwiftUI

struct SettingsTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let appStore: AppStore
    @ObservedObject var backend: BackendClient

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.settings"))
                .font(.title2.weight(.semibold))
            DeviceProfileEditorView(
                profile: profile,
                store: session.profileEditorStore,
                diagnosticsText: {
                    DiagnosticsExportBuilder().build(context: appStore.diagnosticsExportContext(includeBackendEvents: true))
                }
            )
            DashboardDisclosureSection(title: L10n.string("profile_editor.details")) {
                SummaryGrid(rows: [
                    (L10n.string("advanced.profile_id"), profile.id),
                    (L10n.string("advanced.config"), profile.configPath),
                    (L10n.string("advanced.helper"), backend.helperPath.isEmpty ? L10n.string("value.auto") : backend.helperPath)
                ], valueLineLimit: nil)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            }
            EventList(events: session.events)
        }
    }
}

private struct DeviceProfileEditorView: View {
    let profile: DeviceProfile
    @ObservedObject var store: DeviceProfileEditorStore
    let diagnosticsText: () -> String

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
                    Text(L10n.string("dashboard.password.title"))
                        .foregroundStyle(.secondary)
                    RevealablePasswordField(
                        L10n.string("dashboard.replacement_password"),
                        text: $store.replacementPassword
                    ) {
                        guard store.canSave else { return }
                        Task { @MainActor in
                            await store.save(profile: profile)
                        }
                    }
                    .frame(maxWidth: 360)
                }
            }

            if let passwordError = store.passwordError {
                Text(passwordError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            DeviceProfileAdvancedSettingsView(store: store)

            Divider()

            HStack(spacing: 10) {
                Button {
                    Task { @MainActor in
                        await store.save(profile: profile)
                    }
                } label: {
                    Label(L10n.string("profile_editor.save"), systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.borderedProminent)
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
                ErrorRecoveryView(error: error, diagnosticsText: diagnosticsText) { _ in }
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

private struct DeviceProfileAdvancedSettingsView: View {
    @ObservedObject var store: DeviceProfileEditorStore

    var body: some View {
        DashboardDisclosureSection(title: L10n.string("profile_editor.advanced")) {
            VStack(alignment: .leading, spacing: 8) {
                Text(L10n.string("profile_editor.advanced.deploy_notice"))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                DeviceAdvancedSettingsFields(
                    rsyncEnabled: $store.draft.rsyncEnabled,
                    internalShareUseDiskRoot: $store.draft.internalShareUseDiskRoot,
                    smbBrowseCompatibility: $store.draft.smbBrowseCompatibility,
                    mdnsAdvertiseAFP: $store.draft.mdnsAdvertiseAFP,
                    anyProtocol: $store.draft.anyProtocol,
                    requireSMBEncryption: $store.draft.requireSMBEncryption,
                    forceDisableSMBSigningAndEncryption: $store.draft.forceDisableSMBSigningAndEncryption,
                    fruitMetadataNetatalk: $store.draft.fruitMetadataNetatalk,
                    vfsAIOForkEnabled: $store.draft.vfsAIOForkEnabled,
                    debugLogging: $store.draft.debugLogging,
                    mountWaitSeconds: $store.draft.mountWaitSeconds,
                    ataIdleSeconds: $store.draft.ataIdleSeconds,
                    ataStandby: $store.draft.ataStandby
                )
            }
            .frame(maxWidth: 680, alignment: .leading)
        }
    }
}
