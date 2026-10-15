import Foundation

struct OperationTimelineItem: Equatable, Identifiable {
    enum State: String, Equatable {
        case pending
        case running
        case succeeded
        case warning
        case failed
    }

    let id: String
    let operation: String
    let title: String
    let detail: String?
    let state: State
    let risk: String?
    let cancellable: Bool?
    /// Shown under the detail while the step runs, without the running animation.
    var note: String? = nil
    /// Set while a long step runs: the row counts its time from here.
    var elapsedSince: Date? = nil
}

private struct OperationStageLocalization {
    let titleKey: String
    let detailKey: String
}

enum OperationTimelineBuilder {
    private static let activateStageLocalizations: [String: OperationStageLocalization] = [
        "probe_runtime": .init(titleKey: "timeline.activate.title.probe_runtime", detailKey: "timeline.activate.detail.probe_runtime")
    ]

    private static let deployStageLocalizations: [String: OperationStageLocalization] = [
        "load_config": .init(titleKey: "timeline.deploy.title.load_config", detailKey: "timeline.deploy.detail.load_config"),
        "resolve_managed_target": .init(titleKey: "timeline.deploy.title.resolve_managed_target", detailKey: "timeline.deploy.detail.resolve_managed_target"),
        "validate_artifacts": .init(titleKey: "timeline.deploy.title.validate_artifacts", detailKey: "timeline.deploy.detail.validate_artifacts"),
        "check_compatibility": .init(titleKey: "timeline.deploy.title.check_compatibility", detailKey: "timeline.deploy.detail.check_compatibility"),
        "read_mast": .init(titleKey: "timeline.deploy.title.read_mast", detailKey: "timeline.deploy.detail.read_mast"),
        "select_payload_home": .init(titleKey: "timeline.deploy.title.select_payload_home", detailKey: "timeline.deploy.detail.select_payload_home"),
        "inventory_legacy_metadata": .init(titleKey: "timeline.deploy.title.inventory_legacy_metadata", detailKey: "timeline.deploy.detail.inventory_legacy_metadata"),
        "build_deployment_plan": .init(titleKey: "timeline.deploy.title.build_deployment_plan", detailKey: "timeline.deploy.detail.build_deployment_plan"),
        "check_flash_capacity": .init(titleKey: "timeline.deploy.title.check_flash_capacity", detailKey: "timeline.deploy.detail.check_flash_capacity"),
        "pre_upload_actions": .init(titleKey: "timeline.deploy.title.pre_upload_actions", detailKey: "timeline.deploy.detail.pre_upload_actions"),
        "prepare_deployment_files": .init(titleKey: "timeline.deploy.title.prepare_deployment_files", detailKey: "timeline.deploy.detail.prepare_deployment_files"),
        "upload_payload": .init(titleKey: "timeline.deploy.title.upload_payload", detailKey: "timeline.deploy.detail.upload_payload"),
        "upload_smbd": .init(titleKey: "timeline.deploy.title.upload_smbd", detailKey: "timeline.deploy.detail.upload_smbd"),
        "upload_xattr_migrator": .init(titleKey: "timeline.deploy.title.upload_xattr_migrator", detailKey: "timeline.deploy.detail.upload_xattr_migrator"),
        "inspect_migration_sources": .init(titleKey: "timeline.deploy.title.inspect_migration_sources", detailKey: "timeline.deploy.detail.inspect_migration_sources"),
        "wait_for_previous_migration": .init(titleKey: "timeline.deploy.title.wait_for_previous_migration", detailKey: "timeline.deploy.detail.wait_for_previous_migration"),
        "migrate_xattrs_copy": .init(titleKey: "timeline.deploy.title.migrate_xattrs_copy", detailKey: "timeline.deploy.detail.migrate_xattrs_copy"),
        "migrate_xattrs_cleanup": .init(titleKey: "timeline.deploy.title.migrate_xattrs_cleanup", detailKey: "timeline.deploy.detail.migrate_xattrs_cleanup"),
        "replace_software": .init(titleKey: "timeline.deploy.title.replace_software", detailKey: "timeline.deploy.detail.replace_software"),
        "install_runtime_config": .init(titleKey: "timeline.deploy.title.install_runtime_config", detailKey: "timeline.deploy.detail.install_runtime_config"),
        "upload_rsync": .init(titleKey: "timeline.deploy.title.upload_rsync", detailKey: "timeline.deploy.detail.upload_rsync"),
        "upload_boot_files": .init(titleKey: "timeline.deploy.title.upload_boot_files", detailKey: "timeline.deploy.detail.upload_boot_files"),
        "upload_runtime_config": .init(titleKey: "timeline.deploy.title.upload_runtime_config", detailKey: "timeline.deploy.detail.upload_runtime_config"),
        "post_upload_actions": .init(titleKey: "timeline.deploy.title.post_upload_actions", detailKey: "timeline.deploy.detail.post_upload_actions"),
        "verify_payload_upload": .init(titleKey: "timeline.deploy.title.verify_payload_upload", detailKey: "timeline.deploy.detail.verify_payload_upload"),
        "flush_payload_upload": .init(titleKey: "timeline.deploy.title.flush_payload_upload", detailKey: "timeline.deploy.detail.flush_payload_upload"),
        "enable_boot": .init(titleKey: "timeline.deploy.title.upload_boot_files", detailKey: "timeline.deploy.detail.upload_boot_files"),
        "flush_boot_hook": .init(titleKey: "timeline.deploy.title.flush_payload_upload", detailKey: "timeline.deploy.detail.flush_payload_upload"),
        "verify_payload_upload_after_sync": .init(titleKey: "timeline.deploy.title.verify_payload_upload_after_sync", detailKey: "timeline.deploy.detail.verify_payload_upload_after_sync"),
        "post_reboot_activation": .init(titleKey: "timeline.deploy.title.post_reboot_activation", detailKey: "timeline.deploy.detail.post_reboot_activation"),
        "verify_runtime_activation": .init(titleKey: "timeline.deploy.title.verify_runtime_activation", detailKey: "timeline.deploy.detail.verify_runtime_activation"),
        "verify_runtime_reboot": .init(titleKey: "timeline.deploy.title.verify_runtime_reboot", detailKey: "timeline.deploy.detail.verify_runtime_reboot")
    ]

