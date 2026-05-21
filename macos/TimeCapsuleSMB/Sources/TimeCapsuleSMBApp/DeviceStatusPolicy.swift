import Foundation

enum DeviceDisplayStatus: String, CaseIterable, Equatable, Identifiable {
    case unchecked
    case passwordNeeded
    case passwordInvalid
    case keychainUnavailable
    case checking
    case installing
    case maintaining
    case readyToInstall
    case healthy
    case warning
    case failed
    case activationNeeded
    case removed
    case offline
    case unsupported

    var id: String { rawValue }

    var title: String {
        switch self {
        case .unchecked:
            return "Unchecked"
        case .passwordNeeded:
            return "Password Needed"
        case .passwordInvalid:
            return "Password Invalid"
        case .keychainUnavailable:
            return "Keychain Unavailable"
        case .checking:
            return "Checking"
        case .installing:
            return "Installing"
        case .maintaining:
            return "Maintenance"
        case .readyToInstall:
            return "Ready to Install"
        case .healthy:
            return "Healthy"
        case .warning:
            return "Warning"
        case .failed:
            return "Failed"
        case .activationNeeded:
            return "Activation Needed"
        case .removed:
            return "Removed"
        case .offline:
            return "Offline"
        case .unsupported:
            return "Unsupported"
        }
    }

    var systemImage: String {
        switch self {
        case .unchecked:
            return "circle"
        case .passwordNeeded, .passwordInvalid, .keychainUnavailable:
            return "key"
        case .checking:
            return "stethoscope"
        case .installing:
            return "square.and.arrow.up"
        case .maintaining:
            return "wrench.and.screwdriver"
        case .readyToInstall:
            return "arrow.down.circle"
        case .healthy:
            return "checkmark.circle"
        case .warning, .activationNeeded:
            return "exclamationmark.triangle"
        case .failed, .offline, .unsupported:
            return "xmark.octagon"
        case .removed:
            return "trash"
        }
    }
}

enum DeviceStatusPolicy {
    static func status(
        for profile: DeviceProfile,
        passwordState: DevicePasswordState,
        activeOperation: ActiveOperation?
    ) -> DeviceDisplayStatus {
        if let activeOperation, activeOperation.profileID == profile.id {
            switch activeOperation.operation {
            case "doctor":
                return .checking
            case "deploy":
                return .installing
            case "activate", "uninstall", "fsck", "repair-xattrs", "flash":
                return .maintaining
            default:
                break
            }
        }

        switch passwordState {
        case .missing, .unknown:
            return .passwordNeeded
        case .invalid:
            return .passwordInvalid
        case .keychainUnavailable:
            return .keychainUnavailable
        case .available:
            break
        }

        if !profile.traits.isSupported {
            return .unsupported
        }

        guard let checkup = profile.lastCheckup else {
            return .unchecked
        }

        if checkup.failCount > 0 || checkup.state == .failed || checkup.state == .runFailed {
            return .failed
        }
        if profile.traits.needsActivationAfterReboot, profile.lastDeploy != nil, checkup.warnCount > 0 {
            return .activationNeeded
        }
        if checkup.warnCount > 0 || checkup.state == .warning {
            return .warning
        }
        if profile.lastDeploy == nil {
            return .readyToInstall
        }
        return .healthy
    }

}

enum DashboardPrimaryActionPolicy {
    static func primaryAction(
        for profile: DeviceProfile,
        passwordState: DevicePasswordState,
        activeOperation: ActiveOperation?
    ) -> DashboardPrimaryAction {
        let status = DeviceStatusPolicy.status(
            for: profile,
            passwordState: passwordState,
            activeOperation: activeOperation
        )
        switch status {
        case .passwordNeeded, .passwordInvalid, .keychainUnavailable:
            return .replacePassword
        case .unchecked:
            return .runCheckup
        case .readyToInstall:
            return .installSMB
        case .warning, .failed, .activationNeeded:
            return .viewCheckup
        case .healthy:
            return .openSMB
        case .checking:
            return .viewCheckup
        case .installing:
            return .installSMB
        case .maintaining:
            return .viewCheckup
        case .removed, .offline, .unsupported:
            return .runCheckup
        }
    }
}
