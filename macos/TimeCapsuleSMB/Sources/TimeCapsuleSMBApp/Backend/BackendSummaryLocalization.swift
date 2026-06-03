import Foundation

enum BackendSummaryLocalization {
    static func localized(_ summary: String, operation: String, payload: JSONValue? = nil) -> String {
        if let payload, let structured = localizedStructuredSummary(operation: operation, payload: payload) {
            return structured
        }
        return localizedKnownSummary(summary)
    }

    private static func localizedKnownSummary(_ summary: String) -> String {
        let normalized = summary.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch normalized {
        case "helper capabilities resolved.":
            return L10n.string("backend.summary.helper_capabilities_resolved")
        case "install validation passed.":
            return L10n.string("backend.summary.install_validation_passed")
        case "install validation failed.":
            return L10n.string("backend.summary.install_validation_failed")
        case "telemetry is enabled.":
            return L10n.string("backend.summary.telemetry_enabled")
        case "telemetry is disabled.":
            return L10n.string("backend.summary.telemetry_disabled")
        case "update required.":
            return L10n.string("backend.summary.update_required")
        case "timecapsulesmb is up to date.":
            return L10n.string("backend.summary.up_to_date")
        case "version metadata is unavailable.":
            return L10n.string("backend.summary.version_metadata_unavailable")
        case "configuration saved and ssh authentication verified.":
            return L10n.string("backend.summary.configuration_saved")
        case "deployment dry-run plan generated.":
            return L10n.string("backend.summary.deploy_plan_generated")
        case "deployment completed.":
            return L10n.string("backend.summary.deploy_completed")
        case "netbsd4 activation dry-run plan generated.":
            return L10n.string("backend.summary.activation_plan_generated")
        case "netbsd4 payload was already active.":
            return L10n.string("backend.summary.activation_already_active")
        case "netbsd4 activation completed.":
            return L10n.string("backend.summary.activation_completed")
        case "uninstall dry-run plan generated.":
            return L10n.string("backend.summary.uninstall_plan_generated")
        case "uninstall completed.":
            return L10n.string("backend.summary.uninstall_completed")
        case "uninstall completed without post-reboot verification.":
            return L10n.string("backend.summary.uninstall_unverified")
        case "fsck dry-run plan generated.", "dry-run plan generated for fsck.":
            return L10n.string("backend.summary.fsck_plan_generated")
        case "fsck completed.", "disk repair completed with fsck.":
            return L10n.string("backend.summary.fsck_completed")
        case "flash plan is already satisfied; no write is needed.":
            return L10n.string("backend.summary.flash_plan_already_satisfied")
        case "flash write was not needed.":
            return L10n.string("backend.summary.flash_write_not_needed")
        case "flash patch write validated; manual power cycle required.":
            return L10n.string("backend.summary.flash_patch_write_validated_power_cycle")
        case "flash restore write validated; device rebooted.":
            return L10n.string("backend.summary.flash_restore_write_validated_rebooted")
        case "flash restore write validated; reboot requested.":
            return L10n.string("backend.summary.flash_restore_write_validated_reboot_requested")
        case "flash restore write validated; manual reboot required.":
            return L10n.string("backend.summary.flash_restore_write_validated_manual_reboot")
        case "flash write completed.":
            return L10n.string("backend.summary.flash_write_completed")
        case "doctor checks passed.":
            return L10n.string("backend.summary.doctor_checks_passed")
        case "doctor found one or more fatal problems.":
            return L10n.string("backend.summary.doctor_found_fatal")
        case "operation exited.":
            return L10n.string("backend.summary.operation_exited")
        case "ssh reachable; smb port reachable.":
            return L10n.string("backend.summary.reachability.all_reachable")
        case "ssh reachable, smb port closed.":
            return L10n.string("backend.summary.reachability.ssh_only")
        case "smb port reachable, ssh closed.":
            return L10n.string("backend.summary.reachability.smb_only")
        case "could not reach ssh or smb.":
            return L10n.string("backend.summary.reachability.unreachable")
        default:
            return summary
        }
    }

