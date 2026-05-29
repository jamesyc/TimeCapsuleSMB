import Foundation

enum DashboardActionPolicy {
    static func secondaryActions(for summary: DeviceDashboardSummary) -> [DashboardSecondaryAction] {
        var actions: [DashboardSecondaryAction] = []
        if let contextualAction = contextualSecondaryAction(for: summary.primaryAction) {
            actions.append(contextualAction)
        }
        if summary.profile.runtimeState?.state.isInstalled == true && summary.primaryAction != .openSMB {
            actions.append(.openFinder)
        }
        actions.append(.settings)
        return removingDuplicates(actions.filter { isAvailable($0, for: summary) })
    }

    static func requiresPasswordReplacement(_ passwordState: DevicePasswordState) -> Bool {
        switch passwordState {
        case .unknown, .missing, .invalid, .keychainUnavailable:
            return true
        case .available:
            return false
        }
    }

    static func isEnabled(_ action: DashboardPrimaryAction, for summary: DeviceDashboardSummary) -> Bool {
        !blocksMutatingActions(summary.displayStatus) || !action.isMutatingOverviewAction
    }

    static func isEnabled(_ action: DashboardSecondaryAction, for summary: DeviceDashboardSummary) -> Bool {
        !blocksMutatingActions(summary.displayStatus) || !action.isMutatingOverviewAction
    }

    static func checkupAction(for summary: DeviceDashboardSummary) -> DashboardSecondaryAction {
        summary.displayStatus == .checking ? .viewCheckup : .runCheckup
    }

    private static func contextualSecondaryAction(for primaryAction: DashboardPrimaryAction) -> DashboardSecondaryAction? {
        switch primaryAction {
        case .replacePassword:
            return .runCheckup
        case .runCheckup:
            return .installUpdate
        case .installSMB, .viewCheckup, .openSMB:
            return .runCheckup
        }
    }

    private static func isAvailable(_ action: DashboardSecondaryAction, for summary: DeviceDashboardSummary) -> Bool {
        switch action {
        case .runCheckup:
            return summary.displayStatus != .checking
        case .refreshStatus,
             .installUpdate,
             .openFinder,
             .replacePassword,
             .viewCheckup,
             .startSMB,
             .settings:
            return true
        }
    }

    private static func blocksMutatingActions(_ status: DeviceDisplayStatus) -> Bool {
        switch status {
        case .checking, .installing, .maintaining:
            return true
        case .unchecked,
             .passwordNeeded,
             .passwordInvalid,
             .keychainUnavailable,
             .readyToInstall,
             .healthy,
             .warning,
             .failed,
             .activationNeeded,
             .removed,
             .offline,
             .unsupported:
            return false
        }
    }

    private static func removingDuplicates(_ actions: [DashboardSecondaryAction]) -> [DashboardSecondaryAction] {
        var seen: Set<DashboardSecondaryAction> = []
        return actions.filter { seen.insert($0).inserted }
    }
}

extension DashboardPrimaryAction {
    var isMutatingOverviewAction: Bool {
        switch self {
        case .runCheckup, .installSMB:
            return true
        case .replacePassword, .viewCheckup, .openSMB:
            return false
        }
    }
}

extension DashboardSecondaryAction {
    var isMutatingOverviewAction: Bool {
        switch self {
        case .refreshStatus, .runCheckup, .installUpdate:
            return true
        case .openFinder, .replacePassword, .viewCheckup, .startSMB, .settings:
            return false
        }
    }
}
