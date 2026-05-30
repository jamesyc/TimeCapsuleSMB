import XCTest
@testable import TimeCapsuleSMBApp

final class OperationTimelineBuilderTests: XCTestCase {
    func testBuildsUserFacingTimelineFromStagesResultsAndErrors() {
        let events = [
            BackendEvent(
                type: "stage",
                operation: "deploy",
                stage: "upload_payload",
                risk: "remote_write",
                cancellable: false,
                description: "Upload managed Samba payload files."
            ),
            BackendEvent(
                type: "error",
                operation: "deploy",
                code: "confirmation_required",
                message: "Confirm deployment."
            ),
            BackendEvent(
                type: "result",
                operation: "deploy",
                ok: true,
                payload: .object(["summary": .string("Deployment completed.")])
            )
        ]

        let timeline = OperationTimelineBuilder.timeline(from: events)

        XCTAssertEqual(timeline.map(\.title), ["Upload Payload", "Needs Confirmation", "Done"])
        XCTAssertEqual(timeline[0].risk, "remote_write")
        XCTAssertEqual(timeline[0].cancellable, false)
        XCTAssertEqual(timeline[0].state, .succeeded)
        XCTAssertEqual(timeline[1].state, .warning)
        XCTAssertEqual(timeline[2].state, .succeeded)
        XCTAssertEqual(timeline[2].detail, "Deployment completed.")
    }

    func testStageBecomesSucceededWhenLaterStageForSameOperationAppears() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "validate_artifacts"),
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Check Local Files", "Running Checkup", "Upload Payload"])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .running, .running])
    }

    func testSuccessfulResultCompletesLastStage() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "uninstall", stage: "build_uninstall_plan"),
            BackendEvent(type: "stage", operation: "uninstall", stage: "uninstall_payload"),
            BackendEvent(type: "result", operation: "uninstall", ok: true, payload: .object(["summary": .string("removed")]))
        ])

        XCTAssertEqual(timeline.map(\.title), ["Planning Uninstall", "Removing Managed Files", "Done"])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .succeeded, .succeeded])
    }

    func testFailureDoesNotMarkCurrentStageSucceeded() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload"),
            BackendEvent(type: "result", operation: "deploy", ok: false, payload: .object(["summary": .string("upload failed")]))
        ])

        XCTAssertEqual(timeline.map(\.title), ["Upload Payload", "Failed"])
        XCTAssertEqual(timeline.map(\.state), [.failed, .failed])
    }

    func testErrorMarksCurrentStageFailed() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "read_mast"),
            BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: "No deployable HFS disk was found.")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Find Payload Volume", "Needs Attention"])
        XCTAssertEqual(timeline.map(\.state), [.failed, .failed])
    }

    func testOperationTitlesAreUserFacing() {
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("deploy"), "Install / Update")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("doctor"), "Checkup")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("repair-xattrs"), "File Metadata Repair")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("capabilities"), "App Readiness")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("flash"), "Persistent NetBSD4 Boot Hook")
    }

    func testDeployStartupStagesAreUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "probe_runtime"),
            BackendEvent(type: "stage", operation: "deploy", stage: "post_reboot_activation"),
            BackendEvent(type: "stage", operation: "deploy", stage: "verify_runtime_activation")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Check Boot Startup", "Start SMB After Reboot", "Verify SMB Startup"])
        XCTAssertEqual(
            timeline.first?.detail,
            "Checking whether the device will start TimeCapsuleSMB automatically."
        )
    }

    func testConfigureAcpIdentityStageIsUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "configure", stage: "acp_identity_probe")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Checking AirPort Identity"])
    }

    func testActivateRuntimeProbeStageIsUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "activate", stage: "probe_runtime")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Check Existing Runtime"])
        XCTAssertEqual(
            timeline.first?.detail,
            "Checking whether TimeCapsuleSMB is already running before activating it."
        )
    }

    func testDeployCleanupStageWarnsAboutOldFileDeletion() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "pre_upload_actions")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Stop Existing Runtime"])
    }

    func testDeployUploadStagesAreUserFacingAndPresentTense() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_mdns_advertiser"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_nbns_advertiser"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_boot_files"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_runtime_config"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_samba_accounts")
        ])

        XCTAssertEqual(timeline.map(\.title), [
            "Upload smbd",
            "Upload mdns-advertiser",
            "Upload nbns-advertiser",
            "Upload Boot Files",
            "Upload Runtime Config",
            "Upload Samba Account Files"
        ])
    }

    func testDeployRebootStagesUseLocalizedDetails() {
        XCTAssertEqual(
            OperationTimelineBuilder.stageDetail(
                for: "deploy",
                stage: "wait_for_reboot_up",
                fallback: "raw backend detail"
            ),
            "Device went down; waiting for it to come back up."
        )
        XCTAssertEqual(
            OperationTimelineBuilder.stageDetail(
                for: "deploy",
                stage: "verify_runtime_reboot",
                fallback: "raw backend detail"
            ),
            "Device is back online. Waiting for managed runtime to finish starting."
        )
    }

    func testAllKnownDeployStagesHaveLocalizedTitlesAndDetails() {
        let deployStages = [
            "load_config",
            "resolve_managed_target",
            "validate_artifacts",
            "check_compatibility",
            "read_mast",
            "select_payload_home",
            "build_deployment_plan",
            "pre_upload_actions",
            "prepare_deployment_files",
            "upload_payload",
            "upload_smbd",
            "upload_mdns_advertiser",
            "upload_nbns_advertiser",
            "upload_boot_files",
            "upload_runtime_config",
            "upload_samba_accounts",
            "post_upload_actions",
            "verify_payload_upload",
            "flush_payload_upload",
            "verify_payload_upload_after_sync",
            "reboot",
            "wait_for_reboot_down",
            "wait_for_reboot_up",
            "probe_runtime",
            "activate_runtime",
            "post_reboot_activation",
            "verify_runtime_activation",
            "verify_runtime_reboot"
        ]

        for stage in deployStages {
            let title = OperationTimelineBuilder.stageTitle(for: "deploy", stage: stage)
            let detail = OperationTimelineBuilder.stageDetail(for: "deploy", stage: stage, fallback: nil)
            XCTAssertFalse(title.hasPrefix("timeline."), "\(stage) title should be localized")
            XCTAssertFalse(title.contains("_"), "\(stage) title should not fall back to title-cased stage id")
            XCTAssertNotNil(detail, "\(stage) should have a localized detail")
            XCTAssertFalse(detail?.hasPrefix("timeline.") == true, "\(stage) detail should be localized")
        }
    }

    func testRemovedNetBSD4DeployActivationStageIsNotMapped() {
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "netbsd4_activation"), "Netbsd4 Activation")
        XCTAssertNil(OperationTimelineBuilder.stageDetail(for: "deploy", stage: "netbsd4_activation", fallback: nil))
    }
}
