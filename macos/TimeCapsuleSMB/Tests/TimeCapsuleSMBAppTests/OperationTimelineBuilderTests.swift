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
                payload: .object(["summary": .string("deployment completed.")])
            )
        ]

        let timeline = OperationTimelineBuilder.timeline(from: events)

        XCTAssertEqual(timeline.map(\.title), ["Uploading", "Needs Confirmation", "Done"])
        XCTAssertEqual(timeline[0].risk, "remote_write")
        XCTAssertEqual(timeline[0].cancellable, false)
        XCTAssertEqual(timeline[0].state, .succeeded)
        XCTAssertEqual(timeline[1].state, .warning)
        XCTAssertEqual(timeline[2].state, .succeeded)
        XCTAssertEqual(timeline[2].detail, "deployment completed.")
    }

    func testStageBecomesSucceededWhenLaterStageForSameOperationAppears() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "validate_artifacts"),
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Checking Bundled Files", "Running Checkup", "Uploading"])
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

        XCTAssertEqual(timeline.map(\.title), ["Uploading", "Failed"])
        XCTAssertEqual(timeline.map(\.state), [.running, .failed])
    }

    func testOperationTitlesAreUserFacing() {
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("deploy"), "Install / Update")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("doctor"), "Checkup")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("repair-xattrs"), "File Metadata Repair")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("paths"), "App Readiness")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("flash"), "Persistent NetBSD4 Boot Hook")
    }

    func testDeployStartupStagesAreUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "probe_runtime"),
            BackendEvent(type: "stage", operation: "deploy", stage: "post_reboot_activation"),
            BackendEvent(type: "stage", operation: "deploy", stage: "verify_runtime_activation")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Checking SMB", "Starting SMB", "Verifying SMB"])
    }
}
