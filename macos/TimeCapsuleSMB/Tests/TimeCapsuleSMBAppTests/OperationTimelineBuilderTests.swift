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
        XCTAssertEqual(timeline[1].state, .warning)
        XCTAssertEqual(timeline[2].detail, "deployment completed.")
    }

    func testOperationTitlesAreUserFacing() {
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("deploy"), "Install / Update")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("doctor"), "Checkup")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("repair-xattrs"), "File Metadata Repair")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("paths"), "App Readiness")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("flash"), "Persistent NetBSD4 Boot Hook")
    }
}
