import XCTest
@testable import TimeCapsuleSMBApp

final class ActivityProgressTextAnimatorTests: XCTestCase {
    func testRunningMessageCyclesOneTwoAndThreeDots() {
        let message = "Run local and remote diagnostic checks."

        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: true, phase: 0), "Run local and remote diagnostic checks.")
        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: true, phase: 1), "Run local and remote diagnostic checks..")
        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: true, phase: 2), "Run local and remote diagnostic checks...")
        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: true, phase: 3), "Run local and remote diagnostic checks.")
    }

    func testRunningMessageNormalizesExistingDotsBeforeAnimating() {
        XCTAssertEqual(ActivityProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 0), "Resolve target.")
        XCTAssertEqual(ActivityProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 1), "Resolve target..")
        XCTAssertEqual(ActivityProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 2), "Resolve target...")
    }

    func testInactiveMessagesRemainStable() {
        let message = "deployment completed."

        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: false, phase: 0), message)
        XCTAssertEqual(ActivityProgressTextAnimator.message(message, isRunning: false, phase: 2), message)
    }

    func testEmptyMessagesDoNotAnimate() {
        XCTAssertNil(ActivityProgressTextAnimator.message(nil, isRunning: true, phase: 1))
        XCTAssertEqual(ActivityProgressTextAnimator.message("", isRunning: true, phase: 1), "")
        XCTAssertEqual(ActivityProgressTextAnimator.message("   ", isRunning: true, phase: 1), "   ")
    }

    func testAnimationIdentityExistsOnlyForActiveMessages() {
        let running = ActivitySnapshot(
            isRunning: true,
            scope: .app,
            operationTitle: "Checkup",
            latestMessage: "Run local and remote diagnostic checks.",
            timeline: []
        )
        let completed = ActivitySnapshot(
            isRunning: false,
            scope: .app,
            operationTitle: "Checkup",
            latestMessage: "Run local and remote diagnostic checks.",
            timeline: []
        )

        XCTAssertEqual(ActivityProgressTextAnimator.animationIdentity(for: running), "Run local and remote diagnostic checks.")
        XCTAssertNil(ActivityProgressTextAnimator.animationIdentity(for: completed))
    }

    func testFrameIntervalMatchesBottomBarAnimationCadence() {
        XCTAssertEqual(ActivityProgressTextAnimator.frameInterval, 0.3)
    }
}
