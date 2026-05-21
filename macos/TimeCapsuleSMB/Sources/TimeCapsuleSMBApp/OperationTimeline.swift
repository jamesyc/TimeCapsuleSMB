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
                    state: .running,
                    risk: event.risk,
                    cancellable: event.cancellable
                )
            case "result":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):result",
                    operation: event.operation,
                    title: event.ok == true ? "Done" : "Failed",
                    detail: event.payloadSummaryText ?? event.summary,
                    state: event.ok == true ? .succeeded : .failed,
                    risk: nil,
                    cancellable: nil
                )
            case "error":
                return OperationTimelineItem(
                    id: "\(index):\(event.operation):error",
                    operation: event.operation,
                    title: event.code == "confirmation_required" ? "Needs Confirmation" : "Needs Attention",
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

    static func operationTitle(_ operation: String) -> String {
        switch operation {
        case "discover":
            return "Discovery"
        case "configure":
            return "Add Time Capsule"
        case "deploy":
            return "Install / Update"
        case "doctor":
            return "Checkup"
        case "activate":
            return "Start SMB"
        case "fsck":
            return "Disk Repair"
        case "repair-xattrs":
            return "File Metadata Repair"
        case "uninstall":
            return "Uninstall"
        case "capabilities", "validate-install", "paths":
            return "App Readiness"
        case "flash":
            return "Persistent NetBSD4 Boot Hook"
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
            return "Finding Time Capsules"
        case ("configure", "ssh_probe"), ("configure", "ssh_probe_after_acp"):
            return "Checking SSH"
        case ("configure", "acp_enable_ssh"):
            return "Enabling SSH"
        case ("configure", "wait_for_ssh_after_acp"):
            return "Waiting for Device"
        case ("configure", "write_env"):
            return "Saving Device"
        case ("deploy", "build_deployment_plan"):
            return "Planning Install"
        case ("deploy", "validate_artifacts"):
            return "Checking Bundled Files"
        case ("deploy", "read_mast"), ("deploy", "select_payload_home"):
            return "Finding Disk"
        case ("deploy", "upload_payload"):
            return "Uploading"
        case ("deploy", "flush_payload_upload"):
            return "Syncing to Disk"
        case ("deploy", "reboot"), ("deploy", "wait_for_reboot_down"), ("deploy", "wait_for_reboot_up"):
            return "Rebooting"
        case ("deploy", "netbsd4_activation"):
            return "Starting SMB"
        case ("deploy", "verify_runtime_activation"), ("deploy", "verify_runtime_reboot"):
            return "Verifying SMB"
        case ("doctor", "run_checks"):
            return "Running Checkup"
        case ("activate", "build_activation_plan"):
            return "Planning Start SMB"
        case ("activate", "run_activation"):
            return "Starting SMB"
        case ("uninstall", "build_uninstall_plan"):
            return "Planning Uninstall"
        case ("uninstall", "uninstall_payload"):
            return "Removing Managed Files"
        case ("fsck", "read_mast"), ("fsck", "select_fsck_volume"):
            return "Finding Volumes"
        case ("fsck", "run_fsck"):
            return "Repairing Disk"
        case ("repair-xattrs", "scan_findings"):
            return "Scanning Metadata"
        case ("repair-xattrs", "repair_findings"):
            return "Repairing Metadata"
        case ("validate-install", "validate_install"):
            return "Validating App Bundle"
        default:
            return stage
                .split(separator: "_")
                .map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
        }
    }
}
