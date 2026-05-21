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
        self.title = plan.netbsd4
            ? L10n.string("deploy.presentation.title.netbsd4")
            : L10n.string("deploy.presentation.title.standard")
        self.summaryRows = [
            PresentationRow(label: L10n.string("deploy.presentation.row.target"), value: profile.title),
            PresentationRow(label: L10n.string("deploy.presentation.row.host"), value: plan.host),
            PresentationRow(label: L10n.string("deploy.presentation.row.payload"), value: plan.payloadFamily ?? profile.payloadFamily ?? L10n.string("value.unknown")),
            PresentationRow(label: L10n.string("deploy.presentation.row.disk_location"), value: plan.volumeRoot ?? plan.payloadDir),
            PresentationRow(label: L10n.string("deploy.presentation.row.reboot"), value: plan.requiresReboot ? L10n.string("value.required") : L10n.string("value.not_required")),
            PresentationRow(
                label: L10n.string("deploy.presentation.row.expected_changes"),
                value: L10n.format("deploy.presentation.expected_changes", plan.uploads.count, plan.postUploadActions.count)
            )
        ]
        self.advancedRows = [
            PresentationRow(label: L10n.string("deploy.presentation.row.payload_directory"), value: plan.payloadDir),
            PresentationRow(label: L10n.string("deploy.presentation.row.pre_upload_actions"), value: "\(plan.preUploadActions.count)"),
            PresentationRow(label: L10n.string("deploy.presentation.row.post_upload_actions"), value: "\(plan.postUploadActions.count)"),
            PresentationRow(label: L10n.string("deploy.presentation.row.activation_actions"), value: "\(plan.activationActions.count)"),
            PresentationRow(label: L10n.string("deploy.presentation.row.post_install_checks"), value: plan.postDeployChecks.map(\.description).joined(separator: ", "))
        ]
        var warnings: [String] = []
        if plan.netbsd4 {
            warnings.append(L10n.string("deploy.presentation.warning.netbsd4_activation"))
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
            self.headline = L10n.string("checkup.presentation.headline.passed")
        case .warning:
            self.headline = L10n.string("checkup.presentation.headline.warning")
        case .failed:
            self.headline = L10n.string("checkup.presentation.headline.failed")
        case .runFailed:
            self.headline = L10n.string("checkup.presentation.headline.run_failed")
        case .idle:
            self.headline = L10n.string("checkup.presentation.headline.idle")
        case .running:
            self.headline = L10n.string("checkup.presentation.headline.running")
        }
        self.summaryRows = [
            PresentationRow(label: L10n.string("checkup.presentation.row.pass"), value: "\(summary.passCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.warning"), value: "\(summary.warnCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.fail"), value: "\(summary.failCount)"),
            PresentationRow(label: L10n.string("checkup.presentation.row.info"), value: "\(summary.infoCount)")
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
                title: L10n.string("maintenance.presentation.activate.title"),
                subtitle: L10n.string("maintenance.presentation.activate.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.activate.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.remote_write")
            )
        case .uninstall:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.uninstall.title"),
                subtitle: L10n.string("maintenance.presentation.uninstall.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.uninstall.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.destructive")
            )
        case .fsck:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.fsck.title"),
                subtitle: L10n.string("maintenance.presentation.fsck.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.fsck.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.destructive")
            )
        case .repairXattrs:
            return MaintenanceWorkflowPresentation(
                title: L10n.string("maintenance.presentation.repair_xattrs.title"),
                subtitle: L10n.string("maintenance.presentation.repair_xattrs.subtitle"),
                primaryAction: L10n.string("maintenance.presentation.repair_xattrs.primary_action"),
                risk: L10n.string("maintenance.presentation.risk.local_destructive")
            )
        }
    }
}
