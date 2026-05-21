import Foundation

struct PresentationRow: Equatable, Identifiable {
    var id: String {
        "\(label):\(value)"
    }

    let label: String
    let value: String
}

struct DeployPlanPresentation: Equatable {
    let title: String
    let summaryRows: [PresentationRow]
    let advancedRows: [PresentationRow]
    let warnings: [String]

    init(plan: DeployPlanPayload, profile: DeviceProfile, hostWarning: HostCompatibilityWarning? = nil) {
        self.title = plan.netbsd4 ? "Install SMB and Start Runtime" : "Install SMB"
        self.summaryRows = [
            PresentationRow(label: "Target", value: profile.title),
            PresentationRow(label: "Host", value: plan.host),
            PresentationRow(label: "Payload", value: plan.payloadFamily ?? profile.payloadFamily ?? "Unknown"),
            PresentationRow(label: "Disk Location", value: plan.volumeRoot ?? plan.payloadDir),
            PresentationRow(label: "Reboot", value: plan.requiresReboot ? "Required" : "Not required"),
            PresentationRow(label: "Expected Changes", value: "\(plan.uploads.count) file upload(s), \(plan.postUploadActions.count) install action(s)")
        ]
        self.advancedRows = [
            PresentationRow(label: "Payload Directory", value: plan.payloadDir),
            PresentationRow(label: "Pre-upload Actions", value: "\(plan.preUploadActions.count)"),
            PresentationRow(label: "Post-upload Actions", value: "\(plan.postUploadActions.count)"),
            PresentationRow(label: "Activation Actions", value: "\(plan.activationActions.count)"),
            PresentationRow(label: "Post-install Checks", value: plan.postDeployChecks.map(\.description).joined(separator: ", "))
        ]
        var warnings: [String] = []
        if plan.netbsd4 {
            warnings.append("This NetBSD4 device may need Start SMB after future reboots unless the boot hook is patched.")
        }
        if let hostWarning {
            warnings.append(hostWarning.message)
        }
        self.warnings = warnings
    }
}

struct CheckupPresentation: Equatable {
    let headline: String
    let summaryRows: [PresentationRow]
    let groups: [DoctorCheckGroup]

    init(summary: DoctorSummary, state: DoctorWorkflowState) {
        switch state {
        case .passed:
            self.headline = "SMB looks healthy."
        case .warning:
            self.headline = "Checkup found warnings."
        case .failed:
            self.headline = "Checkup found failures."
        case .runFailed:
            self.headline = "Checkup could not finish."
        case .idle:
            self.headline = "Run a checkup to verify this Time Capsule."
        case .running:
            self.headline = "Running checkup..."
        }
        self.summaryRows = [
            PresentationRow(label: "Pass", value: "\(summary.passCount)"),
            PresentationRow(label: "Warning", value: "\(summary.warnCount)"),
            PresentationRow(label: "Fail", value: "\(summary.failCount)"),
            PresentationRow(label: "Info", value: "\(summary.infoCount)")
        ]
        self.groups = summary.groups
    }
}

struct MaintenanceWorkflowPresentation: Equatable {
    let title: String
    let subtitle: String
    let primaryAction: String
    let risk: String

    static func presentation(for workflow: MaintenanceWorkflow) -> MaintenanceWorkflowPresentation {
        switch workflow {
        case .activate:
            return MaintenanceWorkflowPresentation(
                title: "NetBSD4 Activation",
                subtitle: "Start the deployed SMB runtime on a NetBSD4 Time Capsule.",
                primaryAction: "Start SMB",
                risk: "Remote write"
            )
        case .uninstall:
            return MaintenanceWorkflowPresentation(
                title: "Uninstall",
                subtitle: "Remove managed SMB files from the selected Time Capsule.",
                primaryAction: "Uninstall",
                risk: "Destructive"
            )
        case .fsck:
            return MaintenanceWorkflowPresentation(
                title: "Disk Repair",
                subtitle: "Unmount a selected HFS volume and run fsck_hfs on the device.",
                primaryAction: "Run Disk Repair",
                risk: "Destructive"
            )
        case .repairXattrs:
            return MaintenanceWorkflowPresentation(
                title: "File Metadata Repair",
                subtitle: "Scan and repair macOS metadata on a mounted SMB share.",
                primaryAction: "Repair Metadata",
                risk: "Local destructive"
            )
        }
    }
}
