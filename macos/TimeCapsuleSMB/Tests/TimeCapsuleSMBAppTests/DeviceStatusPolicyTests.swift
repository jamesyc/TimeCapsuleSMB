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
        let profile = try makeProfile(lastCheckup: passedCheckup(), lastDeploy: deployed())

        XCTAssertEqual(status(profile, .available, operation: "doctor"), .checking)
        XCTAssertEqual(status(profile, .available, operation: "deploy"), .installing)
        XCTAssertEqual(status(profile, .available, operation: "fsck"), .maintaining)
    }

    func testHealthStatusFallsBackThroughCheckupAndDeploySnapshots() throws {
        XCTAssertEqual(status(try makeProfile(), .available), .unchecked)
        XCTAssertEqual(status(try makeProfile(lastCheckup: passedCheckup()), .available), .readyToInstall)
        XCTAssertEqual(status(try makeProfile(lastCheckup: passedCheckup(), lastDeploy: deployed()), .available), .healthy)
        XCTAssertEqual(status(try makeProfile(lastCheckup: warningCheckup(), lastDeploy: deployed()), .available), .warning)
        XCTAssertEqual(status(try makeProfile(lastCheckup: failedCheckup(), lastDeploy: deployed()), .available), .failed)
    }

    func testNetBSD4WarningAfterDeployMapsToActivationNeeded() throws {
        let profile = try makeProfile(
            payloadFamily: "netbsd4_samba4",
            lastCheckup: warningCheckup(),
            lastDeploy: deployed()
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
            for: try makeProfile(lastCheckup: passedCheckup()),
            passwordState: .available,
            activeOperation: nil
        ), .installSMB)
        XCTAssertEqual(DashboardPrimaryActionPolicy.primaryAction(
            for: try makeProfile(lastCheckup: passedCheckup(), lastDeploy: deployed()),
            passwordState: .available,
            activeOperation: nil
        ), .openSMB)
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
        lastDeploy: DeviceDeploySnapshot? = nil
    ) throws -> DeviceProfile {
        var profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(payloadFamily: payloadFamily),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true),
            date: Date(timeIntervalSince1970: 1)
        )
        profile.lastCheckup = lastCheckup
        profile.lastDeploy = lastDeploy
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

    private func deployed() -> DeviceDeploySnapshot {
        DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 11),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )
    }
}