    private static func localizedStructuredSummary(operation: String, payload: JSONValue) -> String? {
        switch operation {
        case "capabilities":
            return L10n.string("backend.summary.helper_capabilities_resolved")
        case "validate-install":
            return payload.bool("ok").map { installValidationSummary(ok: $0) }
        case "set-telemetry":
            return payload.bool("telemetry_enabled").map { telemetrySummary(enabled: $0) }
        case "version-check":
            return versionSummary(
                source: payload.string("source"),
                shouldBlock: payload.bool("should_block"),
                updateAvailable: payload.bool("update_available")
            )
        case "discover":
            return payload.count("devices").map { discoveredDevicesSummary(count: $0) }
        case "configure":
            return L10n.string("backend.summary.configuration_saved")
        case "deploy":
            return deploySummary(payload: payload)
        case "doctor":
            return payload.bool("fatal").map(doctorSummary)
        case "activate":
            return activationSummary(payload: payload)
        case "uninstall":
            return uninstallSummary(payload: payload)
        case "fsck":
            return fsckSummary(payload: payload)
        case "repair-xattrs":
            let findings = payload.int("finding_count") ?? payload.count("findings")
            let repairable = payload.int("repairable_count") ?? payload.count("repairable")
            guard findings != nil || repairable != nil else {
                return nil
            }
            return repairXattrsSummary(findings: findings, repairable: repairable)
        case "flash":
            return flashSummary(payload: payload)
        case "reachability":
            return reachabilitySummary(status: payload.string("status"), summary: payload.string("summary"))
        default:
            return nil
        }
    }

    static func installValidationSummary(ok: Bool?) -> String {
        ok == false
            ? L10n.string("backend.summary.install_validation_failed")
            : L10n.string("backend.summary.install_validation_passed")
    }

    static func telemetrySummary(enabled: Bool?) -> String {
        enabled == false
            ? L10n.string("backend.summary.telemetry_disabled")
            : L10n.string("backend.summary.telemetry_enabled")
    }

    static func versionSummary(source: String?, shouldBlock: Bool?, updateAvailable: Bool?) -> String {
        if source == "unavailable" {
            return L10n.string("backend.summary.version_metadata_unavailable")
        }
        if shouldBlock == true {
            return L10n.string("backend.summary.update_required")
        }
        if updateAvailable == true {
            return L10n.string("backend.summary.update_available")
        }
        return L10n.string("backend.summary.up_to_date")
    }

    static func discoveredDevicesSummary(count: Int?) -> String {
        L10n.format("backend.summary.discovered_devices", count ?? 0)
    }

    static func deployResultSummary(summary: String, message: String?, netbsd4: Bool) -> String {
        if isNetBSD4ActivationMessage(message ?? summary) {
            return netbsd4ActivationCompletedWithFollowup()
        }
        return localizedKnownSummary(message ?? summary)
    }

    static func activationResultSummary(summary: String, message: String?, alreadyActive: Bool) -> String {
        if alreadyActive {
            return L10n.string("backend.summary.activation_already_active")
        }
        if isNetBSD4ActivationMessage(message ?? summary) {
            return netbsd4ActivationCompletedWithFollowup()
        }
        return localizedKnownSummary(message ?? summary)
    }

    static func hfsVolumesFoundSummary(count: Int) -> String {
        L10n.format("backend.summary.hfs_volumes_found", count)
    }

    static func repairXattrsSummary(findings: Int?, repairable: Int?) -> String {
        L10n.format("backend.summary.repair_xattrs_found", findings ?? 0, repairable ?? 0)
    }

    static func flashBackupSummary(backupDir: String?) -> String {
        L10n.format("backend.summary.flash_backup_saved", backupDir ?? L10n.string("value.unknown"))
    }

