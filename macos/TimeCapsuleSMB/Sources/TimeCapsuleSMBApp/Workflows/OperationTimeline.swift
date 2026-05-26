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

enum OperationTimelineBuilder {
    static func timeline(from events: [BackendEvent]) -> [OperationTimelineItem] {
        events.enumerated().compactMap { index, event in
            switch event.type {
            case "stage":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):\(event.stage ?? "stage")",
                    operation: event.operation,
                    title: title(for: event.operation, stage: event.stage),
                    detail: event.description,
                    state: stageState(forEventAt: index, in: events),
                    risk: event.risk,
                    cancellable: event.cancellable
                )
            case "result":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):result",
                    operation: event.operation,
                    title: event.ok == true ? L10n.string("timeline.result.done") : L10n.string("timeline.result.failed"),
                    detail: event.payloadSummaryText ?? event.summary,
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
        if laterEvents.contains(where: { $0.type == "stage" || ($0.type == "result" && $0.ok == true) }) {
            return .succeeded
        }
        return .running
    }

    static func operationTitle(_ operation: String) -> String {
        switch operation {
        case "discover":
            return L10n.string("timeline.operation.discovery")
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
        case "capabilities", "validate-install", "paths":
            return L10n.string("timeline.operation.readiness")
        case "set-telemetry", "telemetry-identity":
            return L10n.string("timeline.operation.telemetry")
        case "version-check":
            return L10n.string("timeline.operation.version_check")
        case "flash":
            return L10n.string("timeline.operation.flash")
        default:
            return operation
        }
    }

    private static func title(for operation: String, stage: String?) -> String {
        guard let stage else {
            return operationTitle(operation)
        }
        switch (operation, stage) {
        case ("discover", "bonjour_discovery"):
            return L10n.string("timeline.stage.finding_time_capsules")
        case ("configure", "ssh_probe"), ("configure", "ssh_probe_after_acp"):
            return L10n.string("timeline.stage.checking_ssh")
        case ("configure", "confirm_enable_ssh"):
            return L10n.string("timeline.stage.confirming_ssh_enable")
        case ("configure", "acp_enable_ssh"):
            return L10n.string("timeline.stage.enabling_ssh")
        case ("configure", "wait_for_ssh_after_acp"):
            return L10n.string("timeline.stage.waiting_for_device")
        case ("configure", "write_env"):
            return L10n.string("timeline.stage.saving_device")
        case ("deploy", "build_deployment_plan"):
            return L10n.string("timeline.stage.planning_install")
        case ("deploy", "validate_artifacts"):
            return L10n.string("timeline.stage.checking_bundled_files")
        case ("deploy", "read_mast"), ("deploy", "select_payload_home"):
            return L10n.string("timeline.stage.finding_disk")
        case ("deploy", "pre_upload_actions"):
            return L10n.string("timeline.stage.deleting_old_deployed_files")
        case ("deploy", "upload_payload"):
            return L10n.string("timeline.stage.uploading")
        case ("deploy", "flush_payload_upload"):
            return L10n.string("timeline.stage.syncing_to_disk")
        case ("deploy", "reboot"), ("deploy", "wait_for_reboot_down"), ("deploy", "wait_for_reboot_up"):
            return L10n.string("timeline.stage.rebooting")
        case ("deploy", "probe_runtime"):
            return L10n.string("timeline.stage.checking_runtime")
        case ("deploy", "activate_runtime"), ("deploy", "post_reboot_activation"), ("deploy", "netbsd4_activation"):
            return L10n.string("timeline.stage.starting_smb")
        case ("deploy", "verify_runtime_activation"), ("deploy", "verify_runtime_reboot"):
            return L10n.string("timeline.stage.verifying_smb")
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
}
