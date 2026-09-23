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