    static func flashPlanSummary(
        mode: String,
        alreadySatisfied: Bool,
        writeRequested: Bool,
        appleFirmwareMatch: FlashAppleFirmwareMatchPayload?,
        firmwarePayload: FlashFirmwarePayload?
    ) -> String {
        if mode == "check_apple" {
            let version = appleFirmwareMatch?.templateVersion
            if appleFirmwareMatch?.matched == true {
                return L10n.format("backend.summary.flash_apple_stock_matches", flashVersionSuffix(version))
            }
            return L10n.format("backend.summary.flash_apple_stock_mismatch", flashVersionSuffix(version))
        }
        if mode == "download_only" {
            return L10n.format(
                "backend.summary.flash_apple_restore_validated",
                flashAppleRestoreDetail(version: firmwarePayload?.templateVersion, product: firmwarePayload?.templateProductID)
            )
        }
        if alreadySatisfied {
            return L10n.string("backend.summary.flash_plan_already_satisfied")
        }
        let modeText = flashModeText(mode)
        if writeRequested {
            return L10n.format("backend.summary.flash_write_plan_generated", modeText)
        }
        return L10n.format("backend.summary.flash_plan_generated", modeText)
    }

    static func flashWriteSummary(
        mode: String,
        writeStatus: String,
        writeValidated: Bool,
        postWriteAction: String,
        rebootRequested: Bool,
        rebooted: Bool
    ) -> String {
        if writeStatus == "not_needed" {
            return L10n.string("backend.summary.flash_write_not_needed")
        }
        if writeValidated && mode == "patch" {
            return L10n.string("backend.summary.flash_patch_write_validated_power_cycle")
        }
        if writeValidated && mode == "restore" {
            if postWriteAction == "ssh_reboot" && rebooted {
                return L10n.string("backend.summary.flash_restore_write_validated_rebooted")
            }
            if postWriteAction == "ssh_reboot" && rebootRequested {
                return L10n.string("backend.summary.flash_restore_write_validated_reboot_requested")
            }
            return L10n.string("backend.summary.flash_restore_write_validated_manual_reboot")
        }
        if writeValidated {
            return L10n.format("backend.summary.flash_write_validated", flashModeText(mode))
        }
        return L10n.string("backend.summary.flash_write_completed")
    }

    static func reachabilitySummary(status: String?, summary: String?) -> String {
        if let summary {
            let localized = localizedKnownSummary(summary)
            if localized != summary {
                return localized
            }
        }
        switch status {
        case "reachable":
            return L10n.string("backend.summary.reachability.all_reachable")
        case "partial":
            return summary ?? L10n.string("backend.summary.reachability.partial")
        case "unreachable":
            return L10n.string("backend.summary.reachability.unreachable")
        default:
            return summary ?? L10n.string("value.unknown")
        }
    }

    private static func deploySummary(payload: JSONValue) -> String? {
        if payload.array("uploads") != nil || payload.array("post_deploy_checks") != nil {
            return L10n.string("backend.summary.deploy_plan_generated")
        }
        if isNetBSD4ActivationMessage(payload.string("message") ?? payload.string("summary")) {
            return netbsd4ActivationCompletedWithFollowup()
        }
        return nil
    }

    private static func doctorSummary(fatal: Bool) -> String {
        fatal ? L10n.string("backend.summary.doctor_found_fatal") : L10n.string("backend.summary.doctor_checks_passed")
    }

    private static func activationSummary(payload: JSONValue) -> String? {
        if payload.array("actions") != nil || payload.array("post_activation_checks") != nil {
            return L10n.string("backend.summary.activation_plan_generated")
        }
        return activationResultSummary(
            summary: payload.string("summary") ?? "",
            message: payload.string("message"),
            alreadyActive: payload.bool("already_active") == true
        )
    }

