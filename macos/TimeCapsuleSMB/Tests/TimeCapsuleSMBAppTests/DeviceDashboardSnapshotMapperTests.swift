import XCTest
@testable import TimeCapsuleSMBApp

final class DeviceDashboardSnapshotMapperTests: XCTestCase {
    func testPassedCheckupMapsRuntimeToInstalledVerified() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "device")
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
            testDoctorCheck(status: "PASS", message: "local checks passed", domain: "computer")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile,
            skipSSH: true,
            state: .passed,
            summary: summary
        )

        XCTAssertNil(runtimeState)
    }

    func testThisMacFailuresLeaveTheDeviceHealthy() throws {
        // #372: this Mac's nsmb.conf turned off SMB 3 while the device passed
        // every check of its own.
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "device"),
            testDoctorCheck(status: "PASS", message: "SMB file create works", domain: "device"),
            testDoctorCheck(status: "FAIL", message: "/etc/nsmb.conf turns off SMB 3", domain: "computer"),
            testDoctorCheck(status: "WARN", message: "Bonjour IPv6 not seen", domain: "computer")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        let snapshot = DeviceDashboardSnapshotMapper.checkupSnapshot(
            state: .failed, summary: summary, observedAt: Date(timeIntervalSince1970: 1))

        XCTAssertEqual(runtimeState?.state, .installedVerified)
        // The Checkup itself failed; each section keeps its own result.
        XCTAssertEqual(snapshot.state, .failed)
        XCTAssertEqual([snapshot.passCount, snapshot.warnCount, snapshot.failCount], [2, 1, 1])
        XCTAssertEqual(snapshot.device?.status, .pass)
        XCTAssertEqual(snapshot.device.map { [$0.passCount, $0.warnCount, $0.failCount] }, [2, 0, 0])
        XCTAssertEqual(snapshot.thisMac?.status, .fail)
        XCTAssertEqual(snapshot.thisMac.map { [$0.passCount, $0.warnCount, $0.failCount] }, [0, 1, 1])
    }

    func testDeviceFailuresStillMarkTheDeviceUnhealthy() throws {
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "FAIL", message: "managed smbd parent process is not running", domain: "device"),
            testDoctorCheck(status: "FAIL", message: "SMB IPv4 is not reachable", domain: "computer")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        let snapshot = DeviceDashboardSnapshotMapper.checkupSnapshot(
            state: .failed, summary: summary, observedAt: Date(timeIntervalSince1970: 1))

        XCTAssertEqual(runtimeState?.state, .unhealthy)
        XCTAssertEqual(runtimeState?.summary, "PASS 0, WARN 0, FAIL 1")
        XCTAssertEqual([snapshot.passCount, snapshot.warnCount, snapshot.failCount], [0, 0, 2])
        XCTAssertEqual(snapshot.device?.status, .fail)
    }

    func testCheckupThatReachedNothingOnTheDeviceKeepsTheDevicesLastKnownHealth() throws {
        // SSH refused (a VPN on this Mac, or the device off): nothing says the
        // device is broken, so its saved health stays; the Connection row and
        // the Checkup's own result show the failure.
        var profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        profile.runtimeState = testRuntimeState(state: .installedVerified, payloadFamily: "netbsd6_samba4", verified: true)
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "FAIL", message: "ssh down", domain: "computer"),
            testDoctorCheck(status: "SKIP", message: "device checks skipped", domain: "device", code: "device_not_checked")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        let snapshot = DeviceDashboardSnapshotMapper.checkupSnapshot(
            state: .failed, summary: summary, observedAt: Date(timeIntervalSince1970: 1))

        XCTAssertNil(runtimeState)
        XCTAssertEqual(snapshot.state, .failed)
        XCTAssertEqual(snapshot.device?.status, .notChecked)
        XCTAssertEqual(snapshot.thisMac?.status, .fail)
    }

    func testPayloadMissingOrDamagedOnDiskNeedsInstallEvenOnNetBSD4() throws {
        // Erased disk, or a damaged copy on it: Activate cannot help, only
        // Install / Update can. The same Checkup on NetBSD 4 must not ask for
        // Activate, and the state keeps which of the two it was.
        let profile = try makeProfile(payloadFamily: "netbsd4le_samba4")
        for code in [DoctorSummary.payloadMissingFromDiskResultCode, DoctorSummary.payloadDamagedOnDiskResultCode] {
            let summary = try makeDoctorSummary(checks: [
                testDoctorCheck(status: "FAIL", message: "the Samba files on the disk cannot be used",
                                domain: "device", code: code)
            ])

            let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
                profile: profile, skipSSH: false, state: .failed, summary: summary)

            XCTAssertEqual(summary.payloadProblemCode, code)
            XCTAssertEqual(runtimeState?.state, .notInstalled)
            XCTAssertEqual(runtimeState?.errorCode, code)
        }
    }

    func testAFailedCheckupWithoutAPayloadCodeHasNoPayloadProblem() throws {
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "FAIL", message: "managed smbd parent process is not running", domain: "device")
        ])
        XCTAssertNil(summary.payloadProblemCode)
    }

    func testDiskNotMountingIsUnhealthyWithItsOwnCode() throws {
        // Installed and running, but no disk mounts: the dashboard names the
        // restart; any other failure keeps the generic code.
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")
        let stuck = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "FAIL", message: "a disk on the device will not mount",
                            domain: "device", code: "disk_not_mounting")
        ])
        let other = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "FAIL", message: "managed smbd parent process is not running", domain: "device")
        ])

        let stuckState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: stuck)
        let otherState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: other)

        XCTAssertTrue(stuck.diskNotMounting)
        XCTAssertFalse(other.diskNotMounting)
        XCTAssertEqual(stuckState?.state, .unhealthy)
        XCTAssertEqual(stuckState?.errorCode, DoctorSummary.diskNotMountingResultCode)
        XCTAssertEqual(otherState?.state, .unhealthy)
        XCTAssertEqual(otherState?.errorCode, "doctor_failed")
    }

    func testASectionDecodesWithoutItsTelemetryCodeLists() throws {
        // fail_codes and warn_codes are for telemetry; the app reads only the
        // status and counts.
        let data = Data(#"{"status": "warn", "counts": {"WARN": 1}, "fail_codes": [], "warn_codes": ["x"]}"#.utf8)
        let section = try JSONDecoder().decode(DoctorSection.self, from: data)
        XCTAssertEqual(section, DoctorSection(status: .warn, counts: ["WARN": 1]))
    }

    func testTheHelpersSectionStatusesWinOverTheChecks() throws {
        // The payload's sections decide; a device "not checked" from the helper
        // keeps the saved health even with device rows present.
        var payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "local tools", domain: "computer")
        ]).decode(DoctorPayload.self)
        payload.status = .warn
        payload.sections = [
            "computer": DoctorSection(status: .warn, counts: ["PASS": 1, "WARN": 1]),
            "device": DoctorSection(status: .notChecked, counts: [:])
        ]
        let summary = DoctorSummary(payload: payload)

        XCTAssertEqual(summary.overallStatus, .warn)
        XCTAssertEqual(summary.sections[.thisMac]?.status, .warn)
        XCTAssertNil(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: try makeProfile(payloadFamily: "netbsd6_samba4"), skipSSH: false, state: .warning, summary: summary))
    }
    func testWarningCheckupOnNetBSD4IsInstalledUnverified() throws {
        // Doctor warnings come from a running runtime; a stopped one fails.
        var profile = try makeProfile(payloadFamily: "netbsd4le_samba4")
        profile.runtimeState = testRuntimeState(state: .installedVerified, payloadFamily: "netbsd4le_samba4", verified: true)
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(status: "WARN", message: "NBNS check skipped: timed out", domain: "device")
        ])

        let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .warning, summary: summary)

        XCTAssertEqual(runtimeState?.state, .installedUnverified)
    }

    func testRuntimeNotStartedNeedsActivationOnlyOnNetBSD4() throws {
        let summary = try makeDoctorSummary(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "managed runtime directory /mnt/Memory/samba4 is missing; run deploy or activate to start the managed runtime",
                domain: "device",
                code: "runtime_not_started"
            )
        ])
        for (family, expected) in [("netbsd4le_samba4", DeviceRuntimeState.activationNeeded), ("netbsd6_samba4", .unhealthy)] {
            let runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
                profile: try makeProfile(payloadFamily: family), skipSSH: false, state: .failed, summary: summary)
            XCTAssertEqual(runtimeState?.state, expected, family)
        }
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
        XCTAssertEqual(failed.deployState.operationID, operation.id.uuidString)
        XCTAssertEqual(failed.deployState.startedAt, finishedAt)

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

    func testProgressKeepsAttemptDetailsWhileUpdatingItsStage() {
        let startedAt = Date(timeIntervalSince1970: 100)
        let observedAt = Date(timeIntervalSince1970: 200)
        var current = testDeployState(status: .deploying, startedAt: startedAt,
            finishedAt: nil, summary: "Waiting for confirmation", diagnosticText: "old diagnostic")
        current.operationID = "current-attempt"
        current.summaryRef = BackendSummary(key: "deploy.result.default_message", text: current.summary)
        let runtime = testRuntimeState(state: .installing, summary: "Preparing install")

        for previousRuntime in [nil, runtime] {
            let next = DeviceDashboardSnapshotMapper.inProgressDeploySnapshots(
                current: current, runtimeState: previousRuntime, status: .awaitingConfirmation,
                stage: "confirm_reboot", observedAt: observedAt)

            XCTAssertEqual(next.deployState.operationID, current.operationID)
            XCTAssertEqual(next.deployState.startedAt, startedAt)
            XCTAssertEqual(next.deployState.updatedAt, observedAt)
            XCTAssertEqual(next.deployState.status, .awaitingConfirmation)
            XCTAssertEqual(next.deployState.stage, "confirm_reboot")
            XCTAssertEqual(next.deployState.payloadFamily, current.payloadFamily)
            XCTAssertEqual(next.deployState.summaryRef, current.summaryRef)
            XCTAssertEqual(next.deployState.summary, current.summary)
            XCTAssertNil(next.deployState.finishedAt)
            XCTAssertNil(next.deployState.diagnosticText)
            XCTAssertEqual(next.runtimeState.state, .installing)
            XCTAssertEqual(next.runtimeState.stage, "confirm_reboot")
            XCTAssertEqual(next.runtimeState.summary, previousRuntime?.summary ?? "")
            XCTAssertEqual(next.runtimeState.payloadFamily, previousRuntime?.payloadFamily ?? current.payloadFamily)
        }
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
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "device"),
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
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "device"),
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
