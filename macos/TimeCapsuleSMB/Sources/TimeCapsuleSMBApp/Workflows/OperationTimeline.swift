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
}

private struct OperationStageLocalization {
    let titleKey: String
    let detailKey: String
}

enum OperationTimelineBuilder {
    private static let activateStageLocalizations: [String: OperationStageLocalization] = [
        "probe_runtime": .init(titleKey: "timeline.activate.title.probe_runtime", detailKey: "timeline.activate.detail.probe_runtime"),
        "post_activation_settle": .init(titleKey: "timeline.activate.title.post_activation_settle", detailKey: "timeline.activate.detail.post_activation_settle")
    ]

    private static let deployStageLocalizations: [String: OperationStageLocalization] = [
        "load_config": .init(titleKey: "timeline.deploy.title.load_config", detailKey: "timeline.deploy.detail.load_config"),
        "resolve_managed_target": .init(titleKey: "timeline.deploy.title.resolve_managed_target", detailKey: "timeline.deploy.detail.resolve_managed_target"),
        "validate_artifacts": .init(titleKey: "timeline.deploy.title.validate_artifacts", detailKey: "timeline.deploy.detail.validate_artifacts"),
        "check_compatibility": .init(titleKey: "timeline.deploy.title.check_compatibility", detailKey: "timeline.deploy.detail.check_compatibility"),
        "read_mast": .init(titleKey: "timeline.deploy.title.read_mast", detailKey: "timeline.deploy.detail.read_mast"),
        "select_payload_home": .init(titleKey: "timeline.deploy.title.select_payload_home", detailKey: "timeline.deploy.detail.select_payload_home"),
        "build_deployment_plan": .init(titleKey: "timeline.deploy.title.build_deployment_plan", detailKey: "timeline.deploy.detail.build_deployment_plan"),
        "pre_upload_actions": .init(titleKey: "timeline.deploy.title.pre_upload_actions", detailKey: "timeline.deploy.detail.pre_upload_actions"),
        "prepare_deployment_files": .init(titleKey: "timeline.deploy.title.prepare_deployment_files", detailKey: "timeline.deploy.detail.prepare_deployment_files"),
        "upload_payload": .init(titleKey: "timeline.deploy.title.upload_payload", detailKey: "timeline.deploy.detail.upload_payload"),
        "upload_smbd": .init(titleKey: "timeline.deploy.title.upload_smbd", detailKey: "timeline.deploy.detail.upload_smbd"),
        "upload_mdns_advertiser": .init(titleKey: "timeline.deploy.title.upload_mdns_advertiser", detailKey: "timeline.deploy.detail.upload_mdns_advertiser"),
        "upload_nbns_advertiser": .init(titleKey: "timeline.deploy.title.upload_nbns_advertiser", detailKey: "timeline.deploy.detail.upload_nbns_advertiser"),
        "upload_boot_files": .init(titleKey: "timeline.deploy.title.upload_boot_files", detailKey: "timeline.deploy.detail.upload_boot_files"),
        "upload_runtime_config": .init(titleKey: "timeline.deploy.title.upload_runtime_config", detailKey: "timeline.deploy.detail.upload_runtime_config"),
        "upload_samba_accounts": .init(titleKey: "timeline.deploy.title.upload_samba_accounts", detailKey: "timeline.deploy.detail.upload_samba_accounts"),
        "post_upload_actions": .init(titleKey: "timeline.deploy.title.post_upload_actions", detailKey: "timeline.deploy.detail.post_upload_actions"),
        "verify_payload_upload": .init(titleKey: "timeline.deploy.title.verify_payload_upload", detailKey: "timeline.deploy.detail.verify_payload_upload"),
        "flush_payload_upload": .init(titleKey: "timeline.deploy.title.flush_payload_upload", detailKey: "timeline.deploy.detail.flush_payload_upload"),
        "verify_payload_upload_after_sync": .init(titleKey: "timeline.deploy.title.verify_payload_upload_after_sync", detailKey: "timeline.deploy.detail.verify_payload_upload_after_sync"),
        "reboot": .init(titleKey: "timeline.deploy.title.reboot", detailKey: "timeline.deploy.detail.reboot"),
        "wait_for_reboot_down": .init(titleKey: "timeline.deploy.title.wait_for_reboot_down", detailKey: "timeline.deploy.detail.wait_for_reboot_down"),
        "wait_for_reboot_up": .init(titleKey: "timeline.deploy.title.wait_for_reboot_up", detailKey: "timeline.deploy.detail.wait_for_reboot_up"),
        "probe_runtime": .init(titleKey: "timeline.deploy.title.probe_runtime", detailKey: "timeline.deploy.detail.probe_runtime"),
        "activate_runtime": .init(titleKey: "timeline.deploy.title.activate_runtime", detailKey: "timeline.deploy.detail.activate_runtime"),
        "post_reboot_boot_settle": .init(titleKey: "timeline.deploy.title.post_reboot_boot_settle", detailKey: "timeline.deploy.detail.post_reboot_boot_settle"),
        "post_activation_settle": .init(titleKey: "timeline.deploy.title.post_activation_settle", detailKey: "timeline.deploy.detail.post_activation_settle"),
        "post_reboot_activation": .init(titleKey: "timeline.deploy.title.post_reboot_activation", detailKey: "timeline.deploy.detail.post_reboot_activation"),
        "verify_runtime_activation": .init(titleKey: "timeline.deploy.title.verify_runtime_activation", detailKey: "timeline.deploy.detail.verify_runtime_activation"),
        "verify_runtime_reboot": .init(titleKey: "timeline.deploy.title.verify_runtime_reboot", detailKey: "timeline.deploy.detail.verify_runtime_reboot")
    ]