    private static func uninstallSummary(payload: JSONValue) -> String? {
        if payload.array("remote_actions") != nil || payload.array("payload_dirs") != nil {
            return L10n.string("backend.summary.uninstall_plan_generated")
        }
        if let verified = payload.bool("verified") {
            return verified
                ? L10n.string("backend.summary.uninstall_completed")
                : L10n.string("backend.summary.uninstall_unverified")
        }
        if let summary = payload.string("summary") {
            return localizedKnownSummary(summary)
        }
        return nil
    }

    private static func fsckSummary(payload: JSONValue) -> String? {
        if let targetCount = payload.count("targets") {
            return hfsVolumesFoundSummary(count: targetCount)
        }
        if payload.string("device") != nil || payload.string("mountpoint") != nil {
            return L10n.string("backend.summary.fsck_completed")
        }
        if payload.object("target") != nil {
            return L10n.string("backend.summary.fsck_plan_generated")
        }
        return nil
    }

    private static func flashSummary(payload: JSONValue) -> String? {
        if let writeStatus = payload.string("write_status") {
            return flashWriteSummary(
                mode: payload.string("mode") ?? "unknown",
                writeStatus: writeStatus,
                writeValidated: payload.bool("write_validated") == true,
                postWriteAction: payload.string("post_write_action") ?? "",
                rebootRequested: payload.bool("reboot_requested") == true,
                rebooted: payload.bool("rebooted") == true
            )
        }
        if let mode = payload.string("mode") {
            let appleFirmwareMatch = try? payload.object("apple_firmware_match")?.decode(FlashAppleFirmwareMatchPayload.self)
            let firmwarePayload = try? payload.object("firmware_payload")?.decode(FlashFirmwarePayload.self)
            return flashPlanSummary(
                mode: mode,
                alreadySatisfied: payload.bool("already_satisfied") == true,
                writeRequested: payload.bool("write_requested") == true,
                appleFirmwareMatch: appleFirmwareMatch,
                firmwarePayload: firmwarePayload
            )
        }
        if payload.string("backup_dir") != nil {
            return flashBackupSummary(backupDir: payload.string("backup_dir"))
        }
        return nil
    }

    private static func isNetBSD4ActivationMessage(_ message: String?) -> Bool {
        message?.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .hasPrefix("netbsd4 activation complete.") == true
    }

    private static func netbsd4ActivationCompletedWithFollowup() -> String {
        L10n.format(
            "backend.summary.activation_completed_with_followup",
            L10n.string("backend.summary.activation_followup")
        )
    }

    private static func flashModeText(_ mode: String) -> String {
        switch mode {
        case "patch":
            return L10n.string("backend.summary.flash_mode.patch")
        case "restore":
            return L10n.string("backend.summary.flash_mode.restore")
        case "check_apple":
            return L10n.string("backend.summary.flash_mode.check_apple")
        case "download_only":
            return L10n.string("backend.summary.flash_mode.download_only")
        default:
            return mode
        }
    }

    private static func flashVersionSuffix(_ version: String?) -> String {
        guard let version, !version.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return ""
        }
        return L10n.format("backend.summary.flash_version_suffix", version)
    }

    private static func flashAppleRestoreDetail(version: String?, product: String?) -> String {
        var parts: [String] = []
        if let version, !version.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            parts.append(L10n.format("backend.summary.flash_apple_restore_version", version))
        }
        if let product, !product.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            parts.append(L10n.format("backend.summary.flash_apple_restore_product", product))
        }
        guard !parts.isEmpty else {
            return ""
        }
        return L10n.format(
            "backend.summary.flash_apple_restore_detail",
            parts.joined(separator: L10n.string("value.list_separator"))
        )
    }
}

extension CapabilitiesPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.helper_capabilities_resolved")
    }
}

extension InstallValidationPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.installValidationSummary(ok: ok)
    }
}

extension VersionCheckPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.versionSummary(
            source: source,
            shouldBlock: shouldBlock,
            updateAvailable: updateAvailable
        )
    }
}

