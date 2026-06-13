import Foundation

enum MaintenanceWorkflow: String, CaseIterable, Equatable, Identifiable {
    case sshAccess
    case activate
    case uninstall
    case fsck
    case repairXattrs

    var id: String { rawValue }

    var title: String {
        switch self {
        case .sshAccess:
            return L10n.string("maintenance.workflow.ssh_access")
        case .activate:
            return L10n.string("maintenance.workflow.activate")
        case .uninstall:
            return L10n.string("maintenance.workflow.uninstall")
        case .fsck:
            return L10n.string("maintenance.workflow.fsck")
        case .repairXattrs:
            return L10n.string("maintenance.workflow.repair_xattrs")
        }
    }

    var deviceWorkflowLane: DeviceWorkflowLane {
        switch self {
        case .sshAccess:
            return .sshAccess
        case .activate:
            return .activate
        case .uninstall:
            return .uninstall
        case .fsck:
            return .fsck
        case .repairXattrs:
            return .repairXattrs
        }
    }
}

enum MaintenanceOperationState: String, CaseIterable, Equatable {
    case idle
    case loading
    case listReady
    case planning
    case planReady
    case planStale
    case scanning
    case scanReady
    case scanStale
    case awaitingConfirmation
    case running
    case repairing
    case succeeded
    case repaired
    case failed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("workflow.state.idle")
        case .loading:
            return L10n.string("workflow.state.loading")
        case .listReady:
            return L10n.string("workflow.state.list_ready")
        case .planning:
            return L10n.string("workflow.state.planning")
        case .planReady:
            return L10n.string("workflow.state.plan_ready")
        case .planStale:
            return L10n.string("workflow.state.plan_stale")
        case .scanning:
            return L10n.string("workflow.state.scanning")
        case .scanReady:
            return L10n.string("workflow.state.scan_ready")
        case .scanStale:
            return L10n.string("workflow.state.scan_stale")
        case .awaitingConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .running:
            return L10n.string("workflow.state.running")
        case .repairing:
            return L10n.string("workflow.state.repairing")
        case .succeeded:
            return L10n.string("workflow.state.succeeded")
        case .repaired:
            return L10n.string("workflow.state.repaired")
        case .failed:
            return L10n.string("workflow.state.failed")
        }
    }
}

struct MaintenanceOptions: Equatable {
    let noReboot: Bool
    let noWait: Bool
    let mountWait: Int
}

struct FsckTargetViewModel: Identifiable, Equatable {
    let id: String
    let device: String
    let mountpoint: String
    let name: String?
    let builtin: Bool?

    init(payload: FsckTargetPayload) {
        self.id = "\(payload.device)|\(payload.mountpoint)"
        self.device = payload.device
        self.mountpoint = payload.mountpoint
        self.name = payload.name
        self.builtin = payload.builtin
    }

    var volumeParam: String {
        device
    }
}