    private static let stageLocalizations: [String: [String: OperationStageLocalization]] = [
        "activate": activateStageLocalizations,
        "deploy": deployStageLocalizations
    ]

    static func timeline(from events: [BackendEvent]) -> [OperationTimelineItem] {
        events.enumerated().compactMap { index, event in
            switch event.type {
            case "stage":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):\(event.stage ?? "stage")",
                    operation: event.operation,
                    title: stageTitle(for: event.operation, stage: event.stage),
                    detail: stageDetail(for: event.operation, stage: event.stage, fallback: event.description),
                    state: stageState(forEventAt: index, in: events),
                    risk: event.risk,
                    cancellable: event.cancellable
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
        case "configure":
            return L10n.string("timeline.operation.configure")
        case "deploy":
            return L10n.string("timeline.operation.deploy")
        case "doctor":
            return L10n.string("timeline.operation.doctor")
        case "activate":
            return L10n.string("timeline.operation.activate")
        case "fsck":
            return L10n.string("timeline.operation.fsck")
        case "repair-xattrs":
            return L10n.string("timeline.operation.repair_xattrs")
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
        case ("configure", "ssh_probe"), ("configure", "ssh_probe_after_acp"):
            return L10n.string("timeline.stage.checking_ssh")
        case ("configure", "confirm_enable_ssh"):
            return L10n.string("timeline.stage.confirming_ssh_enable")
        case ("configure", "acp_identity_probe"):
            return L10n.string("timeline.stage.checking_airport_identity")
        case ("configure", "acp_enable_ssh"):
            return L10n.string("timeline.stage.enabling_ssh")
        case ("configure", "wait_for_ssh_after_acp"):
            return L10n.string("timeline.stage.waiting_for_device")
        case ("configure", "write_env"):
            return L10n.string("timeline.stage.saving_device")
        case ("doctor", "run_checks"):
            return L10n.string("timeline.stage.running_checkup")
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
        case ("repair-xattrs", "scan_findings"):
            return L10n.string("timeline.stage.scanning_metadata")
        case ("repair-xattrs", "repair_findings"):
            return L10n.string("timeline.stage.repairing_metadata")
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

    private static func localizedStageTitle(for operation: String, stage: String) -> String? {
        stageLocalizations[operation]?[stage].map { L10n.string($0.titleKey) }
    }

    private static func localizedStageDetail(for operation: String, stage: String) -> String? {
        stageLocalizations[operation]?[stage].map { L10n.string($0.detailKey) }
    }
}