    // Every operation that reboots the device goes through the helper's one
    // reboot path, so these stages read the same everywhere.
    private static let rebootStageLocalizations: [String: OperationStageLocalization] = [
        "reboot": .init(titleKey: "timeline.deploy.title.reboot", detailKey: "timeline.deploy.detail.reboot"),
        "wait_for_reboot_down": .init(titleKey: "timeline.deploy.title.wait_for_reboot_down", detailKey: "timeline.deploy.detail.wait_for_reboot_down"),
        "wait_for_reboot_up": .init(titleKey: "timeline.deploy.title.wait_for_reboot_up", detailKey: "timeline.deploy.detail.wait_for_reboot_up")
    ]

    // fsck starts file sharing again after its reboot the way deploy does, so
    // these stages read as deploy's.
    private static let postRebootStartStageLocalizations: [String: OperationStageLocalization] =
        deployStageLocalizations.filter { ["post_reboot_activation", "verify_runtime_activation"].contains($0.key) }

    private static let fsckStageLocalizations: [String: OperationStageLocalization] =
        postRebootStartStageLocalizations.merging([
            "run_fsck": .init(titleKey: "timeline.stage.repairing_disk", detailKey: "timeline.fsck.detail.run_fsck")
        ]) { _, new in new }

    private static let stageLocalizations: [String: [String: OperationStageLocalization]] = [
        "activate": activateStageLocalizations,
        "deploy": deployStageLocalizations,
        "fsck": fsckStageLocalizations
    ]

    /// Whether a running stage shows how long it has run and a note to leave
    /// the device alone: Disk Repair can spend an hour on one step.
    private static func isLongDeviceStep(operation: String, stage: String?) -> Bool {
        operation == "fsck" && stage == "run_fsck"
    }

