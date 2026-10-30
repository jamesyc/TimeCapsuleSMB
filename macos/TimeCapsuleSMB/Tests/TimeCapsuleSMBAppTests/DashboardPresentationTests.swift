import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DashboardPresentationTests: XCTestCase {
    func testCheckupPresentationHeadlineFollowsState() throws {
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "ssh ok", domain: "computer"),
            testDoctorCheck(status: "WARN", message: "device services started 12s ago", domain: "device")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .warning)

        XCTAssertEqual(presentation.headline, "Checkup found warnings.")
        XCTAssertEqual(presentation.summaryRows.first, PresentationRow(label: "Pass", value: "1"))
        XCTAssertEqual(presentation.domains.map(\.domain), [.device, .thisMac])
        XCTAssertEqual(presentation.domains.map(\.title), ["Device", "This Mac"])
        XCTAssertEqual(presentation.domains.map(\.status), [.warning, .passed])
    }

    func testCheckupShowsChecksAsTheyArriveWhileRunning() throws {
        let events = [
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "check", operation: "doctor", message: "found local tool ssh", status: "PASS",
                         details: .object(["domain": .string("computer")])),
            BackendEvent(type: "check", operation: "doctor", message: "managed smbd parent process is not running",
                         status: "FAIL", details: .object(["domain": .string("device")])),
            BackendEvent(type: "check", operation: "deploy", message: "another operation's check", status: "PASS",
                         details: .object(["domain": .string("device")]))
        ]

        let running = CheckupPresentation(summary: nil, state: .running, events: events)

        XCTAssertEqual(running.domains.map(\.domain), [.device, .thisMac])
        XCTAssertEqual(running.domains.map(\.status), [.failed, .passed])
        XCTAssertEqual(running.domains.flatMap(\.rows).map(\.message),
                       ["managed smbd parent process is not running", "found local tool ssh"])
        // Not while idle: the reported checks belong to a run that finished.
        XCTAssertTrue(CheckupPresentation(summary: nil, state: .idle, events: events).domains.isEmpty)
    }

    func testCheckupGroupsTakeTheirStatusFromTheHelpersSections() throws {
        var payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "SKIP", message: "device checks skipped", domain: "device", code: "device_not_checked"),
            testDoctorCheck(status: "FAIL", message: "ssh down", domain: "computer")
        ]).decode(DoctorPayload.self)
        payload.status = .fail
        payload.sections = [
            "computer": DoctorSection(status: .fail, counts: ["FAIL": 1]),
            "device": DoctorSection(status: .notChecked, counts: [:])
        ]

        let presentation = CheckupPresentation(summary: DoctorSummary(payload: payload), state: .failed)

        let device = try XCTUnwrap(presentation.domains.first { $0.domain == .device })
        XCTAssertEqual(device.status, .notChecked)
        XCTAssertEqual(device.status.title, "Not checked")
        XCTAssertEqual(presentation.domains.first { $0.domain == .thisMac }?.status, .failed)
    }

    func testCheckupShowsTheDeviceAsNotCheckedWhenSSHWasSkipped() throws {
        var payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "found local tool ssh", domain: "computer")
        ]).decode(DoctorPayload.self)
        payload.status = .pass
        payload.sections = [
            "computer": DoctorSection(status: .pass, counts: ["PASS": 1]),
            "device": DoctorSection(status: .notChecked, counts: [:])
        ]

        let presentation = CheckupPresentation(summary: DoctorSummary(payload: payload), state: .passed)

        XCTAssertEqual(presentation.domains.map(\.domain), [.thisMac, .device])
        let device = try XCTUnwrap(presentation.domains.last)
        XCTAssertEqual(device.status, .notChecked)
        XCTAssertEqual(device.countSummary, "Not checked")
        XCTAssertTrue(device.rows.isEmpty)
        // Not while running: the device's checks may still come.
        XCTAssertFalse(CheckupPresentation(summary: nil, state: .running).domains.contains { $0.domain == .device })
    }

    func testCheckupPresentationLocalizesKnownDoctorCheckCodes() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "managed runtime payload directory /Volumes/dk2/.samba4 is missing from the data disk",
                domain: "device",
                code: "payload_missing_from_disk"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)

        XCTAssertEqual(presentation.domains.first?.domain, .device)
        XCTAssertEqual(
            row.message,
            "The Samba files are missing from every disk, as after an Erase in AirPort Utility. Run “Install / Update Samba” to reinstall."
        )
        XCTAssertFalse(row.message.contains("managed runtime payload directory"))
    }

    func testCheckupPresentationLocalizesPayloadMissingFromDiskCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "active smb.conf xattr_tdb:file parent is missing",
                domain: "device",
                code: "payload_missing_from_disk"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)

        XCTAssertEqual(presentation.domains.first?.domain, .device)
        XCTAssertEqual(
            row.message,
            "The Samba files are missing from every disk, as after an Erase in AirPort Utility. Run “Install / Update Samba” to reinstall."
        )
        XCTAssertFalse(row.message.contains("xattr_tdb"))
    }

    func testCheckupPresentationLocalizesPayloadDamagedOnDiskCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "the Samba files on the disk are damaged and no longer match what was installed; run \"Install / Update Samba\" in the macOS app, or tcapsule deploy from the command line",
                domain: "device",
                code: "payload_damaged_on_disk"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)

        XCTAssertEqual(presentation.domains.first?.domain, .device)
        XCTAssertEqual(
            row.message,
            "The Samba files on the disk are damaged and no longer match what was installed. Run “Install / Update Samba” to reinstall."
        )
        XCTAssertFalse(row.message.contains("tcapsule deploy"))
    }

    func testCheckupPresentationLocalizesRsyncDamagedOnDiskWithoutCallingSambaUninstalled() throws {
        // A damaged rsync keeps only rsync off: the Checkup line says to
        // reinstall, and the dashboard does not take Samba for missing.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "the rsync file on the disk is damaged and no longer matches what was installed, so rsync is off; run \"Install / Update Samba\" in the macOS app, or tcapsule deploy from the command line",
                domain: "device",
                code: "rsync_damaged_on_disk"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)
        XCTAssertEqual(
            row.message,
            "The rsync file on the disk is damaged and no longer matches what was installed, so rsync is off. File sharing and Time Machine keep working. Run “Install / Update Samba” to reinstall."
        )
        XCTAssertEqual(summary.payloadProblem?.stopsSamba, false)
        var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        profile.runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        XCTAssertEqual(profile.runtimeState?.state, .unhealthy)
        XCTAssertEqual(profile.runtimeState?.errorCode, "rsync_damaged_on_disk")

        // A failure on this Mac is not the device's: the row still names rsync.
        let withMacFailure = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "rsync damaged", domain: "device", code: "rsync_damaged_on_disk"),
            testDoctorCheck(status: "FAIL", message: "this Mac cannot reach the device", domain: "computer")
        ]).decode(DoctorPayload.self))
        XCTAssertEqual(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: withMacFailure)?.errorCode, "rsync_damaged_on_disk")
        // Another failure on the device is one the Checkup lists.
        let withDeviceFailure = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "rsync damaged", domain: "device", code: "rsync_damaged_on_disk"),
            testDoctorCheck(status: "FAIL", message: "smbd is not running", domain: "device")
        ]).decode(DoctorPayload.self))
        XCTAssertEqual(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: withDeviceFailure)?.errorCode, "doctor_failed")
    }

    func testEveryPayloadProblemReachesItsCheckupTextAndRow() throws {
        // DoctorSummary.payloadProblems is the one list of them: each code
        // there reaches the summary, the dashboard's state and row, and the
        // Checkup line, with text of its own. Only one that stops Samba and a
        // reinstall fixes makes the device read as not installed, and only
        // those rows offer Install: files the disk could not read need a new
        // disk, which the Checkup explains.
        XCTAssertEqual(DoctorSummary.payloadProblems.map(\.stopsSamba), [true, true, true, false, false, false])
        XCTAssertEqual(DoctorSummary.payloadProblems.filter(\.replacesDisk).map(\.code),
                       [DoctorSummary.payloadUnreadableOnDiskResultCode, DoctorSummary.rsyncUnreadableOnDiskResultCode])
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let problems = DoctorSummary.payloadProblems
        XCTAssertEqual(Set(problems.map(\.code)).count, problems.count)
        XCTAssertEqual(Set(problems.map(\.rowID)).count, problems.count)
        XCTAssertEqual(Set(problems.map(\.detailKey)).count, problems.count)
        for problem in problems {
            let detail = L10n.string(problem.detailKey)
            XCTAssertNotEqual(detail, problem.detailKey, "no catalog text for \(problem.detailKey)")
            let check = testDoctorCheck(status: "FAIL", message: "the helper's English", domain: "device", code: problem.code)
            let summary = DoctorSummary(payload: try testDoctorPayload(checks: [check]).decode(DoctorPayload.self))
            XCTAssertEqual(summary.payloadProblemCode, problem.code)
            XCTAssertEqual(summary.checks.first?.localizedMessage, detail)
            var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            profile.runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
                profile: profile, skipSSH: false, state: .failed, summary: summary)
            XCTAssertEqual(profile.runtimeState?.state,
                           problem.stopsSamba && !problem.replacesDisk ? .notInstalled : .unhealthy)
            XCTAssertEqual(profile.runtimeState?.errorCode, problem.code)
            let presentation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
                profile: profile,
                passwordState: .available,
                displayStatus: .readyToInstall,
                primaryAction: .installSMB,
                hostWarning: nil
            ))
            let runtimeRow = try row(.runtime, in: presentation)
            XCTAssertEqual(runtimeRow.id, problem.rowID)
            XCTAssertEqual(runtimeRow.detail, detail)
            // A damaged rsync fails the Checkup like anything else.
            XCTAssertEqual(runtimeRow.status, .failed)
            XCTAssertEqual(runtimeRow.action, problem.replacesDisk ? .viewCheckup : .installUpdate)
        }
    }

    func testAFailingDiskOutranksThePayloadRowAndNeverOffersInstall() throws {
        // The disk's errors and the damaged smbd they caused, in one Checkup:
        // the dashboard names the disk, with its Checkup line, and sends the
        // user to the Checkup, not to Install.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English", domain: "device",
                            code: DoctorSummary.diskFailingResultCode),
            testDoctorCheck(status: "FAIL", message: "the helper's English", domain: "device",
                            code: DoctorSummary.payloadDamagedOnDiskResultCode),
        ]).decode(DoctorPayload.self))
        let detail = L10n.string("doctor.check.disk_failing")
        XCTAssertNotEqual(detail, "doctor.check.disk_failing")
        XCTAssertTrue(summary.diskFailing)
        XCTAssertEqual(summary.checks.first?.localizedMessage, detail)
        var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        profile.runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        XCTAssertEqual(profile.runtimeState?.state, .unhealthy)
        XCTAssertEqual(profile.runtimeState?.errorCode, DoctorSummary.diskFailingResultCode)
        let checkupRow = try row(.runtime, in: DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile, passwordState: .available, displayStatus: .readyToInstall,
            primaryAction: .installSMB, hostWarning: nil
        )))
        XCTAssertEqual(checkupRow.id, "runtime-disk-failing")
        XCTAssertEqual(checkupRow.detail, detail)
        XCTAssertEqual(checkupRow.status, .failed)
        XCTAssertEqual(checkupRow.action, .viewCheckup)

        // An install that ended on the disk's errors: no Checkup yet, so the
        // row offers one rather than another install.
        profile.runtimeState?.state = .installFailed
        let installRow = try row(.runtime, in: DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile, passwordState: .available, displayStatus: .readyToInstall,
            primaryAction: .installSMB, hostWarning: nil
        )))
        XCTAssertEqual(installRow.id, "runtime-disk-failing")
        XCTAssertEqual(installRow.action, .runCheckup)
    }

    func testADamagedRsyncNeverHidesAnotherFailure() throws {
        // rsync damaged and smbd down in the same Checkup: the dashboard does
        // not show the rsync row, whose text says file sharing works, but the
        // generic failure that sends the user to the Checkup's full list.
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "managed smbd parent process is not running", domain: "device"),
            testDoctorCheck(status: "FAIL", message: "the helper's English", domain: "device",
                            code: DoctorSummary.rsyncDamagedOnDiskResultCode),
        ]).decode(DoctorPayload.self))
        var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        profile.runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: summary)
        XCTAssertEqual(profile.runtimeState?.state, .unhealthy)
        XCTAssertEqual(profile.runtimeState?.errorCode, "doctor_failed")
        let presentation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile, passwordState: .available, displayStatus: .readyToInstall,
            primaryAction: .installSMB, hostWarning: nil))
        XCTAssertEqual(try row(.runtime, in: presentation).id, "runtime-unhealthy")
    }

    func testADeployThatEndsOnADamagedRsyncNamesItWithInstall() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        profile.runtimeState = DeviceRuntimeStateSnapshot(
            state: .installFailed, source: .deploy, stage: "verify_runtime_activation", payloadFamily: "netbsd4_samba4",
            verified: false, summary: "", errorCode: DoctorSummary.rsyncDamagedOnDiskResultCode,
            errorMessage: nil, recovery: nil)
        let presentation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile, passwordState: .available, displayStatus: .readyToInstall,
            primaryAction: .installSMB, hostWarning: nil))
        let runtimeRow = try row(.runtime, in: presentation)
        XCTAssertEqual(runtimeRow.id, "runtime-rsync-damaged")
        XCTAssertEqual(runtimeRow.detail, L10n.string("doctor.check.rsync_damaged_on_disk"))
        XCTAssertEqual(runtimeRow.action, .installUpdate)
    }

    func testADeployThatEndsOnFilesTheDiskCouldNotReadOffersACheckupNotInstall() throws {
        // The disk could not read Samba or rsync: installing again writes to a
        // failing disk, so the row offers the Checkup, whose text says what to do.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        for problem in DoctorSummary.payloadProblems where problem.replacesDisk {
            var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            let error = BackendErrorViewModel(operation: "deploy", code: problem.code, message: "the helper's English")
            profile.runtimeState = DeviceDashboardSnapshotMapper.failedDeploySnapshots(
                operation: ActiveOperation(operation: "deploy", profileID: profile.id, context: nil), profile: profile,
                stage: "verify_runtime_activation", payloadFamily: "netbsd4_samba4",
                error: error, failedAt: Date()).runtimeState
            let runtimeRow = try row(.runtime, in: DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
                profile: profile, passwordState: .available, displayStatus: .readyToInstall,
                primaryAction: .installSMB, hostWarning: nil)))
            XCTAssertEqual(runtimeRow.id, problem.rowID, problem.code)
            XCTAssertEqual(runtimeRow.detail, L10n.string(problem.detailKey), problem.code)
            XCTAssertEqual(runtimeRow.action, .runCheckup, problem.code)
        }
    }

    func testFilesTheDiskCouldNotReadOutrankNotInstalled() throws {
        // A Checkup that finds the runtime not installed and the disk unable
        // to read its files: Install would write to a failing disk, so the
        // device does not read as not installed, and the row offers the Checkup.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        for problem in DoctorSummary.payloadProblems where problem.replacesDisk {
            let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
                testDoctorCheck(status: "FAIL", message: "not installed", domain: "device",
                                code: DoctorSummary.runtimeNotInstalledResultCode),
                testDoctorCheck(status: "FAIL", message: "the helper's English", domain: "device", code: problem.code),
            ]).decode(DoctorPayload.self))
            XCTAssertTrue(summary.runtimeNotInstalled)
            var profile = try makeProfile(payloadFamily: "netbsd4_samba4")
            profile.runtimeState = DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
                profile: profile, skipSSH: false, state: .failed, summary: summary)
            XCTAssertEqual(profile.runtimeState?.state, .unhealthy, problem.code)
            let runtimeRow = try row(.runtime, in: DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
                profile: profile, passwordState: .available, displayStatus: .failed,
                primaryAction: .viewCheckup, hostWarning: nil)))
            XCTAssertNotEqual(runtimeRow.action, .installUpdate, problem.code)
        }
        // Without the unreadable verdict the same Checkup still offers Install.
        let notInstalled = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "not installed", domain: "device",
                            code: DoctorSummary.runtimeNotInstalledResultCode),
        ]).decode(DoctorPayload.self))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        XCTAssertEqual(DeviceDashboardSnapshotMapper.runtimeStateFromCheckup(
            profile: profile, skipSSH: false, state: .failed, summary: notInstalled)?.state, .notInstalled)
    }

    func testCheckupPresentationLocalizesMetadataMigrationInProgressCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "a metadata migration is still running (copy phase, 120000 files checked); run \"Install / Update Samba\" in the macOS app, or tcapsule deploy from the command line: it waits for the migration to finish",
                domain: "device",
                code: "metadata_migration_in_progress"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)

        XCTAssertEqual(
            row.message,
            "A metadata migration is still running on the device. “Install / Update Samba” waits for it to finish before installing."
        )
        XCTAssertFalse(row.message.contains("tcapsule deploy"))
    }

    func testCheckupPresentationLocalizesDiskNotMountingCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        // The helper's sentence is English; the row follows the app language.
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "a disk on the device will not mount, so file sharing and Time Machine are off; restart the device, unplugging it for 10 seconds if it does not come back. If the disk still does not mount, run \"Disk Repair\" in the macOS app, or tcapsule fsck from the command line; if this keeps happening, the disk may need replacing",
                domain: "device",
                code: "disk_not_mounting"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        L10n.apply(language: .english)
        let english = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertEqual(
            english.message,
            "A disk on the device will not mount, so file sharing and Time Machine are off. Restart the device; if it does not come back, unplug it for 10 seconds. If the disk still does not mount, run “Disk Repair”; if this keeps happening, the disk may need replacing."
        )

        L10n.apply(language: .german)
        let german = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertTrue(german.message.hasPrefix("Ein Datenträger auf dem Gerät lässt sich nicht einbinden"))
    }

    func testCheckupPresentationLocalizesDiskNotRespondingCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        // The helper's sentence names the stuck processes in English; the row
        // is the catalog's sentence in the app language.
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "A disk on the device has stopped responding: a disk operation has waited for minutes without finishing. Stuck: mount_hfs (pid 421) waiting on biolock for 127+ s.",
                domain: "device",
                code: "disk_not_responding"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        L10n.apply(language: .english)
        let english = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertEqual(english.message, L10n.string("doctor.check.disk_not_responding"))
        XCTAssertTrue(english.message.hasPrefix("A disk on the device has stopped responding"))
        XCTAssertFalse(english.message.contains("mount_hfs"))

        L10n.apply(language: .german)
        let german = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertTrue(german.message.hasPrefix("Eine Festplatte am Gerät reagiert nicht mehr"))
    }

    func testCheckupPresentationLocalizesClientOnUnsharedNetworkCheck() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        // The helper's text names this Mac's networks and says "run doctor";
        // the row is the app's own sentence in the app's language.
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "this Mac is on the device's internet (WAN) side (192.168.1.0/24), where the device does not share its disks; join the device's main network (10.0.1.0/24) by Wi-Fi or one of its LAN ports, then run doctor again. Bonjour and SMB were not checked from this Mac",
                domain: "computer",
                code: "client_on_unshared_network"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        L10n.apply(language: .english)
        var row = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertEqual(
            row.message,
            "This Mac is on a network where the device does not share its disks, such as its internet (WAN) side or its guest network. Join the device's main network by Wi-Fi or one of its LAN ports, then run “Checkup” again."
        )
        XCTAssertFalse(row.message.contains("doctor"))

        L10n.apply(language: .french)
        row = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows.first)
        XCTAssertTrue(row.message.hasPrefix("Ce Mac est sur un réseau où l’appareil ne partage pas ses disques"), row.message)
    }

    func testCheckupPresentationLocalizesAnAddressThatDoesNotAnswer() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "The device is not answering at 10.0.1.9: neither SSH port 22 nor AirPort ACP port 5009 is reachable (timed out).",
                domain: "computer",
                code: "device_unreachable"
            ),
            testDoctorCheck(
                status: "FAIL",
                message: "SSH is turned off on 10.0.1.9: AirPort ACP answers on port 5009, but SSH port 22 is closed.",
                domain: "computer",
                code: "ssh_disabled"
            ),
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        L10n.apply(language: .german)
        let rows = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first?.rows)
        XCTAssertEqual(rows.map(\.message), [
            "Das Gerät antwortet weder über SSH noch über AirPort ACP.",
            "SSH ist auf diesem Gerät deaktiviert.",
        ])
    }

    func testCheckupPresentationLocalizesThisMacsNsmbConfChecks() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "/etc/nsmb.conf sets protocol_vers_map=2, which turns off SMB 3 on this Mac (sudo nano /etc/nsmb.conf)",
                domain: "computer",
                code: "mac_smb3_disabled",
                extraDetails: ["path": .string("/etc/nsmb.conf")]
            ),
            testDoctorCheck(
                status: "WARN",
                message: "/etc/nsmb.conf sets protocol_vers_map=2 for the server [NAS]",
                domain: "computer",
                code: "mac_smb3_disabled_for_server",
                extraDetails: ["path": .string("/Users/me/Library/Preferences/nsmb.conf"), "section": .string("NAS")]
            ),
            testDoctorCheck(status: "FAIL", message: "helper text without its path", domain: "computer", code: "mac_smb3_disabled")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        L10n.apply(language: .english)
        let rows = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first { $0.domain == .thisMac }).rows
        XCTAssertEqual(
            rows.map(\.message),
            [
                "/etc/nsmb.conf turns off SMB 3 (protocol_vers_map), and Time Machine needs SMB 3. Delete that line, then remove the disk in Time Machine settings and add it again.",
                "/Users/me/Library/Preferences/nsmb.conf turns off SMB 3 for the server [NAS]. If that is this device, delete that line, then remove the disk in Time Machine settings and add it again.",
                // Without the file it names, the helper's own sentence stays.
                "helper text without its path"
            ]
        )

        L10n.apply(language: .german)
        let german = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first { $0.domain == .thisMac }).rows
        XCTAssertTrue(german.contains { $0.message.hasPrefix("/etc/nsmb.conf deaktiviert SMB 3 (protocol_vers_map)") })
        // Two placeholders, rendered in order in every language.
        XCTAssertTrue(german.contains {
            $0.message.hasPrefix("/Users/me/Library/Preferences/nsmb.conf deaktiviert SMB 3 für den Server [NAS].")
        })
        L10n.apply(language: .simplifiedChinese)
        let chinese = try XCTUnwrap(CheckupPresentation(summary: summary, state: .failed).domains.first { $0.domain == .thisMac }).rows
        XCTAssertTrue(chinese.contains {
            $0.message.hasPrefix("/Users/me/Library/Preferences/nsmb.conf 为服务器 [NAS] 关闭了 SMB 3。")
        })
    }

    func testInstallActionsUseDownloadBoxIconExceptReinstall() {
        XCTAssertEqual(DashboardSecondaryAction.refreshStatus.title, "Refresh Status")
        XCTAssertEqual(DashboardSecondaryAction.refreshStatus.systemImage, "arrow.clockwise")
        XCTAssertEqual(DashboardPrimaryAction.installSMB.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(DashboardSecondaryAction.installUpdate.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(CheckupUserAction.installUpdate.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(InstallUserAction.installUpdate.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(InstallUserAction.reinstall.systemImage, "arrow.clockwise")
    }

    func testOverviewHeaderShowsActualGenerationInsteadOfCoarseCompatibilityBucket() throws {
        let netbsd4 = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: try makeProfile(payloadFamily: "netbsd4_samba4", syap: "116", model: "Time Capsule", deviceGeneration: "gen1-4"),
            passwordState: .available,
            displayStatus: .unchecked,
            primaryAction: .runCheckup,
            hostWarning: nil
        ))

        XCTAssertEqual(try headerValue("Generation", in: netbsd4), "4th generation")

        let modelFallback = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: try makeProfile(syap: "", model: "AirPort Extreme 6th generation", deviceGeneration: "gen1-4"),
            passwordState: .available,
            displayStatus: .unchecked,
            primaryAction: .runCheckup,
            hostWarning: nil
        ))

        XCTAssertEqual(try headerValue("Generation", in: modelFallback), "6th generation")

        let coarseFallback = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: try makeProfile(syap: "", model: "AirPort Extreme", deviceGeneration: "tc_gen4"),
            passwordState: .available,
            displayStatus: .unchecked,
            primaryAction: .runCheckup,
            hostWarning: nil
        ))

        XCTAssertEqual(try headerValue("Generation", in: coarseFallback), "4th generation")
    }

    func testOverviewHeaderLocalizesLastCheckedDateForSimplifiedChinese() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let checkedAt = try XCTUnwrap(calendar.date(from: DateComponents(
            timeZone: .current,
            year: 2026,
            month: 5,
            day: 29,
            hour: 0,
            minute: 12
        )))
        var profile = try makeProfile()
        profile.lastCheckup = DeviceCheckupSnapshot(
            checkedAt: checkedAt,
            state: .passed,
            passCount: 1,
            warnCount: 0,
            failCount: 0,
            summary: ""
        )

        let presentation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        ))

        XCTAssertEqual(presentation.header.lastChecked, "上次检查：2026年5月29日 00:12")
        XCTAssertFalse(presentation.header.lastChecked.contains("May"))
        XCTAssertFalse(presentation.header.lastChecked.contains("AM"))
    }

    func testInstallActionAvailabilityBlocksMutatingActionsWhileDeviceIsBusy() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: []))
            ], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let laneKey = OperationLaneKey.device("device-one")
        let store = DeployWorkflowStore(coordinator: coordinator, laneKey: laneKey)

        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.reinstall, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.installUpdate, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.runCheckup, store: store))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.reinstall, store: store, isDeviceBusy: true))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.installUpdate, store: store, isDeviceBusy: true))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.runCheckup, store: store, isDeviceBusy: true))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.openFinder, store: store, isDeviceBusy: true))
        // Opening the boot hook card starts nothing, so a busy device keeps it.
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.openBootHook, store: store, isDeviceBusy: true))

        _ = coordinator.run(operation: "doctor", context: nil, activeDeviceID: "device-one", laneKey: laneKey)
        try await waitUntilStoreState { store.isBusy }

        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.installUpdate, store: store))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.reinstall, store: store))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.runCheckup, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.openFinder, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.viewCheckup, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.viewDiagnostics, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.openBootHook, store: store))
        runner.finishAll()
    }

    func testSidebarContextMenuIncludesRequestedActionsAndCopyValues() throws {
        var profile = try makeProfile(host: "root@10.0.0.2")
        profile.hostname = "airport-time-capsule.local."
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        )

        let presentation = DeviceSidebarContextMenuPresentation(
            profile: profile,
            summary: summary,
            isDeviceBusy: false
        )

        XCTAssertEqual(
            presentation.navigationItems.map(\.action),
            [.openOverview, .openFinder, .runCheckup, .refreshStatus, .settings]
        )
        XCTAssertTrue(presentation.navigationItems.allSatisfy(\.isEnabled))
        XCTAssertEqual(
            presentation.clipboardItems.map(\.action),
            [.copySMBAddress, .copyHostname, .copyIPAddress]
        )
        XCTAssertEqual(presentation.clipboardValue(for: .copySMBAddress), "smb://airport-time-capsule.local")
        XCTAssertEqual(presentation.clipboardValue(for: .copyHostname), "airport-time-capsule.local")
        XCTAssertEqual(presentation.clipboardValue(for: .copyIPAddress), "10.0.0.2")
        XCTAssertEqual(presentation.destructiveItems, [
            DeviceSidebarContextMenuItem(action: .removeFromThisMac, isEnabled: true)
        ])
    }

    func testSidebarContextMenuSwitchesCheckupActionAndDisablesBusyActions() throws {
        let profile = try makeProfile()
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .checking,
            primaryAction: .viewCheckup,
            hostWarning: nil
        )

        let presentation = DeviceSidebarContextMenuPresentation(
            profile: profile,
            summary: summary,
            isDeviceBusy: true
        )

        XCTAssertTrue(presentation.navigationItems.contains(DeviceSidebarContextMenuItem(action: .viewCheckup, isEnabled: true)))
        XCTAssertFalse(presentation.navigationItems.contains { $0.action == .runCheckup })
        XCTAssertEqual(
            presentation.navigationItems.first { $0.action == .refreshStatus }?.isEnabled,
            false
        )
        XCTAssertEqual(presentation.destructiveItems, [
            DeviceSidebarContextMenuItem(action: .removeFromThisMac, isEnabled: false)
        ])
    }

    func testSidebarContextMenuDisablesRunCheckupWhenDeviceLaneIsBusyWithoutCheckingStatus() throws {
        let profile = try makeProfile()
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        )

        let presentation = DeviceSidebarContextMenuPresentation(
            profile: profile,
            summary: summary,
            isDeviceBusy: true
        )

        XCTAssertEqual(
            presentation.navigationItems.first { $0.action == .runCheckup },
            DeviceSidebarContextMenuItem(action: .runCheckup, isEnabled: false)
        )
        XCTAssertEqual(
            presentation.navigationItems.first { $0.action == .refreshStatus },
            DeviceSidebarContextMenuItem(action: .refreshStatus, isEnabled: false)
        )
        XCTAssertEqual(
            presentation.navigationItems.first { $0.action == .openFinder },
            DeviceSidebarContextMenuItem(action: .openFinder, isEnabled: true)
        )
    }

    func testSidebarCopyIPAddressCopiesTheSavedLinkLocalTarget() throws {
        // Discussion #368: the LAN address is advertised but off this Mac's
        // network; the saved target is the address that answers.
        var profile = try makeProfile(host: "root@fe80::82ea:96ff:fee6:5868%en0")
        profile.addresses = ["192.168.1.83", "169.254.205.45", "fe80::82ea:96ff:fee6:5868%en0"]
        let summary = DeviceDashboardSummary(profile: profile, passwordState: .available, displayStatus: .healthy, primaryAction: .openSMB, hostWarning: nil)

        let presentation = DeviceSidebarContextMenuPresentation(profile: profile, summary: summary, isDeviceBusy: false)

        XCTAssertEqual(presentation.clipboardValue(for: .copyIPAddress), "fe80::82ea:96ff:fee6:5868%en0")
    }

    func testSidebarContextMenuDisablesUnavailableActionsAndCopyValues() throws {
        let profile = try makeProfile(host: "airport-time-capsule.local")
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .missing,
            displayStatus: .passwordNeeded,
            primaryAction: .replacePassword,
            hostWarning: nil
        )

        let presentation = DeviceSidebarContextMenuPresentation(
            profile: profile,
            summary: summary,
            isDeviceBusy: false
        )

        XCTAssertEqual(
            presentation.navigationItems.first { $0.action == .runCheckup },
            DeviceSidebarContextMenuItem(action: .runCheckup, isEnabled: false)
        )
        XCTAssertEqual(
            presentation.clipboardItems.map(\.action),
            [.copySMBAddress, .copyHostname, .copyIPAddress]
        )
        XCTAssertEqual(
            presentation.clipboardItems.first { $0.action == .copyIPAddress }?.isEnabled,
            false
        )
        XCTAssertNil(presentation.clipboardValue(for: .copyIPAddress))
    }

    func testDoctorDomainPolicyUsesTypedDetailsDomainAndSeverity() throws {
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "runtime ok", domain: "device"),
            testDoctorCheck(status: "FAIL", message: "bonjour not seen", domain: "computer"),
            testDoctorCheck(status: "WARN", message: "unknown tag", domain: "SMB"),
            doctorCheckWithoutDomain(status: "INFO", message: "misc info")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let signals = DoctorCheckDomainPolicy.signals(from: summary.checks)

        // "computer" is this Mac; the device's checks, and anything untagged
        // or tagged otherwise, are the device's.
        XCTAssertEqual(signals.map(\.domain), [.thisMac, .device])
        XCTAssertEqual(signals.map(\.severity), [.failed, .warning])
        XCTAssertEqual(signals[1].passCount, 1)
        XCTAssertEqual(signals[1].warnCount, 1)
        XCTAssertEqual(signals[1].infoCount, 1)
        XCTAssertEqual(signals[0].failCount, 1)

        let spacedSummary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: " warn ", message: "nsmb warning", domain: " Computer ")
        ]).decode(DoctorPayload.self))
        let spacedSignals = DoctorCheckDomainPolicy.signals(from: spacedSummary.checks)
        XCTAssertEqual(spacedSignals.map(\.domain), [.thisMac])
        XCTAssertEqual(spacedSignals.first?.warnCount, 1)
        XCTAssertEqual(spacedSignals.first?.severity, .warning)
        XCTAssertEqual(CheckupStatusPresentation(status: " warn "), .warning)
    }

    func testCheckupPresentationCoversStatesTimelineAndHostWarning() throws {
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "ssh ok", domain: "Device")
        ]).decode(DoctorPayload.self))
        let headlines: [DoctorWorkflowState: String] = [
            .idle: "Run a checkup to inspect this Apple AirPort Time Capsule or AirPort Extreme.",
            .running: "Checkup is running.",
            .passed: "Checkup passed.",
            .warning: "Checkup found warnings.",
            .failed: "Checkup failed.",
            .runFailed: "Checkup could not complete."
        ]

        for state in DoctorWorkflowState.allCases {
            let presentation = CheckupPresentation(summary: summary, state: state)

            XCTAssertEqual(presentation.headline, headlines[state], "Unexpected headline for \(state).")
            XCTAssertEqual(presentation.primaryAction, state == .running ? nil : .runCheckup)
        }

        let stageEvent = BackendEvent(
            type: "stage",
            operation: "doctor",
            stage: "run_checks",
            risk: "local_read",
            cancellable: true,
            description: "checking"
        )
        let running = CheckupPresentation(
            summary: summary,
            state: .running,
            events: [stageEvent],
            currentStage: OperationStageState(event: stageEvent),
            hostWarning: HostCompatibilityWarning(title: "macOS Warning", message: "Known Time Machine issue.")
        )

        XCTAssertEqual(running.timeline.count, 1)
        XCTAssertEqual(running.timeline.first?.title, "Running Checkup")
        XCTAssertEqual(running.hostWarning?.message, "Known Time Machine issue.")
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
        XCTAssertEqual(connection.status, .unknown)
        XCTAssertEqual(connection.detail, "Connection status has not been refreshed.")
        XCTAssertEqual(connection.action, .refreshStatus)
    }

    func testOverviewPresentationUsesReachabilityForConnectionRow() throws {
        let profile = try makeProfile()
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .unchecked,
            primaryAction: .runCheckup,
            hostWarning: nil
        )
        let neverRefreshed = DeviceDashboardOverviewPresentation(summary: summary)

        XCTAssertEqual(try row(.connection, in: neverRefreshed).status, .unknown)
        XCTAssertEqual(try row(.connection, in: neverRefreshed).detail, "Connection status has not been refreshed.")
        XCTAssertEqual(try row(.connection, in: neverRefreshed).action, .refreshStatus)

        let missingPassword = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .missing,
            displayStatus: .passwordNeeded,
            primaryAction: .replacePassword,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.connection, in: missingPassword).status, .unknown)
        XCTAssertEqual(try row(.connection, in: missingPassword).detail, "Connection status has not been refreshed.")
        XCTAssertEqual(try row(.connection, in: missingPassword).action, .refreshStatus)

        let reachable = DeviceReachabilitySnapshot(
            refreshedAt: Date(timeIntervalSince1970: 1),
            payload: try testReachabilityPayload().decode(ReachabilityPayload.self)
        )
        let reachablePresentation = DeviceDashboardOverviewPresentation(
            summary: summary,
            reachabilitySnapshot: reachable
        )
        XCTAssertEqual(try row(.connection, in: reachablePresentation).status, .good)
        XCTAssertEqual(try row(.connection, in: reachablePresentation).detail, "SSH reachable; SMB port reachable.")
        XCTAssertEqual(try row(.connection, in: reachablePresentation).action, .refreshStatus)

        let partial = DeviceReachabilitySnapshot(
            refreshedAt: Date(timeIntervalSince1970: 2),
            payload: try testReachabilityPayload(status: "partial", summary: "SSH reachable, SMB port closed.", summaryKey: "reachability.ssh_only")
                .decode(ReachabilityPayload.self)
        )
        let partialRow = try row(.connection, in: DeviceDashboardOverviewPresentation(summary: summary, reachabilitySnapshot: partial))
        XCTAssertEqual(partialRow.status, .warning)
        XCTAssertEqual(partialRow.detail, "SSH reachable, SMB port closed.")

        let authFailed = DeviceReachabilitySnapshot(
            refreshedAt: Date(timeIntervalSince1970: 2),
            payload: try testReachabilityPayload(
                status: "partial",
                summary: "SSH authentication failed.",
                summaryKey: "reachability.auth_failed"
            )
                .decode(ReachabilityPayload.self)
        )
        let authFailedRow = try row(.connection, in: DeviceDashboardOverviewPresentation(summary: summary, reachabilitySnapshot: authFailed))
        XCTAssertEqual(authFailedRow.status, .warning)
        XCTAssertEqual(authFailedRow.detail, "The device rejected the supplied password or SSH credentials.")

        let unreachable = DeviceReachabilitySnapshot(
            refreshedAt: Date(timeIntervalSince1970: 3),
            payload: try testReachabilityPayload(status: "unreachable", summary: "Could not reach SSH or SMB.", summaryKey: "reachability.unreachable")
                .decode(ReachabilityPayload.self)
        )
        let unreachableRow = try row(.connection, in: DeviceDashboardOverviewPresentation(summary: summary, reachabilitySnapshot: unreachable))
        XCTAssertEqual(unreachableRow.status, .failed)
        XCTAssertEqual(unreachableRow.detail, "Could not reach SSH or SMB.")

        let running = DeviceDashboardOverviewPresentation(summary: summary, isReachabilityRunning: true)
        let runningRow = try row(.connection, in: running)
        XCTAssertEqual(runningRow.status, .running)
        XCTAssertEqual(runningRow.detail, "Checking DNS, SSH, and SMB reachability...")
        XCTAssertFalse(running.isEnabled(.refreshStatus))
        XCTAssertFalse(running.isPrimaryActionEnabled)
    }

    func testInstalledVersionRowFollowsTheLoginAndTheRuntimeState() throws {
        let label = L10n.string("dashboard.overview.installed_version")
        let notInstalled = L10n.string("dashboard.overview.installed_version.not_installed")
        let notChecked = L10n.string("dashboard.overview.installed_version.not_checked")
        let outdated = L10n.format("dashboard.overview.installed_version.outdated", "v4.0.0", "v5.0.0")
        let newer = L10n.string("dashboard.overview.installed_version.newer")
        let unknown = L10n.string("value.unknown")
        let expected: [(String?, [DeviceRuntimeState?: String])] = [
            ("current", [.notInstalled: notInstalled, .installedVerified: "v4.0.0", .unknown: "v4.0.0"]),
            ("outdated", [.notInstalled: notInstalled, .installedVerified: outdated, .unknown: outdated]),
            ("newer", [.notInstalled: notInstalled, .installedVerified: newer, .unknown: newer]),
            // A login that read no version: an install before versions were recorded, or another one.
            ("unknown", [.notInstalled: notInstalled, .installedVerified: unknown, .unknown: unknown]),
            ("not_checked", [.notInstalled: notInstalled, .installedVerified: notChecked, .unknown: notChecked]),
            (nil, [.notInstalled: notInstalled, .installedVerified: notChecked, .unknown: notChecked])
        ]
        for (installState, byRuntime) in expected {
            for (runtime, value) in byRuntime {
                var profile = try makeProfile()
                profile.runtimeState = runtime.map { testRuntimeState(state: $0) }
                let snapshot = try installState.map { state in
                    DeviceReachabilitySnapshot(refreshedAt: Date(timeIntervalSince1970: 1), payload: try testReachabilityPayload(
                        installState: state, installedReleaseTag: "v4.0.0", installedVersionCode: 40000, appReleaseTag: "v5.0.0"
                    ).decode(ReachabilityPayload.self))
                }
                let presentation = DeviceDashboardOverviewPresentation(summary: summary(for: profile), reachabilitySnapshot: snapshot)
                XCTAssertEqual(try headerValue(label, in: presentation), value, "\(installState ?? "no snapshot") / \(String(describing: runtime))")
            }
        }
        // Without a snapshot at all, as before the first check.
        var profile = try makeProfile()
        profile.runtimeState = testRuntimeState()
        XCTAssertEqual(try headerValue(label, in: DeviceDashboardOverviewPresentation(summary: summary(for: profile))), notChecked)
        // In English, both tags.
        XCTAssertEqual(String(format: L10n.string("dashboard.overview.installed_version.outdated", language: .english), "v4.0.0", "v5.0.0"),
                       "v4.0.0 (this app installs v5.0.0)")
    }

    func testRuntimeRowOffersInstallForAnOlderOrUnknownInstall() throws {
        for runtime in [DeviceRuntimeState.installedVerified, .installedUnverified] {
            for (installState, detailKey) in [("outdated", "dashboard.health.runtime.outdated"), ("unknown", "dashboard.health.runtime.version_unknown")] {
                let row = try runtimeRow(runtime: runtime, installState: installState)
                XCTAssertEqual(row.status, .warning, "\(runtime) \(installState)")
                XCTAssertEqual(row.action, .installUpdate)
                XCTAssertEqual(row.detail, L10n.string(detailKey))
            }
            // A current or newer install, or none checked, keeps the usual row.
            let usual = try runtimeRow(runtime: runtime, installState: nil)
            for installState in ["current", "newer", "not_checked"] {
                XCTAssertEqual(try runtimeRow(runtime: runtime, installState: installState), usual, "\(runtime) \(installState)")
            }
        }
        // Only for an install the profile knows of.
        for runtime in [DeviceRuntimeState.notInstalled, .installFailed, .unknown] {
            let usual = try runtimeRow(runtime: runtime, installState: nil)
            for installState in ["outdated", "unknown"] {
                XCTAssertEqual(try runtimeRow(runtime: runtime, installState: installState), usual, "\(runtime) \(installState)")
            }
        }
    }

    func testAnInstallStartedAfterTheCheckLeavesTheVersionNotChecked() throws {
        var profile = try makeProfile()
        profile.runtimeState = testRuntimeState()
        let snapshot = DeviceReachabilitySnapshot(refreshedAt: Date(timeIntervalSince1970: 100), payload: try testReachabilityPayload(
            installState: "outdated", installedReleaseTag: "v4.0.0", installedVersionCode: 40000, appReleaseTag: "v5.0.0"
        ).decode(ReachabilityPayload.self))
        let label = L10n.string("dashboard.overview.installed_version")

        // An install from before the check: the check read its result.
        profile.lastDeployState = testDeployState(startedAt: Date(timeIntervalSince1970: 50), updatedAt: Date(timeIntervalSince1970: 60),
                                                  finishedAt: Date(timeIntervalSince1970: 60))
        let before = DeviceDashboardOverviewPresentation(summary: summary(for: profile), reachabilitySnapshot: snapshot)
        XCTAssertEqual(try headerValue(label, in: before), L10n.format("dashboard.overview.installed_version.outdated", "v4.0.0", "v5.0.0"))
        XCTAssertEqual(try row(.runtime, in: before).action, .installUpdate)

        // Install / Update started after it, finished or not: the version may have changed.
        for finishedAt in [Date(timeIntervalSince1970: 300), nil] {
            profile.lastDeployState = testDeployState(startedAt: Date(timeIntervalSince1970: 150), updatedAt: Date(timeIntervalSince1970: 300),
                                                      finishedAt: finishedAt)
            let after = DeviceDashboardOverviewPresentation(summary: summary(for: profile), reachabilitySnapshot: snapshot)
            XCTAssertEqual(try headerValue(label, in: after), L10n.string("dashboard.overview.installed_version.not_checked"))
            XCTAssertNotEqual(try row(.runtime, in: after).action, .installUpdate)
            // The connection row still reads the same check.
            XCTAssertEqual(try row(.connection, in: after), try row(.connection, in: before))
        }

        // A check that was running when the install started read the old
        // version, although its result arrived afterwards.
        let overlapping = DeviceReachabilitySnapshot(refreshedAt: Date(timeIntervalSince1970: 140), payload: snapshot.payload)
        profile.lastDeployState = testDeployState(startedAt: Date(timeIntervalSince1970: 150), updatedAt: Date(timeIntervalSince1970: 300),
                                                  finishedAt: Date(timeIntervalSince1970: 300))
        let afterOverlap = DeviceDashboardOverviewPresentation(summary: summary(for: profile), reachabilitySnapshot: overlapping)
        XCTAssertEqual(try headerValue(label, in: afterOverlap), L10n.string("dashboard.overview.installed_version.not_checked"))
        XCTAssertNotEqual(try row(.runtime, in: afterOverlap).action, .installUpdate)
    }

    func testInstalledVersionStringsInEveryLanguage() {
        let keys = [
            "dashboard.overview.installed_version",
            "dashboard.overview.installed_version.newer",
            "dashboard.overview.installed_version.not_installed",
            "dashboard.overview.installed_version.not_checked",
            "dashboard.health.runtime.outdated",
            "dashboard.health.runtime.version_unknown"
        ]
        for language in AppLanguage.allCases where language != .system {
            for key in keys {
                XCTAssertNotEqual(L10n.strings(language: language)[key], nil, "\(language) \(key)")
            }
            let outdated = String(format: L10n.string("dashboard.overview.installed_version.outdated", language: language), "v4.0.0", "v5.0.0")
            XCTAssertTrue(outdated.contains("v4.0.0") && outdated.contains("v5.0.0"), "\(language): \(outdated)")
            XCTAssertLessThan(outdated.range(of: "v4.0.0")!.lowerBound, outdated.range(of: "v5.0.0")!.lowerBound, "\(language)")
        }
    }

    private func summary(for profile: DeviceProfile) -> DeviceDashboardSummary {
        DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .runCheckup,
            hostWarning: nil
        )
    }

    private func runtimeRow(runtime: DeviceRuntimeState, installState: String?) throws -> DashboardHealthRow {
        var profile = try makeProfile()
        profile.runtimeState = testRuntimeState(state: runtime)
        let snapshot = try installState.map { state in
            DeviceReachabilitySnapshot(refreshedAt: Date(timeIntervalSince1970: 1), payload: try testReachabilityPayload(
                installState: state, installedReleaseTag: "v4.0.0", installedVersionCode: 40000, appReleaseTag: "v5.0.0"
            ).decode(ReachabilityPayload.self))
        }
        return try row(.runtime, in: DeviceDashboardOverviewPresentation(summary: summary(for: profile), reachabilitySnapshot: snapshot))
    }

    func testOverviewShowsTheDevicesOwnHealthAndCheckupSectionsUnderCheckup() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            startedAt: Date(timeIntervalSince1970: 100),
            updatedAt: Date(timeIntervalSince1970: 100),
            finishedAt: Date(timeIntervalSince1970: 100)
        )
        profile.runtimeState = testRuntimeState()
        // A campus network hid the device from this Mac; the device passed.
        profile.lastCheckup = DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 200),
            state: .failed,
            passCount: 40,
            warnCount: 0,
            failCount: 2,
            summary: "",
            device: DoctorSection(status: .pass, counts: ["PASS": 30, "WARN": 0, "FAIL": 0]),
            thisMac: DoctorSection(status: .fail, counts: ["PASS": 10, "WARN": 0, "FAIL": 2])
        )
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        )

        let presentation = DeviceDashboardOverviewPresentation(summary: summary)

        XCTAssertEqual(presentation.healthSections.map(\.domain), [.connection, .runtime, .checkup])
        XCTAssertEqual(presentation.healthSections[1].title, "Device")
        // The Device row is the device's own health: green, with Finder.
        let device = try row(.runtime, in: presentation)
        XCTAssertEqual(device.id, "runtime-installed")
        XCTAssertEqual(device.status, .good)
        XCTAssertEqual(device.action, .openFinder)
        // Checkup: the overall result, then this Mac's section; the device's is
        // the Device row above, not repeated here.
        let checkup = try XCTUnwrap(presentation.healthSections.first { $0.domain == .checkup }).rows
        XCTAssertEqual(checkup.map(\.id), ["checkup-snapshot", "checkup-this-mac"])
        XCTAssertEqual(checkup.map(\.status), [.failed, .failed])
        XCTAssertEqual(checkup.map(\.title), ["Checkup", "This Mac"])
        XCTAssertEqual(checkup[1].detail, "PASS 10, WARN 0, FAIL 2")
        XCTAssertEqual(checkup[1].action, .viewCheckup)
    }

    func testOverviewOmitsSectionRowsForASnapshotSavedBeforeSections() throws {
        var profile = try makeProfile()
        profile.lastCheckup = DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 200), state: .passed, passCount: 5, warnCount: 0, failCount: 0, summary: ""
        )
        let presentation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile, passwordState: .available, displayStatus: .healthy, primaryAction: .openSMB, hostWarning: nil
        ))
        XCTAssertEqual(presentation.healthSections.first { $0.domain == .checkup }?.rows.map(\.id), ["checkup-snapshot"])
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
        healthyProfile.lastDeployState = testDeployState()
        healthyProfile.runtimeState = testRuntimeState()
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
        netbsd4Profile.lastDeployState = healthyProfile.lastDeployState
        netbsd4Profile.runtimeState = testRuntimeState(state: .activationNeeded, source: .doctor, payloadFamily: "netbsd4_samba4", verified: false)
        let activation = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: netbsd4Profile,
            passwordState: .available,
            displayStatus: .activationNeeded,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.runtime, in: activation).status, .warning)
        XCTAssertEqual(try row(.runtime, in: activation).action, .startSMB)

        // Checkup found the installed files gone from every disk: say so, and
        // offer Install rather than the generic "not installed" text.
        var erasedProfile = netbsd4Profile
        erasedProfile.runtimeState = testRuntimeState(
            state: .notInstalled, source: .doctor, payloadFamily: "netbsd4_samba4", verified: false,
            summary: "", errorCode: DoctorSummary.payloadMissingFromDiskResultCode)
        let erased = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: erasedProfile,
            passwordState: .available,
            displayStatus: .readyToInstall,
            primaryAction: .installSMB,
            hostWarning: nil
        ))
        let erasedRow = try row(.runtime, in: erased)
        XCTAssertEqual(erasedRow.id, "runtime-payload-missing")
        XCTAssertEqual(erasedRow.status, .failed)
        XCTAssertEqual(erasedRow.action, .installUpdate)
        XCTAssertEqual(erasedRow.detail, L10n.string("doctor.check.payload_missing_from_disk"))

        // The files on the disk fail their deployed checksum: its own text,
        // the same Install action.
        var damagedProfile = netbsd4Profile
        damagedProfile.runtimeState = testRuntimeState(
            state: .notInstalled, source: .doctor, payloadFamily: "netbsd4_samba4", verified: false,
            summary: "", errorCode: DoctorSummary.payloadDamagedOnDiskResultCode)
        let damaged = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: damagedProfile,
            passwordState: .available,
            displayStatus: .readyToInstall,
            primaryAction: .installSMB,
            hostWarning: nil
        ))
        let damagedRow = try row(.runtime, in: damaged)
        XCTAssertEqual(damagedRow.id, "runtime-payload-damaged")
        XCTAssertEqual(damagedRow.status, .failed)
        XCTAssertEqual(damagedRow.action, .installUpdate)
        XCTAssertEqual(damagedRow.detail, L10n.string("doctor.check.payload_damaged_on_disk"))
        XCTAssertNotEqual(damagedRow.detail, erasedRow.detail)
        // A disk will not mount and none holds the Samba files: say restart,
        // not only how many checks failed.
        var stuckProfile = healthyProfile
        stuckProfile.runtimeState = testRuntimeState(
            state: .unhealthy, source: .doctor, verified: false,
            summary: "PASS 20, WARN 0, FAIL 1", errorCode: DoctorSummary.diskNotMountingResultCode)
        let stuck = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: stuckProfile,
            passwordState: .available,
            displayStatus: .failed,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        let stuckRow = try row(.runtime, in: stuck)
        XCTAssertEqual(stuckRow.id, "runtime-disk-not-mounting")
        XCTAssertEqual(stuckRow.status, .failed)
        XCTAssertEqual(stuckRow.action, .viewCheckup)
        XCTAssertEqual(stuckRow.detail, L10n.string("doctor.check.disk_not_mounting"))
        // A volume only in memory: say what the next restart takes.
        stuckProfile.runtimeState?.errorCode = DoctorSummary.diskVolumesInMemoryResultCode
        let inMemory = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: stuckProfile,
            passwordState: .available,
            displayStatus: .failed,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        let inMemoryRow = try row(.runtime, in: inMemory)
        XCTAssertEqual(inMemoryRow.id, "runtime-disk-volumes-in-memory")
        XCTAssertEqual(inMemoryRow.status, .failed)
        XCTAssertEqual(inMemoryRow.action, .viewCheckup)
        XCTAssertEqual(inMemoryRow.detail, L10n.string("doctor.check.disk_volumes_in_memory"))
        stuckProfile.runtimeState?.errorCode = "doctor_failed"
        let other = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: stuckProfile,
            passwordState: .available,
            displayStatus: .failed,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        XCTAssertEqual(try row(.runtime, in: other).id, "runtime-unhealthy")
        // The disk stopped responding: say unplug, and what a repeat means.
        stuckProfile.runtimeState?.errorCode = DoctorSummary.diskNotRespondingResultCode
        let stalled = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: stuckProfile,
            passwordState: .available,
            displayStatus: .failed,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        let stalledRow = try row(.runtime, in: stalled)
        XCTAssertEqual(stalledRow.id, "runtime-disk-not-responding")
        XCTAssertEqual(stalledRow.status, .failed)
        XCTAssertEqual(stalledRow.action, .viewCheckup)
        XCTAssertEqual(stalledRow.detail, L10n.string("doctor.check.disk_not_responding"))
        // Apple's ACPd is gone: say unplug, not only how many checks failed.
        stuckProfile.runtimeState?.errorCode = DoctorSummary.acpdBrokenResultCode
        let acpdGone = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: stuckProfile,
            passwordState: .available,
            displayStatus: .failed,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        let acpdRow = try row(.runtime, in: acpdGone)
        XCTAssertEqual(acpdRow.id, "runtime-acpd-gone")
        XCTAssertEqual(acpdRow.status, .failed)
        XCTAssertEqual(acpdRow.action, .viewCheckup)
        XCTAssertEqual(acpdRow.detail, L10n.string("doctor.check.acpd_gone"))

        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")
        let hostWarning = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: healthyProfile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: warning
        ))
        // The warning is about this Mac's macOS: a row under Checkup that
        // leaves the Checkup result itself showing.
        let checkupRows = try XCTUnwrap(hostWarning.healthSections.first { $0.domain == .checkup }).rows
        XCTAssertEqual(checkupRows.first?.id, "checkup-snapshot")
        let macRow = try XCTUnwrap(checkupRows.last)
        XCTAssertEqual(macRow.id, "checkup-host-warning")
        XCTAssertEqual(macRow.title, "macOS Warning")
        XCTAssertEqual(macRow.status, .warning)
        XCTAssertEqual(macRow.detail, "Time Machine warning.")
        XCTAssertFalse(try XCTUnwrap(healthy.healthSections.first { $0.domain == .checkup }).rows.contains { $0.id == "checkup-host-warning" })
    }

    func testOverviewActionsUseFinderLabelAndSuppressRunCheckupWhileChecking() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState()
        profile.runtimeState = testRuntimeState()

        let healthy = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        ))
        XCTAssertEqual(DashboardPrimaryAction.openSMB.title, "Open Finder")
        XCTAssertEqual(healthy.primaryAction, .openSMB)
        XCTAssertEqual(healthy.secondaryActions, [.runCheckup, .settings])

        let checking = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .checking,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        XCTAssertEqual(checking.primaryAction, .viewCheckup)
        XCTAssertEqual(checking.secondaryActions, [.openFinder, .settings])
        XCTAssertFalse(checking.secondaryActions.contains(.runCheckup))
        let checkingRow = try row(.checkup, in: checking)
        XCTAssertEqual(checkingRow.status, .running)
        XCTAssertEqual(checkingRow.detail, "Checkup is running.")
        XCTAssertEqual(checkingRow.action, .viewCheckup)

        let warning = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .warning,
            primaryAction: .viewCheckup,
            hostWarning: nil
        ))
        XCTAssertEqual(warning.secondaryActions, [.runCheckup, .openFinder, .settings])
    }

    func testOverviewDisablesMutatingActionsWhileOperationIsActive() throws {
        let profile = try makeProfile()
        let installing = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .installing,
            primaryAction: .installSMB,
            hostWarning: nil
        ))

        XCTAssertFalse(installing.isPrimaryActionEnabled)
        XCTAssertEqual(installing.secondaryActions, [.runCheckup, .settings])
        XCTAssertFalse(installing.isEnabled(.runCheckup))
        XCTAssertFalse(installing.isEnabled(.installUpdate))
        XCTAssertTrue(installing.isEnabled(.settings))

        let checkup = try row(.checkup, in: installing)
        XCTAssertEqual(checkup.action, .runCheckup)
        XCTAssertFalse(installing.isEnabled(try XCTUnwrap(checkup.action)))
    }

    func testInstallWorkflowPresentationRestoresPersistedDeployFailure() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            status: .failed,
            startedAt: Date(timeIntervalSince1970: 300),
            updatedAt: Date(timeIntervalSince1970: 300),
            finishedAt: Date(timeIntervalSince1970: 300),
            stage: "read_mast",
            payloadFamily: "netbsd6_samba4",
            rebootRequested: nil,
            verified: nil,
            summary: "",
            errorCode: "remote_error",
            errorMessage: "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart.",
            recovery: DeviceRecoverySnapshot(
                title: "No HFS volumes found",
                message: "The device did not report a deployable HFS disk through MaSt.",
                actions: [],
                actionIDs: [],
                retryable: true,
                suggestedOperation: "deploy",
                docsAnchor: nil
            )
        )

        let presentation = InstallWorkflowPresentation(
            state: .idle,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        XCTAssertEqual(presentation.stateTitle, "Install / Update Failed")
        // The header only states the failure; the error box below names it,
        // with the saved English text as its details line.
        XCTAssertEqual(presentation.statusMessage, L10n.string("install.state.deploy_failed"))
        XCTAssertEqual(presentation.error?.recovery?.title, "No HFS volumes found")
        XCTAssertEqual(
            presentation.error?.diagnosticMessage,
            "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart."
        )
        XCTAssertNil(presentation.failureGuidance)
        XCTAssertEqual(presentation.timeline?.items.first?.title, "Find Payload Volume")
        XCTAssertEqual(presentation.timeline?.items.first?.state, .failed)
        XCTAssertNil(presentation.completion)
    }

    func testInstallWorkflowPresentationRestoresInterruptedDeployState() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            status: .interrupted,
            startedAt: Date(timeIntervalSince1970: 300),
            updatedAt: Date(timeIntervalSince1970: 310),
            finishedAt: Date(timeIntervalSince1970: 310),
            stage: "read_mast",
            payloadFamily: "netbsd6_samba4",
            rebootRequested: nil,
            verified: nil,
            summary: "",
            errorCode: "operation_interrupted"
        )

        let presentation = InstallWorkflowPresentation(
            state: .idle,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        XCTAssertEqual(presentation.stateTitle, "Install / Update Failed")
        XCTAssertEqual(presentation.statusMessage, "The Samba installation or update was interrupted before it completed.")
        XCTAssertEqual(presentation.error?.code, "operation_interrupted")
        XCTAssertEqual(presentation.timeline?.items.first?.title, "Find Payload Volume")
        XCTAssertEqual(presentation.timeline?.items.first?.state, .failed)
        XCTAssertNil(presentation.completion)
    }

    func testInstallWorkflowPresentationCoversAllDeployStates() throws {
        let profile = try makeProfile()
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let cases: [(DeployWorkflowState, DeployResultPayload?, BackendErrorViewModel?, [InstallUserAction])] = [
            (.idle, nil, nil, [.installUpdate]),
            (.deploying, nil, nil, [.installUpdate]),
            (.awaitingConfirmation, nil, nil, [.installUpdate]),
            (.deployed, result, nil, []),
            (.deployFailed, nil, error, [.installUpdate])
        ]
        XCTAssertEqual(Set(cases.map(\.0)), Set(DeployWorkflowState.allCases))

        for testCase in cases {
            let presentation = InstallWorkflowPresentation(
                state: testCase.0,
                result: testCase.1,
                error: testCase.2,
                events: [],
                currentStage: nil,
                profile: profile
            )
            XCTAssertEqual(presentation.actions, testCase.3, "Unexpected actions for \(testCase.0)")
        }
    }

    func testInstallWorkflowPresentationRestoresPostInstallViewFromSavedDeploySnapshot() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            startedAt: Date(timeIntervalSince1970: 200),
            updatedAt: Date(timeIntervalSince1970: 200),
            finishedAt: Date(timeIntervalSince1970: 200),
            stage: "verify_runtime_reboot",
            payloadFamily: "netbsd6_samba4",
            rebootRequested: false,
            verified: true,
            summary: ""
        )

        let presentation = InstallWorkflowPresentation(
            state: .idle,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        let completion = try XCTUnwrap(presentation.completion)
        XCTAssertEqual(presentation.stateTitle, "Installed / Updated")
        XCTAssertEqual(presentation.statusMessage, "Install / Update completed.")
        XCTAssertEqual(presentation.actions, [])
        XCTAssertEqual(completion.title, "Install / Update Verified")
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Reboot Requested", value: "no")))
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Message", value: "Install completed.")))
        XCTAssertEqual(completion.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
        let timeline = try XCTUnwrap(presentation.timeline)
        XCTAssertEqual(timeline.items.map(\.title), ["Done"])
        XCTAssertEqual(timeline.items.first?.detail, "Samba installation or update completed.")
        XCTAssertEqual(timeline.items.first?.state, .succeeded)
    }

    func testInstallWorkflowPresentationKeepsTimelineAfterSuccessfulDeploy() throws {
        let profile = try makeProfile()
        let result = try testDeployResultPayload(payloadFamily: "netbsd6_samba4")
            .decode(DeployResultPayload.self)
        let presentation = InstallWorkflowPresentation(
            state: .deployed,
            result: result,
            error: nil,
            events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd"),
                BackendEvent(type: "stage", operation: "deploy", stage: "verify_payload_upload"),
                BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload(payloadFamily: "netbsd6_samba4"))
            ],
            currentStage: nil,
            profile: profile
        )

        let timeline = try XCTUnwrap(presentation.timeline)
        XCTAssertEqual(timeline.items.map(\.title), [
            "Upload smbd",
            "Verify Upload",
            "Done"
        ])
        XCTAssertEqual(timeline.items.map(\.state), [.succeeded, .succeeded, .succeeded])
        XCTAssertNotNil(presentation.completion)
    }

    func testInstallWorkflowPresentationShowsViewCheckupWhenCheckupIsRunning() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            startedAt: Date(timeIntervalSince1970: 200),
            updatedAt: Date(timeIntervalSince1970: 200),
            finishedAt: Date(timeIntervalSince1970: 200),
            payloadFamily: "netbsd6_samba4",
            rebootRequested: false,
            verified: true,
            summary: "Installed from previous app session."
        )

        let presentation = InstallWorkflowPresentation(
            state: .idle,
            result: nil,
            error: nil,
            events: [],
            currentStage: nil,
            profile: profile,
            isCheckupRunning: true
        )

        XCTAssertEqual(presentation.completion?.actions, [.reinstall, .openFinder, .viewCheckup, .viewDiagnostics])
        XCTAssertFalse(presentation.completion?.actions.contains(.runCheckup) == true)
    }

    func testInstallWorkflowPresentationPrefersCurrentWorkflowOverSavedDeploySnapshot() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            startedAt: Date(timeIntervalSince1970: 200),
            updatedAt: Date(timeIntervalSince1970: 200),
            finishedAt: Date(timeIntervalSince1970: 200),
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "Installed from previous app session."
        )
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let deploying = InstallWorkflowPresentation(
            state: .deploying,
            result: nil,
            error: nil,
            events: [],
            currentStage: nil,
            profile: profile
        )
        XCTAssertEqual(deploying.stateTitle, "Installing / Updating")
        XCTAssertEqual(deploying.actions, [.installUpdate])
        XCTAssertNil(deploying.completion)

        let deployFailed = InstallWorkflowPresentation(
            state: .deployFailed,
            result: nil,
            error: error,
            events: [],
            currentStage: nil,
            profile: profile
        )
        XCTAssertEqual(deployFailed.stateTitle, "Install / Update Failed")
        XCTAssertEqual(deployFailed.actions, [.installUpdate])
        XCTAssertNotNil(deployFailed.timeline)
        XCTAssertNil(deployFailed.completion)
    }

    func testInstallCompletionPresentationShowsVerificationAndNextActions() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd4_samba4", verified: true, netbsd4: true)
            .decode(DeployResultPayload.self)

        let presentation = InstallCompletionPresentation(
            result: result,
            profile: try makeProfile(payloadFamily: "netbsd4_samba4")
        )

        XCTAssertEqual(presentation.title, "Install / Update Verified")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Verified", value: "yes")))
        XCTAssertEqual(presentation.warnings, [
            InstallCompletionWarning(
                text: "NetBSD4 devices may need Activate after a later reboot unless the boot hook is patched.",
                action: .openBootHook
            )
        ])
        XCTAssertEqual(presentation.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
        XCTAssertEqual(InstallUserAction.openBootHook.title, "Open Boot Hook")
        XCTAssertEqual(InstallUserAction.openBootHook.systemImage, "memorychip")
        XCTAssertEqual(InstallUserAction.installUpdate.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(InstallUserAction.reinstall.systemImage, "arrow.clockwise")
        XCTAssertEqual(InstallUserAction.reinstall.title, "Reinstall Samba")
    }

    func testInstallCompletionOffersTheBootHookOnlyWhereItsCardShows() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd4_samba4", verified: true, netbsd4: true)
            .decode(DeployResultPayload.self)

        // The deploy reports NetBSD 4 but the profile has not recorded it, so
        // the Maintenance tab has no boot hook card to open.
        let presentation = InstallCompletionPresentation(
            result: result,
            profile: try makeProfile(payloadFamily: "netbsd6_samba4")
        )

        XCTAssertEqual(presentation.warnings, [
            InstallCompletionWarning(
                text: "NetBSD4 devices may need Activate after a later reboot unless the boot hook is patched.",
                action: nil
            )
        ])
    }

    func testRestoredInstallCompletionOffersTheBootHookOnNetBSD4() throws {
        let netbsd4 = try makeProfile(payloadFamily: "netbsd4_samba4")
        let netbsd6 = try makeProfile(payloadFamily: "netbsd6_samba4")
        let warningText = "NetBSD4 devices may need Activate after a later reboot unless the boot hook is patched."

        // Either the saved deploy's payload family or the profile marks NetBSD 4,
        // but only a NetBSD 4 profile has a boot hook card to open.
        let fromSnapshot = InstallCompletionPresentation(
            snapshot: testDeployState(payloadFamily: "netbsd4_samba4"),
            profile: netbsd6
        )
        XCTAssertEqual(fromSnapshot.warnings, [InstallCompletionWarning(text: warningText, action: nil)])
        let fromProfile = InstallCompletionPresentation(
            snapshot: testDeployState(payloadFamily: nil),
            profile: netbsd4
        )
        XCTAssertEqual(fromProfile.warnings, [InstallCompletionWarning(text: warningText, action: .openBootHook)])

        let neither = InstallCompletionPresentation(
            snapshot: testDeployState(payloadFamily: "netbsd6_samba4"),
            profile: netbsd6
        )
        XCTAssertEqual(neither.warnings, [])
        XCTAssertEqual(neither.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
    }

    func testInstallCompletionPresentationReplacesRunCheckupWithViewCheckupWhileChecking() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd6_samba4", verified: true, netbsd4: false)
            .decode(DeployResultPayload.self)

        let presentation = InstallCompletionPresentation(
            result: result,
            profile: try makeProfile(payloadFamily: "netbsd6_samba4"),
            isCheckupRunning: true
        )

        XCTAssertEqual(presentation.warnings, [])
        XCTAssertEqual(presentation.actions, [.reinstall, .openFinder, .viewCheckup, .viewDiagnostics])
        XCTAssertEqual(InstallUserAction.viewCheckup.title, "View Checkup")
        XCTAssertEqual(InstallUserAction.viewCheckup.systemImage, "list.bullet.clipboard")
    }

    func testInstallTimelinePresentationUsesDeployEventsOnly() {
        let presentation = InstallTimelinePresentation(events: [
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload", description: "uploading")
        ], currentStage: nil)

        XCTAssertEqual(presentation.items.count, 1)
        XCTAssertEqual(presentation.items.first?.title, "Upload Payload")
    }

    func testInstallTimelinePresentationStopsRunningStageAfterDeployError() {
        let presentation = InstallTimelinePresentation(events: [
            BackendEvent(type: "stage", operation: "deploy", stage: "read_mast"),
            BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: "No deployable HFS disk was found.")
        ], currentStage: nil)

        XCTAssertEqual(presentation.items.first?.title, "Find Payload Volume")
        XCTAssertEqual(presentation.items.first?.state, .failed)
        XCTAssertEqual(presentation.items.last?.state, .failed)
        XCTAssertFalse(presentation.items.contains { $0.state == .running })
    }

    func testInstallProgressPresentationAppearsOnlyWhileDeploying() {
        let stage = OperationStageState(event: BackendEvent(
            type: "stage",
            operation: "deploy",
            stage: "upload_payload",
            description: "Uploading files."
        ))

        let deploying = InstallProgressPresentation(state: .deploying, currentStage: stage)

        XCTAssertEqual(deploying?.title, "Installing / Updating")
        XCTAssertEqual(deploying?.message, "Uploading and applying the managed Samba runtime. This can take a few minutes...")
        XCTAssertEqual(deploying?.detail, "Uploading managed SMB payload files.")
        for state in DeployWorkflowState.allCases where state != .deploying {
            XCTAssertNil(InstallProgressPresentation(state: state, currentStage: stage), "\(state) should not show a blocking progress modal.")
        }
    }

    func testInstallProgressShowsTheMigrationsPositionForItsStageOnly() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let stage = try XCTUnwrap(OperationStageState(event: BackendEvent(type: "stage", operation: "deploy", stage: "migrate_xattrs_copy")))
        let progress = BackendEvent(type: "progress", operation: "deploy", stage: "migrate_xattrs_copy", entries: 120000)

        let updated = try XCTUnwrap(stage.updated(by: progress))
        XCTAssertEqual(InstallProgressPresentation(state: .deploying, currentStage: updated)?.detail,
                       "Files checked: 120,000")
        XCTAssertNil(stage.updated(by: BackendEvent(type: "progress", operation: "deploy", stage: "migrate_xattrs_cleanup", entries: 1)))
        XCTAssertNil(stage.updated(by: BackendEvent(type: "log", operation: "deploy", stage: "migrate_xattrs_copy", message: "x")))
    }

    func testCheckupProgressPresentationAppearsOnlyWhileRunning() {
        let stage = OperationStageState(event: BackendEvent(
            type: "stage",
            operation: "doctor",
            stage: "run_checks",
            description: "Run local and remote diagnostic checks."
        ))

        let running = CheckupProgressPresentation(state: .running, currentStage: stage)

        XCTAssertEqual(running?.title, "Running Checkup")
        XCTAssertEqual(running?.message, "Running local and remote diagnostic checks.\nThis can take a few minutes...")
        XCTAssertNil(running?.detail)
        for state in DoctorWorkflowState.allCases where state != .running {
            XCTAssertNil(CheckupProgressPresentation(state: state, currentStage: stage), "\(state) should not show a blocking progress modal.")
        }
    }

    func testMaintenanceActionPolicyUsesStableWorkflowActionGroups() {
        XCTAssertEqual(MaintenanceActionPolicy.actions(for: .sshAccess), [.checkSSHAccess, .enableSSHAccess])
        XCTAssertEqual(MaintenanceActionPolicy.actions(for: .activate), [.runActivation])
        XCTAssertEqual(MaintenanceActionPolicy.actions(for: .uninstall), [.runUninstall])
        XCTAssertEqual(MaintenanceActionPolicy.actions(for: .fsck), [.findVolumes, .planFsck, .runFsck])
        XCTAssertEqual(MaintenanceUserAction.checkSSHAccess.title, "Check SSH")
        XCTAssertFalse(MaintenanceUserAction.checkSSHAccess.isCommitAction)
        XCTAssertTrue(MaintenanceUserAction.enableSSHAccess.isCommitAction)
        XCTAssertEqual(MaintenanceUserAction.runActivation.title, "Activate")
        XCTAssertTrue(MaintenanceUserAction.runActivation.isCommitAction)
    }

    func testMaintenanceStatusMessagesCoverAllStates() {
        for state in MaintenanceOperationState.allCases {
            XCTAssertFalse(state.maintenanceStatusMessage(for: .activate).isEmpty)
            XCTAssertFalse(state.maintenanceStatusMessage(for: .fsck).isEmpty)
        }

        XCTAssertEqual(MaintenanceOperationState.listReady.maintenanceStatusMessage(for: .fsck), "Choose a volume, then plan disk repair.")
    }

    func testMaintenancePresentationHidesActivationForDevicesThatDoNotNeedIt() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        let presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)

        XCTAssertEqual(
            presentation.cards.map(\.card),
            [MaintenanceCard.workflow(.sshAccess), .workflow(.uninstall), .workflow(.fsck), .workflow(.wipeDisk)]
        )
        XCTAssertEqual(presentation.cards.first?.isSelected, true)
        XCTAssertEqual(presentation.detail.workflow, .sshAccess)
        XCTAssertEqual(presentation.detail.title, "SSH Access")
    }

    func testMaintenancePresentationKeepsActivationForNetBSD4Devices() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        let presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)

        XCTAssertEqual(
            presentation.cards.map(\.card),
            [
                MaintenanceCard.workflow(.sshAccess), .bootHook, .workflow(.activate),
                .workflow(.uninstall), .workflow(.fsck), .workflow(.wipeDisk)
            ]
        )
        XCTAssertEqual(presentation.cards.filter(\.isSelected).map(\.card), [.workflow(.activate)])
        XCTAssertEqual(presentation.selectedCard, .workflow(.activate))
        XCTAssertEqual(presentation.detail.workflow, .activate)
    }

    func testMaintenancePresentationSelectsTheBootHookCard() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let flashStore = makeFlashStore()
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        flashStore.refresh(profile: profile)

        store.select(.bootHook)
        let presentation = MaintenanceDashboardPresentation(store: store, flashStore: flashStore, profile: profile)

        XCTAssertEqual(presentation.selectedCard, .bootHook)
        XCTAssertEqual(presentation.cards.filter(\.isSelected).map(\.card), [.bootHook])
        let card = try XCTUnwrap(presentation.cards.first { $0.card == .bootHook })
        XCTAssertEqual(card.title, "Persistent NetBSD4 Boot Hook")
        XCTAssertEqual(card.subtitle, "Patch the firmware so Samba starts after every reboot, or restore Apple firmware.")
        XCTAssertEqual(card.stateTitle, FlashWorkflowState.writeLocked.title)
        XCTAssertEqual(FlashPresentation(store: flashStore).risk, "Firmware write")
    }

    func testBootHookCardShowsTheFlashStoresEligibility() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let flashStore = makeFlashStore(buildPolicy: .disabled)
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        flashStore.refresh(profile: profile)

        let presentation = MaintenanceDashboardPresentation(store: store, flashStore: flashStore, profile: profile)

        let card = try XCTUnwrap(presentation.cards.first { $0.card == .bootHook })
        XCTAssertEqual(card.stateTitle, FlashWorkflowState.disabledInThisBuild.title)
    }

    func testSelectingAWorkflowLeavesTheBootHookCard() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        store.select(.bootHook)
        store.select(.workflow(.fsck))
        var presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.selectedCard, .workflow(.fsck))
        XCTAssertEqual(presentation.detail.workflow, .fsck)

        // Recovery actions and runs set the workflow directly; that also
        // leaves the boot hook, even when it names the same workflow again.
        store.select(.bootHook)
        store.selectedWorkflow = .fsck
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.selectedCard, .workflow(.fsck))
        XCTAssertEqual(presentation.cards.filter(\.isSelected).map(\.card), [.workflow(.fsck)])
    }

    func testBootHookSelectionFallsBackToAWorkflowWhereTheCardIsHidden() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        store.select(.bootHook)
        let presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)

        XCTAssertFalse(presentation.cards.contains { $0.card == .bootHook })
        XCTAssertEqual(presentation.selectedCard, .workflow(.sshAccess))
        XCTAssertEqual(presentation.cards.filter(\.isSelected).map(\.card), [.workflow(.sshAccess)])
        XCTAssertEqual(presentation.detail.workflow, .sshAccess)
    }

    func testMaintenancePresentationShowsRunActionsDisabledBeforePlanning() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        var presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .activate)
        XCTAssertEqual(presentation.detail.actions, [.runActivation])
        XCTAssertTrue(presentation.detail.isEnabled(.runActivation))

        store.selectedWorkflow = .uninstall
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.actions, [.runUninstall])
        XCTAssertTrue(presentation.detail.isEnabled(.runUninstall))

        store.selectedWorkflow = .fsck
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.actions, [.findVolumes, .planFsck, .runFsck])
        XCTAssertTrue(presentation.detail.isEnabled(.findVolumes))
        XCTAssertFalse(presentation.detail.isEnabled(.planFsck))
        XCTAssertFalse(presentation.detail.isEnabled(.runFsck))
    }

    func testMaintenancePresentationBuildsWorkflowPlansAndCompletions() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }
        var presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .activate)
        XCTAssertEqual(presentation.detail.actions, [.runActivation])
        XCTAssertTrue(presentation.detail.isEnabled(.runActivation))
        XCTAssertNil(presentation.detail.plan)
        XCTAssertEqual(presentation.detail.completion?.title, "Activation Complete")
        XCTAssertTrue(presentation.detail.completion?.rows.contains(PresentationRow(label: "Already Active", value: "yes")) == true)

        // Uninstall runs directly: it offers only its run action and never shows a plan.
        store.selectedWorkflow = .uninstall
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .uninstall)
        XCTAssertEqual(presentation.detail.actions, [.runUninstall])
        XCTAssertTrue(presentation.detail.isEnabled(.runUninstall))
        XCTAssertNil(presentation.detail.plan)

        store.refreshFsckTargets(password: "pw")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .fsck)
        XCTAssertEqual(presentation.detail.actions, [.findVolumes, .planFsck, .runFsck])
        XCTAssertTrue(presentation.detail.isEnabled(.findVolumes))
        XCTAssertTrue(presentation.detail.isEnabled(.planFsck))
        XCTAssertFalse(presentation.detail.isEnabled(.runFsck))

        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.plan?.title, "Disk Repair Plan")
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Disk repair can modify the selected volume."])
    }

    func testMaintenancePresentationKeepsTimelineAfterWorkflowCompletes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "activate", stage: "probe_runtime"),
                BackendEvent(type: "stage", operation: "activate", stage: "run_activation"),
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: false))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallResultPayload(waited: true, verified: true))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }

        var presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)
        XCTAssertEqual(presentation.detail.timeline?.items.map(\.title), [
            "Check Existing Runtime",
            "Starting SMB",
            "Done"
        ])
        XCTAssertEqual(presentation.detail.timeline?.items.map(\.state), [.succeeded, .succeeded, .succeeded])

        store.runUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .succeeded && !store.isRunning }
        store.selectedWorkflow = .activate
        presentation = MaintenanceDashboardPresentation(store: store, flashStore: makeFlashStore(), profile: profile)

        XCTAssertEqual(presentation.detail.workflow, .activate)
        XCTAssertEqual(presentation.detail.timeline?.items.map(\.title), [
            "Check Existing Runtime",
            "Starting SMB",
            "Done"
        ])
    }

    func testMaintenanceTimelineFiltersByWorkflowOperation() {
        let presentation = MaintenanceTimelinePresentation(events: [
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "uninstall", stage: "remove_payload", description: "removing")
        ], currentStage: nil, workflow: .uninstall)

        XCTAssertEqual(presentation.items.count, 1)
        XCTAssertEqual(presentation.items.first?.title, "Remove Payload")
    }

    private func doctorCheckWithoutDomain(status: String, message: String) -> JSONValue {
        .object([
            "status": .string(status),
            "message": .string(message),
            "details": .object([:])
        ])
    }

    private func makeFlashStore(buildPolicy: FlashBuildPolicy = .writesEnabled) -> FlashWorkflowStore {
        FlashWorkflowStore(backend: BackendClient(runner: StoreTestRunner(responses: [])), buildPolicy: buildPolicy)
    }

    private func makeProfile(
        id: String = "device-one",
        host: String = "10.0.0.2",
        payloadFamily: String = "netbsd6_samba4",
        syap: String = "119",
        model: String = "Time Capsule",
        deviceGeneration: String = "tc_gen4"
    ) throws -> DeviceProfile {
        DeviceProfile.make(
            id: id,
            configuredDevice: try testConfiguredDevice(
                host: host,
                syap: syap,
                model: model,
                payloadFamily: payloadFamily,
                deviceGeneration: deviceGeneration
            ),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }

    private func headerValue(
        _ label: String,
        in presentation: DeviceDashboardOverviewPresentation
    ) throws -> String {
        try XCTUnwrap(presentation.header.rows.first { $0.label == label }).value
    }

    private func row(
        _ domain: DashboardHealthDomain,
        in presentation: DeviceDashboardOverviewPresentation
    ) throws -> DashboardHealthRow {
        let section = try XCTUnwrap(presentation.healthSections.first { $0.domain == domain })
        return try XCTUnwrap(section.rows.first)
    }
}
