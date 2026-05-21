import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class FlashWorkflowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(FlashWorkflowState.allCases, [
            .unavailable,
            .disabledInThisBuild,
            .eligibleForReadOnlyAnalysis,
            .readingBanks,
            .savingBackup,
            .analyzingBanks,
            .planAvailable,
            .writeLocked,
            .awaitingStrongConfirmation,
            .writing,
            .readbackValidating,
            .writeValidated,
            .manualPowerCycleRequired,
            .restoreRebooting,
            .failed
        ])
    }

    func testReleaseDefaultDisablesFlashEvenForNetBSD4() throws {
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        let store = FlashWorkflowStore()

        store.refresh(profile: profile)

        XCTAssertEqual(store.state, .disabledInThisBuild)
        XCTAssertTrue(store.eligibilityMessage.contains("disabled"))
    }

    func testReadOnlyPolicyAllowsAnalysisButNotWrites() throws {
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .readOnly)

        XCTAssertEqual(eligibility.state, .eligibleForReadOnlyAnalysis)
        XCTAssertTrue(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testNonNetBSD4DeviceIsUnavailable() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        let eligibility = FlashEligibilityPolicy.eligibility(for: profile, buildPolicy: .writesEnabled)

        XCTAssertEqual(eligibility.state, .unavailable)
        XCTAssertFalse(eligibility.readOnlyAllowed)
        XCTAssertFalse(eligibility.writeAllowed)
    }

    func testFlashPresentationExposesAllActionsButEnablesOnlyReadOnlyEntryPoint() {
        let readOnlyStates: Set<FlashWorkflowState> = [
            .eligibleForReadOnlyAnalysis,
            .planAvailable,
            .writeLocked,
            .awaitingStrongConfirmation
        ]

        for state in FlashWorkflowState.allCases {
            let presentation = FlashPresentation(state: state, message: "message")

            XCTAssertEqual(presentation.actions, [.backupAndInspect, .patchBootHook, .restoreFirmware])
            XCTAssertEqual(presentation.message, "message")
            XCTAssertEqual(presentation.stateTitle, state.title)
            XCTAssertEqual(presentation.isEnabled(.backupAndInspect), readOnlyStates.contains(state), "Unexpected backup action state for \(state).")
            XCTAssertFalse(presentation.isEnabled(.patchBootHook), "Patch action must remain disabled for \(state).")
            XCTAssertFalse(presentation.isEnabled(.restoreFirmware), "Restore action must remain disabled for \(state).")
        }
    }

    private func makeProfile(payloadFamily: String) throws -> DeviceProfile {
        DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }
}
