import XCTest
@testable import TimeCapsuleSMBApp

final class DashboardPresentationTests: XCTestCase {
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

    func testOverviewPresentationPromptsForMissingPassword() throws {
        var profile = try makeProfile()
        profile.passwordState = .missing
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .missing,
            displayStatus: .passwordNeeded,
            primaryAction: .replacePassword,
            hostWarning: nil
        )

        let presentation = DeviceDashboardOverviewPresentation(summary: summary)
        let connection = try row(.connection, in: presentation)

        XCTAssertEqual(presentation.primaryAction, .replacePassword)
        XCTAssertTrue(presentation.requiresPasswordReplacement)
        XCTAssertEqual(connection.status, .warning)
        XCTAssertEqual(connection.action, .replacePassword)
    }

    func testOverviewPresentationUsesTypedCheckupDomainsForHealthRows() throws {
        var profile = try makeProfile()
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 100),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )
        let checkup = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "runtime ok", domain: "Runtime"),
            testDoctorCheck(status: "WARN", message: "bonjour warning", domain: "Bonjour"),
            testDoctorCheck(status: "FAIL", message: "smb failed", domain: "SMB"),
            testDoctorCheck(status: "PASS", message: "time machine ok", domain: "Time Machine")
        ]).decode(DoctorPayload.self))
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        )

        let presentation = DeviceDashboardOverviewPresentation(summary: summary, currentCheckupSummary: checkup)

        XCTAssertEqual(try row(.runtime, in: presentation).status, .good)
        XCTAssertEqual(try row(.finderBonjour, in: presentation).status, .warning)
        XCTAssertEqual(try row(.smbAuth, in: presentation).status, .failed)
        XCTAssertEqual(try row(.timeMachine, in: presentation).status, .good)
    }

    func testOverviewPresentationCoversInstallHealthyActivationAndHostWarningStates() throws {
        var readyProfile = try makeProfile()
        readyProfile.lastCheckup = DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 3,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        )
        let ready = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: readyProfile,
            passwordState: .available,
            displayStatus: .readyToInstall,
            primaryAction: .installSMB,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.runtime, in: ready).status, .warning)
        XCTAssertEqual(try row(.runtime, in: ready).action, .installUpdate)

        var healthyProfile = readyProfile
        healthyProfile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 120),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )
        let healthy = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: healthyProfile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.runtime, in: healthy).status, .good)
        XCTAssertEqual(try row(.runtime, in: healthy).action, .openFinder)

        var netbsd4Profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        netbsd4Profile.lastDeploy = healthyProfile.lastDeploy
        let activation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: netbsd4Profile,
            passwordState: .available,
            displayStatus: .activationNeeded,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.runtime, in: activation).status, .warning)
        XCTAssertEqual(try row(.runtime, in: activation).action, .startSMB)

        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")
        let hostWarning = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: healthyProfile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: warning
        ))
        XCTAssertEqual(try row(.timeMachine, in: hostWarning).status, .warning)
        XCTAssertEqual(try row(.timeMachine, in: hostWarning).detail, "Time Machine warning.")
    }

    func testInstallPlanPresentationShowsDeviceImpactAndWarnings() throws {
        let plan = try netbsd4DeployPlan().decode(DeployPlanPayload.self)
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")

        let presentation = InstallPlanPresentation(plan: plan, profile: profile, hostWarning: warning)

        XCTAssertEqual(presentation.title, "Install / Update SMB and Start Runtime")
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(InstallPlanRow(label: "Remote Actions", value: "1"))
        })
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(InstallPlanRow(label: "Expected Downtime", value: "Usually under a minute; the runtime may start without reboot."))
        })
        XCTAssertEqual(presentation.warnings.count, 2)
    }

    func testInstallWorkflowPresentationCoversAllDeployStates() throws {
        let profile = try makeProfile()
        let plan = try testDeployPlanPayload().decode(DeployPlanPayload.self)
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let cases: [(DeployWorkflowState, DeployPlanPayload?, DeployResultPayload?, BackendErrorViewModel?, InstallUserAction?)] = [
            (.idle, nil, nil, nil, .createPlan),
            (.planning, nil, nil, nil, nil),
            (.planReady, plan, nil, nil, .installUpdate),
            (.planStale, plan, nil, nil, .regeneratePlan),
            (.planFailed, nil, nil, error, .createPlan),
            (.deploying, plan, nil, nil, nil),
            (.awaitingConfirmation, plan, nil, nil, nil),
            (.deployed, plan, result, nil, nil),
            (.deployFailed, plan, nil, error, .regeneratePlan)
        ]

        for testCase in cases {
            let presentation = InstallWorkflowPresentation(
                state: testCase.0,
                plan: testCase.1,
                result: testCase.2,
                error: testCase.3,
                events: [],
                currentStage: nil,
                profile: profile
            )
            XCTAssertEqual(presentation.primaryAction, testCase.4, "Unexpected primary action for \(testCase.0)")
        }
    }

    func testInstallCompletionPresentationShowsVerificationAndNextActions() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd4_samba4", verified: true, netbsd4: true)
            .decode(DeployResultPayload.self)

        let presentation = InstallCompletionPresentation(result: result)

        XCTAssertEqual(presentation.title, "Install / Update Verified")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Verified", value: "yes")))
        XCTAssertEqual(presentation.warnings, [
            "NetBSD4 devices may need Start SMB after a later reboot unless the boot hook is patched."
        ])
        XCTAssertEqual(presentation.actions, [.openFinder, .runCheckup, .viewDiagnostics])
    }

    func testInstallTimelinePresentationUsesDeployEventsOnly() {
        let presentation = InstallTimelinePresentation(events: [
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", description: "uploading")
        ], currentStage: nil)

        XCTAssertEqual(presentation.items.count, 1)
        XCTAssertEqual(presentation.items.first?.title, "Uploading")
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

    private func makeProfile(
        id: String = "device-one",
        payloadFamily: String = "netbsd6_samba4"
    ) throws -> DeviceProfile {
        DeviceProfile.make(
            id: id,
            configuredDevice: try testConfiguredDevice(payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }

    private func row(
        _ domain: DashboardHealthDomain,
        in presentation: DeviceDashboardOverviewPresentation
    ) throws -> DashboardHealthRow {
        let section = try XCTUnwrap(presentation.healthSections.first { $0.domain == domain })
        return try XCTUnwrap(section.rows.first)
    }
}
