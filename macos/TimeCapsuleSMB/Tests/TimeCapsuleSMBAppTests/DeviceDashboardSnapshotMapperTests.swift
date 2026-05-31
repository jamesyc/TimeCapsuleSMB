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
