import XCTest
@testable import TimeCapsuleSMBApp

final class DashboardPresentationTests: XCTestCase {
    func testDeployPlanPresentationSeparatesSummaryAdvancedAndWarnings() throws {
        let plan = try netbsd4DeployPlan().decode(DeployPlanPayload.self)
        let profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(payloadFamily: "netbsd4_samba4"),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")

        let presentation = DeployPlanPresentation(plan: plan, profile: profile, hostWarning: warning)

        XCTAssertEqual(presentation.title, "Install SMB and Start Runtime")
        XCTAssertTrue(presentation.summaryRows.contains(PresentationRow(label: "Payload", value: "netbsd4_samba4")))
        XCTAssertTrue(presentation.advancedRows.contains(PresentationRow(label: "Activation Actions", value: "1")))
        XCTAssertEqual(presentation.warnings.count, 2)
    }

    func testCheckupPresentationHeadlineFollowsState() throws {
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "ssh ok", domain: "Device"),
            testDoctorCheck(status: "WARN", message: "bonjour missing", domain: "Finder")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .warning)

        XCTAssertEqual(presentation.headline, "Checkup found warnings.")
        XCTAssertEqual(presentation.summaryRows.first, PresentationRow(label: "Pass", value: "1"))
        XCTAssertEqual(presentation.groups.first?.domain, "Finder")
    }

    private func netbsd4DeployPlan() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "host": .string("root@10.0.0.2"),
            "volume_root": .string("/Volumes/dk2"),
            "payload_dir": .string("/Volumes/dk2/.samba4"),
            "payload_family": .string("netbsd4_samba4"),
            "netbsd4": .bool(true),
            "requires_reboot": .bool(false),
            "reboot_required": .bool(false),
            "uploads": .array([.object(["description": .string("smbd")])]),
            "pre_upload_actions": .array([]),
            "post_upload_actions": .array([]),
            "activation_actions": .array([.object(["description": .string("start smbd")])]),
            "post_deploy_checks": .array([]),
            "summary": .string("deployment dry-run plan generated.")
        ])
    }
}
