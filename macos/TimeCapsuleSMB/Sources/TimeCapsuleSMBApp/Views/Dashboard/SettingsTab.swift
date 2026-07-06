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
            SummaryGrid(rows: [
                (L10n.string("advanced.profile_id"), profile.id),
                (L10n.string("advanced.config"), profile.configPath),
                (L10n.string("advanced.helper"), backend.helperPath.isEmpty ? L10n.string("value.auto") : backend.helperPath)
            ])
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
                    SecureField(L10n.string("dashboard.replacement_password"), text: $store.replacementPassword)
                        .frame(maxWidth: 360)
                        .onSubmit {
                            guard store.canSave else { return }
                            Task { @MainActor in
                                await store.save(profile: profile)
                            }
                        }
                }
            }

            if let passwordError = store.passwordError {
                Text(passwordError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            DeviceProfileAdvancedSettingsView(store: store)

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

                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    GridRow {
                        Text(L10n.string("field.mount_wait"))
                            .foregroundStyle(.secondary)
                        TextField(L10n.string("field.mount_wait"), text: $store.draft.mountWaitSeconds)
                            .frame(width: 160)
                    }
                    GridRow {
                        Text(L10n.string("field.ata_idle_seconds"))
                            .foregroundStyle(.secondary)
                        TextField(L10n.string("field.ata_idle_seconds"), text: $store.draft.ataIdleSeconds)
                            .frame(width: 160)
                    }
                    GridRow {
                        Text(L10n.string("field.ata_standby"))
                            .foregroundStyle(.secondary)
                        TextField(L10n.string("field.ata_standby"), text: $store.draft.ataStandby)
                            .frame(width: 160)
                    }
                    GridRow {
                        Toggle(L10n.string("toggle.enable_nbns"), isOn: $store.draft.nbnsEnabled)
                        Toggle(L10n.string("toggle.internal_share_use_disk_root"), isOn: $store.draft.internalShareUseDiskRoot)
                    }
                    GridRow {
                        Toggle(L10n.string("toggle.smb_bind_lan_only"), isOn: $store.draft.smbBindLanOnly)
                        Toggle(L10n.string("toggle.smb_browse_compatibility"), isOn: $store.draft.smbBrowseCompatibility)
                    }
                    GridRow {
                        Toggle(L10n.string("toggle.mdns_advertise_afp"), isOn: $store.draft.mdnsAdvertiseAFP)
                        Toggle(L10n.string("toggle.use_netatalk_metadata"), isOn: $store.draft.fruitMetadataNetatalk)
                    }
                    GridRow {
                        Toggle(L10n.string("toggle.any_protocol"), isOn: anyProtocolBinding)
                            .disabled(!SMBProtocolOptionPolicy.allowsAnyProtocol(requireSMBEncryption: store.draft.requireSMBEncryption))
                        Toggle(L10n.string("toggle.require_smb_encryption"), isOn: requireSMBEncryptionBinding)
                            .disabled(!SMBProtocolOptionPolicy.allowsRequireSMBEncryption(anyProtocol: store.draft.anyProtocol))
                    }
                    Toggle(L10n.string("toggle.force_debug_logging"), isOn: $store.draft.debugLogging)
                }
            }
        }
    }

    private var anyProtocolBinding: Binding<Bool> {
        Binding(
            get: { store.draft.anyProtocol },
            set: { value in
                store.draft.anyProtocol = value
                if value {
                    store.draft.requireSMBEncryption = false
                }
            }
        )
    }

    private var requireSMBEncryptionBinding: Binding<Bool> {
        Binding(
            get: { store.draft.requireSMBEncryption },
            set: { value in
                store.draft.requireSMBEncryption = value
                if value {
                    store.draft.anyProtocol = false
                }
            }
        )
    }
}
