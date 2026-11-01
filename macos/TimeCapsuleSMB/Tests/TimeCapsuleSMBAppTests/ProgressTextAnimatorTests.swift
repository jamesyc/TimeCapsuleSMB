import XCTest
@testable import TimeCapsuleSMBApp

final class ProgressTextAnimatorTests: XCTestCase {
    private var originalLanguage: AppLanguage = .system

    // Each test starts in English, whatever the Mac's own language, and puts
    // back the language it found.
    override func setUp() {
        super.setUp()
        originalLanguage = L10n.currentLanguage
        L10n.apply(language: .english)
    }

    override func tearDown() {
        L10n.apply(language: originalLanguage)
        super.tearDown()
    }

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

    func testRunningChineseLineEndsInOneEllipsisWithoutCycling() {
        L10n.apply(language: .simplifiedChinese)
        let cases = [
            ("维护正在运行。", "维护正在运行…"),
            ("正在查找设备……", "正在查找设备…"),
            ("正在搜索…", "正在搜索…"),
            ("正在启用 SSH", "正在启用 SSH…"),
        ]
        for (message, running) in cases {
            for phase in 0..<ProgressTextAnimator.frameCount {
                XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: true, phase: phase), running, "\(message) \(phase)")
            }
            XCTAssertEqual(ProgressTextAnimator.message(message, isRunning: false, phase: 1), message)
        }
    }

    func testEnglishTextKeepsItsDotsUnlessTheChineseUIShowsAHanCharacter() {
        // The helper's untranslated English, in the Chinese UI.
        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(ProgressTextAnimator.message("Resolve target...", isRunning: true, phase: 1), "Resolve target..")
        // An English line that names a device the user called 客厅 reads as
        // Chinese there, and only there.
        XCTAssertEqual(ProgressTextAnimator.message("Restarting 客厅...", isRunning: true, phase: 1), "Restarting 客厅…")
        L10n.apply(language: .english)
        XCTAssertEqual(ProgressTextAnimator.message("Restarting 客厅...", isRunning: true, phase: 1), "Restarting 客厅..")
    }

    func testInactiveMessagesRemainStable() {
        let message = "Deployment completed."

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
        XCTAssertEqual(ProgressTextAnimator.frameIntervalNanoseconds, 300_000_000)
    }
}
