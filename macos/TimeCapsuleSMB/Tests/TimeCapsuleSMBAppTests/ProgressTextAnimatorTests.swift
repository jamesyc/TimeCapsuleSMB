import XCTest
@testable import TimeCapsuleSMBApp

final class ProgressTextAnimatorTests: XCTestCase {
    func testRunningMessageCyclesOneTwoAndThreeDots() {
        let message = "Run local and remote diagnostic checks."

        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: true, phase: 0), "Run local and remote diagnostic checks.")
        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: true, phase: 1), "Run local and remote diagnostic checks..")
        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: true, phase: 2), "Run local and remote diagnostic checks...")
        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: true, phase: 3), "Run local and remote diagnostic checks.")
    }

    func testRunningMessageNormalizesExistingDotsBeforeAnimating() {
        XCTAssertEqual(ProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 0), "Resolve target.")
        XCTAssertEqual(ProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 1), "Resolve target..")
        XCTAssertEqual(ProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 2), "Resolve target...")
    }

    func testInactiveMessagesRemainStable() {
        let message = "deployment completed."

        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: false, phase: 0), message)
        XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: false, phase: 2), message)
    }

    func testEmptyMessagesDoNotAnimate() {
        XCTAssertNil(ProgressTextAnimator.message(nil, isRunning: true, phase: 1))
        XCTAssertEqual(ProgressTextAnimator.message("", isRunning: true, phase: 1), "")
        XCTAssertEqual(ProgressTextAnimator.message("   ", isRunning: true, phase: 1), "   ")
    }

    func testFrameIntervalMatchesProgressTextAnimationCadence() {
        XCTAssertEqual(ProgressTextAnimator.frameInterval, 0.3)
    }
}
