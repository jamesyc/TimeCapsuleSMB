import SwiftUI

struct AppSettingsView: View {
    @ObservedObject var appStore: AppStore
    @ObservedObject var editor: AppSettingsEditorStore

    private let contentWidth: CGFloat = 760

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                    .frame(maxWidth: contentWidth, alignment: .leading)

                SettingsFormSection(title: L10n.string("app_settings.section.general"), contentWidth: contentWidth) {
                    SettingsFormRow(title: L10n.string("app_settings.language")) {
                        Picker("", selection: $editor.draft.language) {
                            ForEach(AppLanguage.allCases) { language in
                                Text(language.title)
                                    .tag(language)
                            }
                        }
                        .labelsHidden()
                        .frame(width: 220)
                    }
                }

                SettingsFormSection(title: L10n.string("app_settings.section.defaults"), contentWidth: contentWidth) {
                    SettingsFormRow(title: L10n.string("app_settings.default_bonjour_timeout")) {
                        TextField("", text: $editor.draft.defaultBonjourTimeoutSeconds)
                            .frame(width: 120)
                    }
                    Toggle(L10n.string("toggle.enable_nbns"), isOn: $editor.draft.nbnsEnabled)
                    Toggle(L10n.string("toggle.internal_share_use_disk_root"), isOn: $editor.draft.internalShareUseDiskRoot)
                    Toggle(L10n.string("toggle.any_protocol"), isOn: $editor.draft.anyProtocol)
                    Toggle(L10n.string("toggle.force_debug_logging"), isOn: $editor.draft.debugLogging)
                    SettingsFormRow(title: L10n.string("field.mount_wait")) {
                        TextField("", text: $editor.draft.mountWaitSeconds)
                            .frame(width: 120)
                    }
                    SettingsFormRow(title: L10n.string("field.ata_idle_seconds")) {
                        TextField("", text: $editor.draft.ataIdleSeconds)
                            .frame(width: 120)
                    }
                    SettingsFormRow(title: L10n.string("field.ata_standby")) {
                        TextField(L10n.string("app_settings.blank_uses_device_default"), text: $editor.draft.ataStandby)
                            .frame(width: 180)
                    }
                }

                SettingsFormSection(title: L10n.string("app_settings.section.diagnostics"), contentWidth: contentWidth) {
                    SettingsFormRow(title: L10n.string("app_settings.helper_path")) {
                        TextField(L10n.string("value.auto"), text: $editor.draft.helperPathOverride)
                            .frame(maxWidth: 420)
                    }
                    Toggle(L10n.string("app_settings.show_raw_events"), isOn: $editor.draft.showRawBackendEventsByDefault)
                }

                SettingsFormSection(title: L10n.string("app_settings.section.updates"), contentWidth: contentWidth) {
                    Toggle(L10n.string("app_settings.check_updates_on_launch"), isOn: $editor.draft.checkForUpdatesOnLaunch)
                    SettingsFormRow(title: L10n.string("app_settings.version_url")) {
                        TextField(L10n.string("value.auto"), text: $editor.draft.versionCheckURL)
                            .frame(maxWidth: 420)
                    }
                    HStack(spacing: 10) {
                        Button {
                            appStore.appUpdateStore.checkNow(settings: appStore.appSettingsStore.settings)
                        } label: {
                            Label(L10n.string("app_settings.check_now"), systemImage: "arrow.clockwise")
                        }
                        .disabled(appStore.appUpdateStore.isChecking)

                        if appStore.appUpdateStore.isChecking {
                            ProgressView()
                                .controlSize(.small)
                        }
                        Text(updateStatusText)
                            .font(.caption)
                            .foregroundStyle(updateStatusColor)
                    }
                }

                SettingsFormSection(title: L10n.string("app_settings.section.privacy"), contentWidth: contentWidth) {
                    Toggle(L10n.string("app_settings.telemetry_enabled"), isOn: $editor.draft.telemetryEnabled)
                }

                SettingsFormSection(title: L10n.string("app_settings.section.time_machine"), contentWidth: contentWidth) {
                    Toggle(L10n.string("app_settings.time_machine_warnings"), isOn: $editor.draft.timeMachineWarningsEnabled)
                }

                if let message = editor.validationError ?? editor.errorMessage ?? appStore.appSettingsStore.error?.localizedDescription {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .frame(maxWidth: contentWidth, alignment: .leading)
                }

                actionBar
                    .frame(maxWidth: contentWidth, alignment: .leading)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(L10n.string("app_settings.title"))
                .font(.title2.weight(.semibold))
            Text(L10n.string("app_settings.subtitle"))
                .foregroundStyle(.secondary)
        }
    }

    private var actionBar: some View {
        HStack(spacing: 10) {
            Button {
                Task { await editor.save(appStore: appStore) }
            } label: {
                Label(L10n.string("app_settings.save"), systemImage: "checkmark.circle")
            }
            .buttonStyle(.borderedProminent)
            .disabled(!editor.canSave)

            Button(L10n.string("app_settings.reset_saved")) {
                editor.resetDraft()
            }
            .disabled(editor.isSaving || !editor.hasChanges)

            Button(L10n.string("app_settings.restore_defaults")) {
                editor.restoreDefaultsDraft()
            }
            .disabled(editor.isSaving)

            if editor.isSaving {
                ProgressView()
                    .controlSize(.small)
            }
        }
    }

    private var updateStatusText: String {
        if let payload = appStore.appUpdateStore.payload {
            return payload.localizedSummary
        }
        if let error = appStore.appUpdateStore.error {
            return error.message
        }
        return appStore.appUpdateStore.state.title
    }

    private var updateStatusColor: Color {
        switch appStore.appUpdateStore.state {
        case .updateAvailable, .unavailable, .failed:
            return .yellow
        case .current:
            return .green
        default:
            return .secondary
        }
    }
}

private struct SettingsFormSection<Content: View>: View {
    let title: String
    let contentWidth: CGFloat
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 10) {
                Text(title)
                    .font(.headline)
                VStack(alignment: .leading, spacing: 8) {
                    content()
                }
            }
            .frame(maxWidth: contentWidth, alignment: .leading)
            Divider()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct SettingsFormRow<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Text(title)
                .frame(width: 220, alignment: .leading)
                .foregroundStyle(.secondary)
            content()
            Spacer(minLength: 0)
        }
    }
}
