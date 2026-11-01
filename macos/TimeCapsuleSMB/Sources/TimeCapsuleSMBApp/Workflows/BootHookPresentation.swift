import Foundation

/// What the boot hook panel shows beyond the parts every maintenance panel has.
enum BootHookPresentation {
    /// The panel's status line.
    @MainActor
    static func statusMessage(store: MaintenanceStore) -> String {
        let bootHook = store.bootHookStore
        // An error shows in its own box below, not in this line.
        return bootHook.writeResult?.localizedSummary
            // Only while Write Patch can be pressed: during the confirmation
            // and the write, the plan's own summary is still true.
            ?? (store.canWritePatch ? L10n.string("flash.message.patch_plan_ready") : nil)
            ?? bootHook.plan?.localizedSummary
            ?? bootHook.backup?.localizedSummary
            ?? L10n.string("flash.status.ready")
    }

    @MainActor
    static func rows(store: BootHookStore) -> [PresentationRow] {
        var rows: [PresentationRow] = []
        if let backup = store.backup {
            rows.append(PresentationRow(label: L10n.string("flash.row.backup_dir"), value: backup.backupDir))
            // After a write this backup no longer describes the banks; the
            // stale-snapshot warning asks for a new one.
            if !store.backupSnapshotStale {
                // The helper names a bank only when one alone passes its checks
                // (or the running LOGIN script picks it); with both passing
                // there is none to show.
                if let activeBank = backup.activeBank {
                    rows.append(PresentationRow(label: L10n.string("flash.row.active_bank"), value: bankName(activeBank)))
                }
                let primary = backup.banks.first { $0.name == "primary" }
                let secondary = backup.banks.first { $0.name == "secondary" }
                rows.append(PresentationRow(label: L10n.string("flash.row.firmware_banks"), value: bankHealth(primary?.backupValid ?? false, secondary?.backupValid ?? false)))
                rows.append(PresentationRow(label: L10n.string("flash.row.boot_hook"), value: bootHook(primary)))
            }
        }
        if let plan = store.plan {
            rows.append(PresentationRow(label: L10n.string("flash.row.mode"), value: plan.mode.title))
            rows.append(PresentationRow(label: L10n.string("flash.row.write_requested"), value: plan.writeRequested ? L10n.string("value.yes") : L10n.string("value.no")))
            if let match = plan.appleFirmwareMatch {
                if plan.appleFirmwareMatches.count > 1 {
                    for result in plan.appleFirmwareMatches {
                        let bank = result.bank.isEmpty ? L10n.string("value.unknown") : result.bank
                        rows.append(PresentationRow(
                            label: L10n.format("flash.row.apple_match_bank", bank),
                            value: result.match.matched ? L10n.string("value.yes") : L10n.string("value.no")
                        ))
                    }
                } else {
                    rows.append(PresentationRow(label: L10n.string("flash.row.apple_match"), value: match.matched ? L10n.string("value.yes") : L10n.string("value.no")))
                }
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_version"), value: match.templateVersion)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_product"), value: match.templateProductID)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_source"), value: match.templateSource)
                appendIfPresent(&rows, label: L10n.string("flash.row.apple_payload_sha256"), value: match.innerSHA256)
            }
            if let payload = plan.firmwarePayload {
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_version"), value: payload.templateVersion)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_product"), value: payload.templateProductID)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_source"), value: payload.templateSource)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_payload_path"), value: plan.firmwarePayloadPath)
                appendIfPresent(&rows, label: L10n.string("flash.row.firmware_payload_sha256"), value: payload.payloadSHA256)
                if let payloadSize = payload.payloadSize {
                    rows.append(PresentationRow(label: L10n.string("flash.row.firmware_payload_size"), value: Self.byteCount(payloadSize)))
                }
            }
        }
        if let result = store.writeResult {
            rows.append(PresentationRow(label: L10n.string("flash.row.write_status"), value: result.writeStatus))
            rows.append(PresentationRow(label: L10n.string("flash.row.write_validated"), value: result.writeValidated ? L10n.string("value.yes") : L10n.string("value.no")))
        }
        return rows
    }

    private static func bankName(_ name: String) -> String {
        switch name {
        case "primary":
            return L10n.string("flash.bank.primary")
        case "secondary":
            return L10n.string("flash.bank.secondary")
        default:
            return name
        }
    }

    // Both banks passing every check is the normal state, and then no bank can
    // be named active; a patch writes the primary bank either way.
    private static func bankHealth(_ primaryValid: Bool, _ secondaryValid: Bool) -> String {
        switch (primaryValid, secondaryValid) {
        case (true, true):
            return L10n.string("flash.banks.both_valid")
        case (true, false):
            return L10n.string("flash.banks.secondary_failed")
        case (false, true):
            return L10n.string("flash.banks.primary_failed")
        case (false, false):
            return L10n.string("flash.banks.both_failed")
        }
    }

    // The patch replaces the primary bank's LOGIN script, so that bank says
    // whether the hook is in place.
    private static func bootHook(_ primary: FlashBankPayload?) -> String {
        switch primary?.login?.stringValue(for: "classification") {
        case "stock":
            return L10n.string("flash.boot_hook.not_installed")
        case "already_patched":
            return L10n.string("flash.boot_hook.installed")
        default:
            return L10n.string("flash.boot_hook.not_recognized")
        }
    }

    private static func appendIfPresent(_ rows: inout [PresentationRow], label: String, value: String?) {
        guard let value else {
            return
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return
        }
        rows.append(PresentationRow(label: label, value: trimmed))
    }

    private static func byteCount(_ value: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file)
    }

    @MainActor
    static func warnings(store: BootHookStore) -> [String] {
        var warnings: [String] = []
        if store.backupSnapshotStale {
            warnings.append(L10n.string("flash.warning.snapshot_stale"))
        }
        if store.manualPowerCycleRequiredAfterWrite {
            warnings.append(L10n.string("flash.warning.manual_power_cycle"))
        }
        if let plan = store.plan {
            warnings.append(contentsOf: plan.warnings)
        }
        return warnings
    }
}

extension FlashPlanMode {
    var title: String {
        switch self {
        case .patch:
            return L10n.string("flash.mode.patch")
        case .restore:
            return L10n.string("flash.mode.restore")
        case .checkApple:
            return L10n.string("flash.mode.check_apple")
        case .downloadOnly:
            return L10n.string("flash.mode.download_only")
        }
    }
}
