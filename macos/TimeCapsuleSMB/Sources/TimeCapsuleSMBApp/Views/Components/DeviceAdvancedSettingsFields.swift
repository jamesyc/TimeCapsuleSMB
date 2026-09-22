import SwiftUI

struct DeviceAdvancedSettingsFields: View {
    @Binding var rsyncEnabled: Bool
    @Binding var internalShareUseDiskRoot: Bool
    @Binding var smbBrowseCompatibility: Bool
    @Binding var mdnsAdvertiseAFP: Bool
    @Binding var anyProtocol: Bool
    @Binding var requireSMBEncryption: Bool
    @Binding var forceDisableSMBSigningAndEncryption: Bool
    @Binding var fruitMetadataNetatalk: Bool
    @Binding var vfsAIOForkEnabled: Bool
    @Binding var debugLogging: Bool
    @Binding var mountWaitSeconds: String
    @Binding var ataIdleSeconds: String
    @Binding var ataStandby: String

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            group("settings.group.disk") {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
                    timing("settings.mount", note: "settings.note.mount", text: $mountWaitSeconds)
                    timing("settings.idle", note: "settings.note.idle", text: $ataIdleSeconds)
                    timing("settings.standby", note: "settings.note.standby", text: $ataStandby,
                           placeholder: L10n.string("settings.unchanged"))
                }
            }
            group("settings.group.sharing") {
                note("settings.note.nbns")
                option("toggle.enable_rsync", note: "settings.note.rsync", isOn: $rsyncEnabled)
                option("toggle.smb_browse_compatibility", note: "settings.note.browsing", isOn: $smbBrowseCompatibility)
                option("toggle.mdns_advertise_afp", note: "settings.note.afp", isOn: $mdnsAdvertiseAFP)
                    .help(L10n.string("toggle.mdns_advertise_afp.help"))
            }
            group("settings.group.storage") {
                option("toggle.internal_share_use_disk_root", note: "settings.note.root", isOn: $internalShareUseDiskRoot)
                option("toggle.use_netatalk_metadata", note: "settings.note.metadata", isOn: $fruitMetadataNetatalk)
            }
            group("settings.group.security") {
                option("toggle.any_protocol", note: "settings.note.protocol", isOn: anyProtocolBinding)
                    .disabled(!SMBProtocolOptionPolicy.allowsAnyProtocol(requireSMBEncryption: requireSMBEncryption))
                option("toggle.require_smb_encryption", note: "settings.note.encryption", isOn: requireSMBEncryptionBinding)
                    .disabled(!SMBProtocolOptionPolicy.allowsRequireSMBEncryption(
                        anyProtocol: anyProtocol,
                        forceDisableSMBSigningAndEncryption: forceDisableSMBSigningAndEncryption))
                option("toggle.force_disable_smb_signing_and_encryption",
                       note: "toggle.force_disable_smb_signing_and_encryption.note",
                       isOn: forceDisableSMBSigningAndEncryptionBinding)
                    .disabled(!SMBProtocolOptionPolicy.allowsForceDisableSMBSigningAndEncryption(
                        requireSMBEncryption: requireSMBEncryption))
            }
            group("settings.group.performance") {
                option("toggle.enable_vfs_aio_fork", note: "settings.note.aio", isOn: $vfsAIOForkEnabled)
                option("toggle.force_debug_logging", note: "settings.note.debug", isOn: $debugLogging)
            }
        }
        .frame(maxWidth: 680, alignment: .leading)
    }

    private func group<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(L10n.string(title)).font(.headline)
            VStack(alignment: .leading, spacing: 10, content: content)
                .padding(.leading, 14)
        }
    }

    private func note(_ key: String) -> some View {
        Text(L10n.string(key))
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func option(_ title: String, note key: String, isOn: Binding<Bool>) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Toggle(L10n.string(title), isOn: isOn)
                .toggleStyle(.checkbox)
            note(key).padding(.leading, 20)
        }
    }

    private func timing(_ title: String, note key: String, text: Binding<String>, placeholder: String = "") -> some View {
        GridRow(alignment: .firstTextBaseline) {
            Text(L10n.string(title)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    TextField(placeholder, text: text)
                        .accessibilityLabel(L10n.string(title))
                        .frame(width: 110)
                    Text(L10n.string("settings.seconds")).foregroundStyle(.secondary)
                }
                note(key)
            }
        }
    }

    private var anyProtocolBinding: Binding<Bool> {
        Binding(get: { anyProtocol }, set: { value in
            anyProtocol = value
            if value { requireSMBEncryption = false }
        })
    }

    private var requireSMBEncryptionBinding: Binding<Bool> {
        Binding(get: { requireSMBEncryption }, set: { value in
            requireSMBEncryption = value
            if value {
                anyProtocol = false
                forceDisableSMBSigningAndEncryption = false
            }
        })
    }

    private var forceDisableSMBSigningAndEncryptionBinding: Binding<Bool> {
        Binding(get: { forceDisableSMBSigningAndEncryption }, set: { value in
            forceDisableSMBSigningAndEncryption = value
            if value { requireSMBEncryption = false }
        })
    }
}
