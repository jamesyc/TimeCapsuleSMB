import XCTest
@testable import TimeCapsuleSMBApp

final class DeviceDashboardSnapshotMapperTests: XCTestCase {
    func testPassedCheckupMapsRuntimeToInstalledVerified() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: false,
            state: .passed,
            summary: summary
        )

        XCTAssertEqual(runtimeState?.state, .installedVerified)
        XCTAssertEqual(runtimeState?.source, .doctor)
        XCTAssertEqual(runtimeState?.payloadFamily, "netbsd6_samba4")
        XCTAssertEqual(runtimeState?.verified, true)
    }

    func testSkippedSSHCheckupDoesNotInventRuntimeState() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "local checks passed", domain: "General")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: true,
            state: .passed,
            summary: summary
        )

        XCTAssertNil(runtimeState)
    }

    func testWarningCheckupKeepsNetBSD4InstalledRuntimeActivationNeeded() throws {
        var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        profile.runtimeState = testRuntimeState(
            state: .installedVerified,
            payloadFamily: "netbsd4_samba4",
            verified: true
        )
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "WARN", message: "activation required after reboot", domain: "Runtime")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: false,
            state: .warning,
            summary: summary
        )

        XCTAssertEqual(runtimeState?.state, .activationNeeded)
        XCTAssertEqual(runtimeState?.source, .doctor)
        XCTAssertEqual(runtimeState?.payloadFamily, "netbsd4_samba4")
        XCTAssertEqual(runtimeState?.verified, false)
    }

    func testDeployResultUsesCurrentOperationIdentityWhenPriorDeployExists() throws {
        var profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        var prior = testDeployState(startedAt: Date(timeIntervalSince1970: 100))
        prior.operationID = "prior-operation"
        profile.lastDeployState = prior
        let operation = ActiveOperation(operation: "deploy", profileID: profile.id, context: nil)
        let finishedAt = Date(timeIntervalSince1970: 200)
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)

        let succeeded = DeviceDashboardSnapshotMapper.succeededDeploySnapshots(
            operation: operation,
            profile: profile,
            result: result,
            payloadFamily: "netbsd6_samba4",
            stage: nil,
            finishedAt: finishedAt
        )
        let failed = DeviceDashboardSnapshotMapper.failedDeploySnapshots(
            operation: operation,
            profile: profile,
            stage: nil,
            payloadFamily: "netbsd6_samba4",
            error: nil,
            failedAt: finishedAt
        )
        XCTAssertEqual(succeeded.deployState.operationID, operation.id.uuidString)
        XCTAssertEqual(succeeded.deployState.startedAt, finishedAt)
        XCTAssertEqual(failed?.deployState.operationID, operation.id.uuidString)
        XCTAssertEqual(failed?.deployState.startedAt, finishedAt)

        profile.lastDeployState?.operationID = operation.id.uuidString
        let sameOperation = DeviceDashboardSnapshotMapper.succeededDeploySnapshots(
            operation: operation,
            profile: profile,
            result: result,
            payloadFamily: "netbsd6_samba4",
            stage: nil,
            finishedAt: finishedAt
        )
        XCTAssertEqual(sameOperation.deployState.startedAt, prior.startedAt)
    }

    func testSucceededDeploySnapshotsKeepTheResultSummaryKey() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)

        let snapshots = DeviceDashboardSnapshotMapper.succeededDeploySnapshots(
            operation: ActiveOperation(operation: "deploy", profileID: profile.id, context: nil),
            profile: profile,
            result: result,
            payloadFamily: "netbsd6_samba4",
            stage: nil,
            finishedAt: Date(timeIntervalSince1970: 10)
        )

        XCTAssertEqual(snapshots.deployState.summaryRef?.key, "backend.summary.deploy_completed")
        XCTAssertEqual(snapshots.runtimeState.summaryRef?.key, "backend.summary.deploy_completed")
        L10n.apply(language: .german)
        XCTAssertEqual(snapshots.deployState.localizedSummary, L10n.string("backend.summary.deploy_completed"))
        let reloaded = try JSONDecoder().decode(
            DeviceDeployStateSnapshot.self,
            from: JSONEncoder().encode(snapshots.deployState)
        )
        XCTAssertEqual(reloaded.summaryRef, snapshots.deployState.summaryRef)
    }

    func testCheckupCountsFollowTheLanguageInUseWhenShown() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime"),
            testDoctorCheck(status: "WARN", message: "slow disk", domain: "Disk")
        ])
        L10n.apply(language: .english)

        let runtimeState = try XCTUnwrap(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: false,
            state: .warning,
            summary: summary
        ))

        XCTAssertEqual(runtimeState.summaryRef?.key, "summary.checkup_counts")
        XCTAssertEqual(runtimeState.summaryRef?.arguments, [BackendSummaryArgument.int(1), .int(1), .int(0)])
        XCTAssertEqual(runtimeState.localizedSummary, "PASS 1, WARN 1, FAIL 0")
        L10n.apply(language: .russian)
        XCTAssertEqual(
            runtimeState.localizedSummary,
            String(format: L10n.string("summary.checkup_counts"), locale: AppLanguage.russian.locale, 1, 1, 0)
        )
        XCTAssertNotEqual(runtimeState.localizedSummary, "PASS 1, WARN 1, FAIL 0")
    }

    func testFailedCheckupCountsFollowTheLanguageInUseWhenShown() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime"),
            testDoctorCheck(status: "FAIL", message: "share missing", domain: "SMB")
        ])
        L10n.apply(language: .english)

        let runtimeState = try XCTUnwrap(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: false,
            state: .failed,
            summary: summary
        ))

        XCTAssertEqual(runtimeState.state, .unhealthy)
        XCTAssertNil(runtimeState.errorMessage)
        XCTAssertEqual(runtimeState.localizedSummary, "PASS 1, WARN 0, FAIL 1")
        L10n.apply(language: .german)
        XCTAssertEqual(
            runtimeState.localizedSummary,
            String(format: L10n.string("summary.checkup_counts"), locale: AppLanguage.german.locale, 1, 0, 1)
        )
        XCTAssertNotEqual(runtimeState.localizedSummary, "PASS 1, WARN 0, FAIL 1")
    }

    func testUnhealthyRuntimePrefersItsErrorMessageOverSavedCounts() {
        var runtimeState = testRuntimeState(state: .unhealthy, summary: "PASS 1, WARN 0, FAIL 1")
        runtimeState.summaryRef = BackendSummary(
            key: "summary.checkup_counts",
            arguments: [.int(1), .int(0), .int(1)],
            text: "PASS 1, WARN 0, FAIL 1"
        )
        runtimeState.errorMessage = "  smbd crashed  "
        XCTAssertEqual(runtimeState.localizedSummary, "smbd crashed")

        runtimeState.errorMessage = "  "
        runtimeState.summaryRef = nil
        runtimeState.summary = ""
        XCTAssertEqual(runtimeState.localizedSummary, L10n.string("runtime.state.unhealthy"))
    }

    private func makeDoctorSummary(checks: [JSONValue]) throws -> DoctorSummary {
        DoctorSummary(payload: try testDoctorPayload(checks: checks).decode(DoctorPayload.self))
    }

    private func makeProfile(
        id: String = "device-one",
        host: String = "10.0.0.2",
        payloadFamily: String
    ) throws -> DeviceProfile {
        DeviceProfile.make(
            id: id,
            configuredDevice: try testConfiguredDevice(host: host, payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }
}
