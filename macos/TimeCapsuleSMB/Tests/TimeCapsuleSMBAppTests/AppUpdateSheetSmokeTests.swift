import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppUpdateSheetSmokeTests: XCTestCase {
    func testRendersOptionalUpdateWithNotes() throws {
        let view = AppUpdateSheet(
            prompt: prompt(required: false),
            isChecking: false,
            onDownload: {},
            onRemindLater: {},
            onSkip: {}
        )

        try assertRendersNonBlank(view, size: CGSize(width: 560, height: 520))
    }

    func testRendersRequiredUpdateWithoutNotes() throws {
        let view = AppUpdateSheet(
            prompt: prompt(required: true, notes: ""),
            isChecking: true,
            onDownload: {},
            onRemindLater: {},
            onSkip: {}
        )

        try assertRendersNonBlank(view, size: CGSize(width: 560, height: 520))
    }

    func testRendersInstallStates() throws {
        let states: [InstallState] = [
            .downloading(0.4),
            .verifying,
            .installing,
            .readyToRelaunch,
            .failed(.digestMismatch),
            .failed(.teamIDMismatch(expected: "A", actual: "B"))
        ]
        for state in states {
            let view = AppUpdateSheet(
                prompt: prompt(required: false),
                isChecking: false,
                installState: state,
                installUnavailableReason: nil,
                onDownload: {},
                onRemindLater: {},
                onSkip: {},
                onInstall: {}
            )
            try assertRendersNonBlank(view, size: CGSize(width: 560, height: 520))
        }
    }

    func testRendersInstallUnavailableReason() throws {
        let view = AppUpdateSheet(
            prompt: prompt(required: false),
            isChecking: false,
            installState: .idle,
            installUnavailableReason: "In-app updates are unavailable when running from a source checkout.",
            onDownload: {},
            onRemindLater: {},
            onSkip: {},
            onInstall: {}
        )

        try assertRendersNonBlank(view, size: CGSize(width: 560, height: 520))
    }

    private func prompt(required: Bool, notes: String = "## Changes\n- **one**\n- two") -> UpdatePrompt {
        UpdatePrompt(
            versionCode: 30002,
            title: "v3.0.1",
            tag: "v3.0.1",
            notes: notes,
            publishedDate: Date(timeIntervalSince1970: 1_790_000_000),
            htmlURL: URL(string: "https://example.invalid/rel"),
            asset: nil,
            isRequired: required
        )
    }
}