    static func timeline(from events: [BackendEvent]) -> [OperationTimelineItem] {
        events.enumerated().compactMap { index, event in
            switch event.type {
            case "stage":
                let state = stageState(forEventAt: index, in: events)
                let long = state == .running && isLongDeviceStep(operation: event.operation, stage: event.stage)
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):\(event.stage ?? "stage")",
                    operation: event.operation,
                    title: stageTitle(for: event.operation, stage: event.stage),
                    detail: latestProgressDetail(forStageAt: index, in: events)
                        ?? stageDetail(for: event.operation, stage: event.stage, fallback: event.description),
                    state: state,
                    risk: event.risk,
                    cancellable: event.cancellable,
                    note: long ? L10n.string("timeline.fsck.keep_plugged_in") : nil,
                    elapsedSince: long ? event.receivedAt : nil
                )
            case "result":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):result",
                    operation: event.operation,
                    title: event.ok == true ? L10n.string("timeline.result.done") : L10n.string("timeline.result.failed"),
                    detail: event.localizedPayloadSummaryText ?? event.localizedSummary,
                    state: event.ok == true ? .succeeded : .failed,
                    risk: nil,
                    cancellable: nil
                )
            case "error":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):error",
                    operation: event.operation,
                    title: event.code == "confirmation_required"
                        ? L10n.string("timeline.error.needs_confirmation")
                        : L10n.string("timeline.error.needs_attention"),
                    detail: event.message,
                    state: event.code == "confirmation_required" ? .warning : .failed,
                    risk: event.risk,
                    cancellable: event.cancellable
                )
            default:
                return nil
            }
        }
    }

    /// The item a timeline shows for the stage in progress before any of its
    /// stage events has arrived.
    static func currentStageItem(
        _ stage: OperationStageState,
        fallback: String? = nil,
        state: OperationTimelineItem.State = .running
    ) -> OperationTimelineItem {
        let long = state == .running && isLongDeviceStep(operation: stage.operation, stage: stage.stage)
        return OperationTimelineItem(
            id: "current:\(stage.operation):\(stage.stage)",
            operation: stage.operation,
            title: stageTitle(for: stage.operation, stage: stage.stage),
            detail: stage.progressDetail ?? stageDetail(for: stage.operation, stage: stage.stage, fallback: fallback),
            state: state,
            risk: stage.risk,
            cancellable: stage.cancellable,
            note: long ? L10n.string("timeline.fsck.keep_plugged_in") : nil,
            elapsedSince: long ? stage.startedAt : nil
        )
    }

    /// The text of a progress event: "Files checked: N" for a migration, the
    /// fsck_hfs step for Disk Repair.
    static func progressDetail(for event: BackendEvent) -> String? {
        guard event.type == "progress" else {
            return nil
        }
        if let entries = event.entries {
            return L10n.format("timeline.progress.migration_files", entries)
        }
        if let phase = event.phase {
            return fsckPhaseDetail(phase, recheck: event.recheck == true)
        }
        return nil
    }

    /// The text of an fsck_hfs step the helper reports by name; nil for one
    /// this app has no text for, so the row keeps its last text.
    static func fsckPhaseDetail(_ phase: String, recheck: Bool) -> String? {
        let key = "timeline.fsck.phase.\(phase)"
        let step = L10n.string(key)
        guard step != key else {
            return nil
        }
        return recheck ? L10n.format("timeline.fsck.recheck", step) : step
    }

    /// "Running for 12 min", in the app's language.
    static func elapsedText(since start: Date, now: Date) -> String {
        let formatter = DateComponentsFormatter()
        formatter.allowedUnits = [.hour, .minute]
        formatter.unitsStyle = .short
        formatter.zeroFormattingBehavior = .dropLeading
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = L10n.currentLanguage.locale
        formatter.calendar = calendar
        let minutes = (max(now.timeIntervalSince(start), 0) / 60).rounded(.down)
        let duration = formatter.string(from: minutes * 60) ?? ""
        return L10n.format("timeline.fsck.elapsed", duration)
    }

    /// The last progress reported for a stage before the operation moved on.
    private static func latestProgressDetail(forStageAt index: Int, in events: [BackendEvent]) -> String? {
        let event = events[index]
        var detail: String?
        for later in events.dropFirst(index + 1) where later.operation == event.operation {
            if later.type == "stage" {
                break
            }
            if later.stage == event.stage, let text = progressDetail(for: later) {
                detail = text
            }
        }
        return detail
    }

    private static func stageState(forEventAt index: Int, in events: [BackendEvent]) -> OperationTimelineItem.State {
        let event = events[index]
        let laterEvents = events.dropFirst(index + 1).filter { $0.operation == event.operation }
        for laterEvent in laterEvents {
            switch laterEvent.type {
            case "stage":
                return .succeeded
            case "result":
                return laterEvent.ok == true ? .succeeded : .failed
            case "error" where laterEvent.code != "confirmation_required":
                return .failed
            default:
                continue
            }
        }
        return .running
    }

    static func operationTitle(_ operation: String) -> String {
        switch operation {
        case "discover":
            return L10n.string("timeline.operation.discovery")
        case "reachability":
            return L10n.string("timeline.operation.reachability")
        case "set-ssh":
            return L10n.string("timeline.operation.ssh_access")
        case "configure":
            return L10n.string("timeline.operation.configure")
        case "update-config-settings":
            return L10n.string("timeline.stage.saving_device")
        case "deploy":
            return L10n.string("timeline.operation.deploy")
        case "doctor":
            return L10n.string("timeline.operation.doctor")
        case "activate":
            return L10n.string("timeline.operation.activate")
        case "fsck":
            return L10n.string("timeline.operation.fsck")
        case "uninstall":
            return L10n.string("timeline.operation.uninstall")
        case "capabilities", "validate-install":
            return L10n.string("timeline.operation.readiness")
        case "set-telemetry":
            return L10n.string("timeline.operation.telemetry")
        case "version-check":
            return L10n.string("timeline.operation.version_check")
        case "flash":
            return L10n.string("timeline.operation.flash")
        default:
            return operation
        }
    }

    static func stageTitle(for operation: String, stage: String?) -> String {
        guard let stage else {
            return operationTitle(operation)
        }
        if let title = localizedStageTitle(for: operation, stage: stage) {
            return title
        }
        switch (operation, stage) {
        case ("discover", "bonjour_discovery"):
            return L10n.string("timeline.stage.finding_devices")
        case ("reachability", "build_candidates"):
            return L10n.string("timeline.stage.reachability_candidates")
        case ("reachability", "check_dns"):
            return L10n.string("timeline.stage.reachability_dns")
        case ("reachability", "check_ping"):
            return L10n.string("timeline.stage.reachability_ping")
        case ("reachability", "check_ssh_port"):
            return L10n.string("timeline.stage.reachability_ssh_port")
        case ("reachability", "check_ssh_auth"):
            return L10n.string("timeline.stage.reachability_ssh_auth")
        case ("reachability", "check_smb_port"):
            return L10n.string("timeline.stage.reachability_smb_port")
        case ("set-ssh", "probe_ssh"):
            return L10n.string("timeline.stage.checking_ssh")
        case ("set-ssh", "confirm_enable_ssh"):
            return L10n.string("timeline.stage.confirming_ssh_enable")
        case ("set-ssh", "acp_port_probe"):
            return L10n.string("timeline.stage.checking_airport_acp")
        case ("set-ssh", "acp_enable_ssh"):
            return L10n.string("timeline.stage.enabling_ssh")
        case ("configure", "ssh_probe"), ("configure", "ssh_probe_after_acp"):
            return L10n.string("timeline.stage.checking_ssh")
        case ("configure", "confirm_enable_ssh"):
            return L10n.string("timeline.stage.confirming_ssh_enable")
        case ("configure", "check_device_model"):
            return L10n.string("timeline.stage.checking_device_model")
        case ("configure", "select_target"), ("configure", "acp_port_probe"):
            return L10n.string("timeline.stage.checking_airport_acp")
        case ("configure", "acp_enable_ssh"):
            return L10n.string("timeline.stage.enabling_ssh")
        case ("configure", "write_env"):
            return L10n.string("timeline.stage.saving_device")
        case ("update-config-settings", "load_existing_config"),
             ("update-config-settings", "write_env"):
            return L10n.string("timeline.stage.saving_device")
        case ("doctor", "run_checks"):
            return L10n.string("timeline.stage.running_checkup")
        case ("doctor", "wait_for_device_startup"):
            return L10n.string("timeline.stage.waiting_for_device_startup")
        case ("doctor", "finish_checks"):
            return L10n.string("timeline.stage.finishing_checkup")
        case ("activate", "build_activation_plan"):
            return L10n.string("timeline.stage.planning_start_smb")
        case ("activate", "run_activation"):
            return L10n.string("timeline.stage.starting_smb")
        case ("uninstall", "build_uninstall_plan"):
            return L10n.string("timeline.stage.planning_uninstall")
        case ("uninstall", "uninstall_payload"):
            return L10n.string("timeline.stage.removing_managed_files")
        case ("fsck", "read_mast"), ("fsck", "select_fsck_volume"):
            return L10n.string("timeline.stage.finding_volumes")
        case ("fsck", "run_fsck"):
            return L10n.string("timeline.stage.repairing_disk")
        case ("validate-install", "validate_install"):
            return L10n.string("timeline.stage.validating_app_bundle")
        default:
            return stage
                .split(separator: "_")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }

    static func stageDetail(for operation: String, stage: String?, fallback: String?) -> String? {
        guard let stage else {
            return fallback
        }
        if let detail = localizedStageDetail(for: operation, stage: stage) {
            return detail
        }
        return fallback
    }

    private static func stageLocalization(for operation: String, stage: String) -> OperationStageLocalization? {
        stageLocalizations[operation]?[stage] ?? rebootStageLocalizations[stage]
    }

    private static func localizedStageTitle(for operation: String, stage: String) -> String? {
        stageLocalization(for: operation, stage: stage).map { L10n.string($0.titleKey) }
    }

    private static func localizedStageDetail(for operation: String, stage: String) -> String? {
        stageLocalization(for: operation, stage: stage).map { L10n.string($0.detailKey) }
    }
}