extension ReachabilityPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.reachabilitySummary(status: status, summary: summary)
    }
}

extension DiscoverPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.discoveredDevicesSummary(count: devices.count)
    }
}

extension ConfigurePayload {
    var localizedSummary: String {
        L10n.string("backend.summary.configuration_saved")
    }
}

extension DeployPlanPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.deploy_plan_generated")
    }
}

extension DeployResultPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.deployResultSummary(summary: summary, message: message, netbsd4: netbsd4)
    }

    var localizedMessage: String {
        localizedSummary
    }
}

extension DoctorPayload {
    var localizedSummary: String {
        fatal ? L10n.string("backend.summary.doctor_found_fatal") : L10n.string("backend.summary.doctor_checks_passed")
    }
}

extension ActivationPlanPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.activation_plan_generated")
    }
}

extension ActivationResultPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.activationResultSummary(summary: summary, message: message, alreadyActive: alreadyActive)
    }

    var localizedMessage: String {
        localizedSummary
    }
}

extension UninstallPlanPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.uninstall_plan_generated")
    }
}

extension MaintenanceResultPayload {
    var localizedUninstallSummary: String {
        verified == false
            ? L10n.string("backend.summary.uninstall_unverified")
            : L10n.string("backend.summary.uninstall_completed")
    }
}

extension FsckVolumeListPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.hfsVolumesFoundSummary(count: targets.count)
    }
}

extension FsckPlanPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.fsck_plan_generated")
    }
}

extension FsckResultPayload {
    var localizedSummary: String {
        L10n.string("backend.summary.fsck_completed")
    }
}

extension RepairXattrsPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.repairXattrsSummary(findings: findingCount, repairable: repairableCount)
    }
}

extension FlashBackupPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.flashBackupSummary(backupDir: backupDir)
    }
}

extension FlashPlanPayload {
    var localizedSummary: String {
        BackendSummaryLocalization.flashPlanSummary(
            mode: mode.rawValue,
            alreadySatisfied: alreadySatisfied,
            writeRequested: writeRequested,
            appleFirmwareMatch: appleFirmwareMatch,
            firmwarePayload: firmwarePayload
        )
    }
}

extension FlashWritePayload {
    var localizedSummary: String {
        BackendSummaryLocalization.flashWriteSummary(
            mode: mode.rawValue,
            writeStatus: writeStatus,
            writeValidated: writeValidated,
            postWriteAction: postWriteAction,
            rebootRequested: rebootRequested,
            rebooted: rebooted
        )
    }
}

private extension JSONValue {
    func string(_ key: String) -> String? {
        stringValue(for: key)
    }

    func bool(_ key: String) -> Bool? {
        guard case .object(let values) = self, case .bool(let value)? = values[key] else {
            return nil
        }
        return value
    }

    func int(_ key: String) -> Int? {
        guard case .object(let values) = self, let value = values[key] else {
            return nil
        }
        switch value {
        case .number(let number) where number.isFinite:
            return Int(number)
        case .string(let string):
            return Int(string.trimmingCharacters(in: .whitespacesAndNewlines))
        default:
            return nil
        }
    }

    func array(_ key: String) -> [JSONValue]? {
        guard case .object(let values) = self, case .array(let array)? = values[key] else {
            return nil
        }
        return array
    }

    func object(_ key: String) -> JSONValue? {
        guard case .object(let values) = self, case .object? = values[key] else {
            return nil
        }
        return values[key]
    }

    func count(_ key: String) -> Int? {
        if let array = array(key) {
            return array.count
        }
        guard case .object(let values) = self,
              case .object(let counts)? = values["counts"],
              let value = counts[key] else {
            return nil
        }
        switch value {
        case .number(let number) where number.isFinite:
            return Int(number)
        case .string(let string):
            return Int(string.trimmingCharacters(in: .whitespacesAndNewlines))
        default:
            return nil
        }
    }
}
