import XCTest
@testable import TimeCapsuleSMBApp

final class DeviceStatusPolicyTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeviceDisplayStatus.allCases, [
            .unchecked,
            .passwordNeeded,
            .passwordInvalid,
            .keychainUnavailable,
            .checking,
            .installing,
            .maintaining,
            .readyToInstall,
            .healthy,
            .warning,
            .failed,
            .activationNeeded,
            .removed,
            .offline,
            .unsupported
        ])
    }

    func testDisplayStatusTitlesAreLocalized() {
        XCTAssertEqual(DeviceDisplayStatus.allCases.map(\.title), [
            "Unchecked",
            "Password Needed",
            "Password Invalid",
            "Keychain Unavailable",
            "Checking",
            "Installing",
            "Maintenance",
            "Ready to Install",
            "Healthy",
            "Warning",
            "Failed",
            "Activation Needed",
            "Removed",
            "Offline",
            "Unsupported"
        ])
    }

    func testInstallingStatusUsesInstallIcon() {
        XCTAssertEqual(DeviceDisplayStatus.installing.systemImage, "square.and.arrow.down.on.square")
    }

    func testPasswordStateTitlesAreLocalized() {
        XCTAssertEqual(DevicePasswordState.allCases.map(\.title), [
            "Unknown",
            "Available",
            "Missing",
            "Invalid",
            "Keychain unavailable"
        ])
    }

    func testDashboardTabTitlesAreLocalized() {
        XCTAssertEqual(DeviceDashboardTab.allCases.map(\.title), [
            "Overview",
            "Install / Update",
            "Checkup",
            "Maintenance",
            "Settings"
        ])
    }

    func testPasswordStatesTakePriority() throws {
        let profile = try makeProfile()

        XCTAssertEqual(status(profile, .missing), .passwordNeeded)
        XCTAssertEqual(status(profile, .unknown), .passwordNeeded)
        XCTAssertEqual(status(profile, .invalid), .passwordInvalid)
        XCTAssertEqual(status(profile, .keychainUnavailable), .keychainUnavailable)
    }

    func testActiveOperationOverridesStoredHealth() throws {
        let profile = try makeProfile(runtimeState: testRuntimeState())

        XCTAssertEqual(status(profile, .available, operation: "doctor"), .checking)
        XCTAssertEqual(status(profile, .available, operation: "deploy"), .installing)
        XCTAssertEqual(status(profile, .available, operation: "fsck"), .maintaining)
    }

    func testHealthStatusComesFromRuntimeState() throws {
        XCTAssertEqual(status(try makeProfile(), .available), .unchecked)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .installedVerified)), .available), .healthy)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .installedUnverified, verified: false)), .available), .warning)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .notInstalled, source: .doctor, verified: false)), .available), .readyToInstall)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .installing, verified: nil)), .available), .installing)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .installFailed, verified: false)), .available), .failed)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .installInterrupted, verified: nil)), .available), .failed)
        XCTAssertEqual(status(try makeProfile(runtimeState: testRuntimeState(state: .unhealthy, source: .doctor, verified: false)), .available), .failed)
    }

    func testCheckupAndDeployUISnapshotsDoNotDriveSidebarStatus() throws {
        let profile = try makeProfile(
            lastCheckup: passedCheckup(),
            lastDeployState: deployed()
        )

        XCTAssertEqual(status(profile, .available), .unchecked)
    }

    func testFailedRuntimeStateOverridesPreviousHealthyCheckupStatus() throws {
        let profile = try makeProfile(
            lastCheckup: passedCheckup(),
            lastDeployState: deployed(),
            runtimeState: testRuntimeState(
                state: .installFailed,
                verified: false,
                errorMessage: "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart."
            )
        )

        XCTAssertEqual(status(profile, .available), .failed)
    }

    func testNetBSD4ActivationNeededRuntimeStateMapsToActivationNeeded() throws {
        let profile = try makeProfile(
            payloadFamily: "netbsd4_samba4",
            lastCheckup: warningCheckup(),
            lastDeployState: deployed(),
            runtimeState: testRuntimeState(state: .activationNeeded, source: .doctor, payloadFamily: "netbsd4_samba4", verified: false)
        )

        XCTAssertEqual(status(profile, .available), .activationNeeded)
    }

    func testPrimaryActionPolicyUsesStatus() throws {
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(),
            passwordState: .missing,
            activeOperation: nil
        ), .replacePassword)
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(),
            passwordState: .available,
            activeOperation: nil
        ), .runCheckup)
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(runtimeState: testRuntimeState()),
            passwordState: .available,
            activeOperation: nil
        ), .openSMB)
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(
                lastCheckup: passedCheckup(),
                lastDeployState: deployed(),
                runtimeState: testRuntimeState()
            ),
            passwordState: .available,
            activeOperation: nil
        ), .openSMB)
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(runtimeState: testRuntimeState(state: .notInstalled, source: .doctor, verified: false)),
            passwordState: .available,
            activeOperation: nil
        ), .installSMB)
    }

    private func status(
        _ profile: DeviceProfile,
        _ passwordState: DevicePasswordState,
        operation: String? = nil
    ) -> DeviceDisplayStatus {
        DeviceStatusPolicy.status(
            for: profile,
            passwordState: passwordState,
            activeOperation: operation.map {
                ActiveOperation(operation: $0, profileID: profile.id, context: profile.runtimeContext)
            }
        )
    }

    private func makeProfile(
        payloadFamily: String = "netbsd6_samba4",
        lastCheckup: DeviceCheckupSnapshot? = nil,
        lastDeployState: DeviceDeployStateSnapshot? = nil,
        runtimeState: DeviceRuntimeStateSnapshot? = nil
    ) throws -> DeviceProfile {
        var profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true),
            date: Date(timeIntervalSince1970: 1)
        )
        profile.lastCheckup = lastCheckup
        profile.lastDeployState = lastDeployState
        profile.runtimeState = runtimeState
        return profile
    }

    private func passedCheckup() -> DeviceCheckupSnapshot {
        DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 10),
            state: .passed,
            passCount: 3,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        )
    }

    private func warningCheckup() -> DeviceCheckupSnapshot {
        DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 10),
            state: .warning,
            passCount: 2,
            warnCount: 1,
            failCount: 0,
            summary: "warning"
        )
    }

    private func failedCheckup() -> DeviceCheckupSnapshot {
        DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 10),
            state: .failed,
            passCount: 1,
            warnCount: 0,
            failCount: 1,
            summary: "failed"
        )
    }

    private func deployed(verified: Bool = true) -> DeviceDeployStateSnapshot {
        testDeployState(
            startedAt: Date(timeIntervalSince1970: 11),
            updatedAt: Date(timeIntervalSince1970: 11),
            finishedAt: Date(timeIntervalSince1970: 11),
            verified: verified
        )
    }
}
