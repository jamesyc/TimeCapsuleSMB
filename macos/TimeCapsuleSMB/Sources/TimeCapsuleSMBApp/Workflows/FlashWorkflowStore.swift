import Foundation

enum FlashBuildPolicy: String, CaseIterable, Equatable {
    case disabled
    case readOnly
    case writesEnabled
}

enum FlashWorkflowState: String, CaseIterable, Equatable {
    case unavailable
    case disabledInThisBuild
    case eligibleForReadOnlyAnalysis
    case readingBanks
    case savingBackup
    case analyzingBanks
    case planAvailable
    case writeLocked
    case awaitingStrongConfirmation
    case writing
    case readbackValidating
    case writeValidated
    case manualPowerCycleRequired
    case restoreRebooting
    case failed

    var title: String {
        switch self {
        case .unavailable:
            return "Unavailable"
        case .disabledInThisBuild:
            return "Disabled in This Build"
        case .eligibleForReadOnlyAnalysis:
            return "Read-Only Analysis Available"
        case .readingBanks:
            return "Reading Firmware Banks"
        case .savingBackup:
            return "Saving Backup"
        case .analyzingBanks:
            return "Analyzing Firmware"
        case .planAvailable:
            return "Plan Available"
        case .writeLocked:
            return "Write Locked"
        case .awaitingStrongConfirmation:
            return "Awaiting Strong Confirmation"
        case .writing:
            return "Writing Firmware"
        case .readbackValidating:
            return "Validating Write"
        case .writeValidated:
            return "Write Validated"
        case .manualPowerCycleRequired:
            return "Manual Power Cycle Required"
        case .restoreRebooting:
            return "Rebooting After Restore"
        case .failed:
            return "Failed"
        }
    }
}

struct FlashEligibility: Equatable {
    let state: FlashWorkflowState
    let message: String
    let readOnlyAllowed: Bool
    let writeAllowed: Bool
}

enum FlashEligibilityPolicy {
    static func eligibility(for profile: DeviceProfile, buildPolicy: FlashBuildPolicy = .disabled) -> FlashEligibility {
        guard profile.traits.supportsFlashBootHook else {
            return FlashEligibility(
                state: .unavailable,
                message: "Persistent boot hook tools are only for NetBSD4 Time Capsules.",
                readOnlyAllowed: false,
                writeAllowed: false
            )
        }

        switch buildPolicy {
        case .disabled:
            return FlashEligibility(
                state: .disabledInThisBuild,
                message: "Firmware boot hook analysis is planned, but disabled in this build.",
                readOnlyAllowed: false,
                writeAllowed: false
            )
        case .readOnly:
            return FlashEligibility(
                state: .eligibleForReadOnlyAnalysis,
                message: "This device can use read-only firmware backup and inspection when the flash API is available.",
                readOnlyAllowed: true,
                writeAllowed: false
            )
        case .writesEnabled:
            return FlashEligibility(
                state: .writeLocked,
                message: "Write actions require backup review and strong confirmation before they can run.",
                readOnlyAllowed: true,
                writeAllowed: true
            )
        }
    }
}

@MainActor
final class FlashWorkflowStore: ObservableObject {
    @Published private(set) var state: FlashWorkflowState = .disabledInThisBuild
    @Published private(set) var eligibilityMessage = "Firmware boot hook analysis is disabled in this build."

    let buildPolicy: FlashBuildPolicy

    init(buildPolicy: FlashBuildPolicy = .disabled) {
        self.buildPolicy = buildPolicy
    }

    func refresh(profile: DeviceProfile) {
        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: buildPolicy)
        state = eligibility.state
        eligibilityMessage = eligibility.message
    }
}
