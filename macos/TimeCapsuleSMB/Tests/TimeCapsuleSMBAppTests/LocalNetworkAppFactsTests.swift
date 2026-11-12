import XCTest
@testable import TimeCapsuleSMBApp

/// macOS can block this copy of the app from the local network while "Local
/// Network" lists it as allowed: when other copies are on the Mac, and for a
/// few minutes after an update. The Local Network card and Checkup's block
/// line then put the matching step first.
final class LocalNetworkAppFactsTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try FileManager.default.removeItem(at: directory)
    }

    private func makeBundle(named name: String, identifier: String = "com.example.TimeCapsuleSMB") throws -> Bundle {
        let contents = directory.appendingPathComponent(name).appendingPathComponent("Contents", isDirectory: true)
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let plist = try PropertyListSerialization.data(
            fromPropertyList: ["CFBundleIdentifier": identifier, "CFBundlePackageType": "APPL"], format: .xml, options: 0
        )
        try plist.write(to: contents.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: directory.appendingPathComponent(name)))
    }

    func testOtherCopiesLeaveOutThisCopyHoweverItIsNamed() throws {
        let bundle = try makeBundle(named: "TimeCapsuleSMB.app")
        let link = directory.appendingPathComponent("Linked.app")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: bundle.bundleURL)
        let other = directory.appendingPathComponent("Old/TimeCapsuleSMB.app")
        var asked: [String] = []

        let copies = LocalNetworkAppFacts.copies(of: bundle, applicationURLs: { identifier in
            asked.append(identifier)
            return [bundle.bundleURL, link, other, other.appendingPathComponent("../TimeCapsuleSMB.app")]
        }, volumes: [], exists: { _ in true })

        XCTAssertEqual(asked, ["com.example.TimeCapsuleSMB"])
        XCTAssertEqual(copies, [other.standardizedFileURL.path])
    }

    func testDeletedCopiesAndCopiesInBackupsAreNotNamed() throws {
        // LaunchServices keeps copies that were deleted, and knows the ones in
        // Time Machine backups: the user can delete neither.
        let bundle = try makeBundle(named: "TimeCapsuleSMB.app")
        let kept = "/Users/a/Downloads/TimeCapsuleSMB.app"
        let deleted = "/Users/a/Desktop/TimeCapsuleSMB.app"
        let backups = [
            "/Volumes/.timemachine/0A1B/2026-10-01-120000.backup/2026-10-01-120000.backup/Data/Applications/TimeCapsuleSMB.app",
            "/Volumes/Backups of Mac/Backups.backupdb/Mac/Latest/Macintosh HD/Applications/TimeCapsuleSMB.app",
            "/Volumes/com.apple.TimeMachine.localsnapshots/Backups.backupdb/Mac/2026-10-01-120000/Data/Applications/TimeCapsuleSMB.app",
        ]
        var checked: [String] = []
        let copies = LocalNetworkAppFacts.copies(
            of: bundle,
            applicationURLs: { _ in ([kept, deleted] + backups).map { URL(fileURLWithPath: $0) } },
            volumes: [
                volume("/Volumes/.timemachine/0A1B/2026-10-01-120000.backup"),
                volume("/Volumes/Backups of Mac"),
                volume("/Volumes/com.apple.TimeMachine.localsnapshots"),
            ],
            exists: { path in
                checked.append(path)
                return path != deleted
            }
        )
        XCTAssertEqual(copies, [kept])
        // A backup is left out by its path alone, without waking its disk.
        XCTAssertEqual(Set(checked), [kept, deleted])
    }

    private func volume(_ mountPoint: String, readOnly: Bool = false, local: Bool = true) -> LocalNetworkAppFacts.Volume {
        LocalNetworkAppFacts.Volume(mountPoint: mountPoint, isReadOnly: readOnly, isLocal: local)
    }

    func testACopyOnAnotherVolumeIsJudgedFromTheMountTableAlone() throws {
        // Its disk may be asleep, or a network share that no longer answers,
        // and an automounted path mounts when touched: asking any of them
        // would stall the card being drawn.
        let bundle = try makeBundle(named: "TimeCapsuleSMB.app")
        let external = "/Volumes/External/Apps/TimeCapsuleSMB.app"
        let nested = "/Volumes/External/Images/Old/TimeCapsuleSMB.app"
        let share = "/Volumes/Data/TimeCapsuleSMB.app"
        let automounted = "/Network/Servers/nas/Apps/TimeCapsuleSMB.app"
        let mountedAtHome = "/Users/a/Shares/nas/TimeCapsuleSMB.app"
        // An ejected volume's path is left on the startup disk: looking there is quick.
        let ejected = "/Volumes/Ejected/TimeCapsuleSMB.app"
        var checked: [String] = []
        let copies = LocalNetworkAppFacts.copies(
            of: bundle,
            applicationURLs: { _ in
                [external, nested, share, automounted, mountedAtHome, ejected].map { URL(fileURLWithPath: $0) }
            },
            volumes: [
                volume("/", readOnly: true),
                volume("/Volumes/External"),
                volume("/Volumes/External/Images/Old", local: false),
                volume("/Volumes/Data", local: false),
                volume("/Network/Servers", local: false),
                volume("/Users/a/Shares/nas", local: false),
            ],
            exists: { path in
                checked.append(path)
                return false
            }
        )
        // A copy on a network volume is left out, even where its share is
        // mounted inside a local volume.
        XCTAssertEqual(copies, [external])
        XCTAssertEqual(checked, [ejected])
    }

    func testTheMountTableListsThisMacsStartupDisk() {
        let startup = LocalNetworkAppFacts.mountedVolumes().first { $0.mountPoint == "/" }
        XCTAssertEqual(startup?.isLocal, true)
    }

    func testACopyOnAMountedDiskImageIsEjectedNotDeleted() {
        let diskImage = "/Volumes/TimeCapsuleSMB/TimeCapsuleSMB.app"
        let externalDisk = "/Volumes/External/TimeCapsuleSMB.app"
        let downloads = "/Users/a/Downloads/TimeCapsuleSMB.app"
        let facts = LocalNetworkAppFacts(
            copies: [diskImage, downloads, externalDisk],
            volumes: [
                volume("/", readOnly: true),
                volume("/Volumes/TimeCapsuleSMB", readOnly: true),
                volume("/Volumes/External"),
            ],
            justUpdated: false
        )
        XCTAssertEqual(facts.diskImageCopies, [diskImage])
        // An external disk mounts writable, and only /Volumes holds disk
        // images: the read-only system volume holds no copy to eject.
        XCTAssertEqual(facts.otherCopies, [downloads, externalDisk])
    }

    func testOnlyAnAppHasCopies() throws {
        // A test runner's bundle has the same LaunchServices query but no copies to name.
        let bundle = try makeBundle(named: "Runner.xctest")
        let copies = LocalNetworkAppFacts.copies(of: bundle, applicationURLs: { _ in
            XCTFail("a bundle that is not an app is not looked up")
            return []
        }, volumes: [], exists: { _ in true })
        XCTAssertEqual(copies, [])
        XCTAssertEqual(
            LocalNetworkAppFacts.copies(of: try makeBundle(named: "Only.app"), applicationURLs: { _ in [] }, volumes: [], exists: { _ in true }),
            []
        )
    }

    func testACopyRunFromWhereItWasDownloadedReportsNoCopies() throws {
        // macOS runs it from a temporary path, so its own download would look
        // like another copy, and deleting that would delete the app in use.
        let bundle = try makeBundle(named: "AppTranslocation/0A1B/d/TimeCapsuleSMB.app")
        let copies = LocalNetworkAppFacts.copies(of: bundle, applicationURLs: { _ in
            XCTFail("a translocated copy is not looked up")
            return []
        }, volumes: [], exists: { _ in true })
        XCTAssertEqual(copies, [])
    }

    private func history() throws -> (AppLaunchHistory, UserDefaults) {
        let suite = "LocalNetworkAppFactsTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
        return (AppLaunchHistory(defaults: defaults), defaults)
    }

    func testANewVersionIsJustUpdatedForFiveMinutes() throws {
        let (history, _) = try history()
        let start = Date(timeIntervalSinceReferenceDate: 800_000_000)
        history.record(version: "30800", now: start)
        history.record(version: "30801", now: start.addingTimeInterval(60))

        XCTAssertTrue(history.justUpdated(now: start.addingTimeInterval(60)))
        XCTAssertTrue(history.justUpdated(now: start.addingTimeInterval(60 + 299)))
        XCTAssertFalse(history.justUpdated(now: start.addingTimeInterval(60 + 300)))
        // A clock set back is not "a few minutes after".
        XCTAssertFalse(history.justUpdated(now: start))
    }

    func testAnOlderCopyStartingIsNotAnUpdate() throws {
        // Two copies started in turn change the version at every launch.
        let (history, _) = try history()
        let start = Date(timeIntervalSinceReferenceDate: 800_000_000)
        history.record(version: "30801", now: start)
        history.record(version: "30800", now: start)
        XCTAssertFalse(history.justUpdated(now: start.addingTimeInterval(1)))
        // The newer one starting again counts once more.
        history.record(version: "30801", now: start.addingTimeInterval(10))
        XCTAssertTrue(history.justUpdated(now: start.addingTimeInterval(11)))
    }

    func testAVersionThatIsNotABuildNumberIsNeverAnUpdate() throws {
        let (history, _) = try history()
        let start = Date(timeIntervalSinceReferenceDate: 800_000_000)
        history.record(version: "30800", now: start)
        history.record(version: "development", now: start)
        XCTAssertFalse(history.justUpdated(now: start))
        history.record(version: "30801", now: start)
        XCTAssertFalse(history.justUpdated(now: start), "nothing to compare a development build with")
    }

    func testRelaunchingTheSameVersionKeepsItsFirstLaunch() throws {
        let (history, _) = try history()
        let start = Date(timeIntervalSinceReferenceDate: 800_000_000)
        history.record(version: "30800", now: start)
        history.record(version: "30801", now: start)
        history.record(version: "30801", now: start.addingTimeInterval(240))

        XCTAssertFalse(history.justUpdated(now: start.addingTimeInterval(300)))
    }

    func testTheFirstVersionEverRunIsNotAnUpdate() throws {
        let (history, _) = try history()
        let start = Date(timeIntervalSinceReferenceDate: 800_000_000)
        XCTAssertFalse(history.justUpdated(now: start))
        history.record(version: "30800", now: start)
        XCTAssertFalse(history.justUpdated(now: start.addingTimeInterval(1)))
    }

    private func localNetworkError(
        code: String = "acp_no_route",
        key: String = "local_network_permission_denied",
        block: String? = "proven"
    ) throws -> BackendErrorViewModel {
        let recovery = try recoveryValue(
            title: "Local Network access blocked",
            actions: ["One.", "Two.", "Three.", "Four."],
            suggestedOperation: "deploy",
            localizationKey: key,
            localNetworkBlock: block
        ).decode(BackendRecoveryPayload.self)
        return BackendErrorViewModel(operation: "deploy", code: code, message: "blocked", recovery: recovery)
    }

    func testTheLocalNetworkCardPutsWhatTheAppSeesFirst() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let facts = LocalNetworkAppFacts(
            otherCopies: ["/Users/a/Downloads/TimeCapsuleSMB.app"],
            diskImageCopies: ["/Volumes/TimeCapsuleSMB/TimeCapsuleSMB.app"],
            justUpdated: true
        )
        for key in ["local_network_permission_denied", "discover.local_network_permission_denied"] {
            let card = RecoveryGuidancePresentation(error: try localNetworkError(key: key), localNetworkFacts: facts)
            let plain = RecoveryGuidancePresentation(error: try localNetworkError(key: key), localNetworkFacts: .empty)
            // The copies alone: one copy after another starting looks like
            // updates too, so the update is no reason to wait.
            XCTAssertEqual(Array(card.steps.prefix(2)), [
                "Other copies of TimeCapsuleSMB are on this Mac (/Users/a/Downloads/TimeCapsuleSMB.app). Delete them, empty the Trash, then restart the Mac.",
                "TimeCapsuleSMB is also on a mounted disk image (/Volumes/TimeCapsuleSMB/TimeCapsuleSMB.app). Eject it in Finder, then restart the Mac.",
            ], key)
            XCTAssertEqual(Array(card.steps.dropFirst(2)), plain.steps, key)
            XCTAssertEqual(plain.steps.count, 4, key)
        }
    }

    func testAnUpdateWithNoOtherCopiesSaysToWait() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let facts = LocalNetworkAppFacts(otherCopies: [], diskImageCopies: [], justUpdated: true)
        let card = RecoveryGuidancePresentation(error: try localNetworkError(), localNetworkFacts: facts)
        XCTAssertEqual(card.steps.first, "TimeCapsuleSMB was updated a few minutes ago, and macOS can block a new version for a few minutes. Wait 2 minutes, then try again.")
        XCTAssertEqual(card.steps.count, 5)
        XCTAssertEqual(LocalNetworkAppFacts(otherCopies: [], diskImageCopies: [], justUpdated: false).localizedSteps, [])
    }

    func testALongListOfCopiesNamesThree() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let facts = LocalNetworkAppFacts(otherCopies: ["/a.app", "/b.app", "/c.app", "/d.app"], diskImageCopies: [], justUpdated: false)
        XCTAssertEqual(
            facts.localizedSteps,
            ["Other copies of TimeCapsuleSMB are on this Mac (/a.app, /b.app, /c.app, …). Delete them, empty the Trash, then restart the Mac."]
        )
    }

    func testABlockNothingProvedGetsTheCopiesOnlyAsAPossibility() throws {
        // Its last step asks whether AirPort Utility sees the device: a device
        // that is simply off must not read as a reason to delete copies, and
        // an update alone is no reason to suspect a block there.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let facts = LocalNetworkAppFacts(otherCopies: ["/a.app"], diskImageCopies: ["/Volumes/T/TimeCapsuleSMB.app"], justUpdated: true)
        for key in ["device_unreachable", "configure.remote_error.acp_port_probe"] {
            let error = try localNetworkError(code: "device_unreachable", key: key, block: "possible")
            let card = RecoveryGuidancePresentation(error: error, localNetworkFacts: facts)
            let plain = RecoveryGuidancePresentation(error: error, localNetworkFacts: .empty)
            XCTAssertEqual(card.steps, plain.steps + [
                "If macOS still blocks TimeCapsuleSMB, other copies of it on this Mac may be why (/a.app): delete them, empty the Trash, then restart the Mac.",
                "If macOS still blocks TimeCapsuleSMB, its copy on a mounted disk image may be why (/Volumes/T/TimeCapsuleSMB.app): eject the disk image in Finder, then restart the Mac.",
            ], key)
        }
    }

    func testTheHelpersMarkNotTheCardsKeyDecidesWhereTheStepsGo() throws {
        // A card the helper does not mark asks nothing, whatever its key.
        for key in ["auth_failed", "local_network_permission_denied", "device_unreachable"] {
            let card = RecoveryGuidancePresentation(
                error: try localNetworkError(code: "auth_failed", key: key, block: nil),
                localNetworkFacts: { XCTFail("only a marked card asks"); return .empty }()
            )
            XCTAssertEqual(card.steps.count, 4, key)
        }
        // A kind a later helper may add is ignored, and the card still decodes.
        let error = try localNetworkError(block: "elsewhere")
        XCTAssertNil(error.recovery?.localNetworkBlock)
        XCTAssertEqual(error.recovery?.localizationKey, "local_network_permission_denied")
        let card = RecoveryGuidancePresentation(
            error: error,
            localNetworkFacts: LocalNetworkAppFacts(otherCopies: ["/a.app"], diskImageCopies: [], justUpdated: false)
        )
        XCTAssertEqual(card.steps.count, 4)
    }

    func testASavedCardKeepsTheMark() throws {
        // A failed install is saved, and its card is shown again from what was saved.
        let recovery = try XCTUnwrap(try localNetworkError().recovery)
        let saved = try JSONDecoder().decode(DeviceRecoverySnapshot.self, from: JSONEncoder().encode(DeviceRecoverySnapshot(recovery)))
        XCTAssertEqual(BackendRecoveryPayload(saved).localNetworkBlock, .proven)
        // One saved before the mark existed has none.
        let old = Data(#"{"title":"t","actions":[],"actionIDs":[],"retryable":true}"#.utf8)
        XCTAssertNil(try JSONDecoder().decode(DeviceRecoverySnapshot.self, from: old).localNetworkBlock)
        // One saved by a later app, with a kind this one does not know, still loads.
        let later = Data(#"{"title":"t","actions":[],"actionIDs":[],"retryable":true,"localNetworkBlock":"elsewhere"}"#.utf8)
        XCTAssertNil(BackendRecoveryPayload(try JSONDecoder().decode(DeviceRecoverySnapshot.self, from: later)).localNetworkBlock)
    }

    func testCheckupsBlockLineEndsWithWhatTheAppSees() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .german)
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "acp_no_route"),
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "device_unreachable"),
        ]).decode(DoctorPayload.self))
        let facts = LocalNetworkAppFacts(otherCopies: ["/a.app"], diskImageCopies: [], justUpdated: false)
        let line = L10n.string("doctor.check.acp_no_route")

        XCTAssertEqual(summary.checks[0].localizedMessage(localNetworkFacts: .empty), line)
        XCTAssertEqual(
            summary.checks[0].localizedMessage(localNetworkFacts: facts),
            "\(line) Auf diesem Mac gibt es weitere Kopien von TimeCapsuleSMB (/a.app). Löschen Sie sie, leeren Sie den Papierkorb und starten Sie den Mac dann neu."
        )
        XCTAssertEqual(
            summary.checks[1].localizedMessage(localNetworkFacts: { XCTFail("only the block line asks"); return facts }()),
            L10n.string("doctor.check.device_unreachable")
        )
    }

    func testASilentDeviceLineIsTranslatedForWhereThisMacIs() throws {
        // Nothing answers: the helper says whether this Mac is on the network it
        // last reached the device on. Each line is the app's own, in its language.
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .german)
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "device_silent"),
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "device_off_network"),
        ]).decode(DoctorPayload.self))

        XCTAssertEqual(summary.checks[0].localizedMessage(localNetworkFacts: .empty), L10n.string("doctor.check.device_silent"))
        XCTAssertEqual(summary.checks[1].localizedMessage(localNetworkFacts: .empty),
                       L10n.string("doctor.check.device_off_network"))
        XCTAssertNotEqual(L10n.string("doctor.check.device_silent"), "doctor.check.device_silent")
        XCTAssertTrue(L10n.string("doctor.check.device_off_network").contains("Überprüfung"))
    }

    func testTheCheckupTabLooksUpWhatTheAppSeesOnlyForTheBlockLine() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)
        let facts = LocalNetworkAppFacts(otherCopies: ["/a.app"], diskImageCopies: [], justUpdated: false)
        let unreachable = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "device_unreachable"),
        ]).decode(DoctorPayload.self))
        let plain = CheckupPresentation(
            summary: unreachable, state: .failed,
            localNetworkFacts: { XCTFail("no block line, no lookup"); return facts }()
        )
        XCTAssertEqual(plain.domains.flatMap(\.rows).map(\.message), [L10n.string("doctor.check.device_unreachable")])

        let blocked = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "acp_no_route"),
        ]).decode(DoctorPayload.self))
        let row = try XCTUnwrap(CheckupPresentation(summary: blocked, state: .failed, localNetworkFacts: facts).domains.first?.rows.first)
        XCTAssertEqual(row.message, "\(L10n.string("doctor.check.acp_no_route")) \(facts.localizedSteps[0])")
    }

    func testChineseRunsTheLinesSentencesTogether() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)
        let summary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "FAIL", message: "the helper's English line", domain: "computer", code: "acp_no_route"),
        ]).decode(DoctorPayload.self))
        let facts = LocalNetworkAppFacts(otherCopies: ["/a.app"], diskImageCopies: [], justUpdated: false)
        XCTAssertEqual(
            summary.checks[0].localizedMessage(localNetworkFacts: facts),
            L10n.string("doctor.check.acp_no_route") + facts.localizedSteps[0]
        )
    }

    func testEnableSSHSendsThePreflightResult() {
        let preflight = LocalNetworkPreflightResult(status: .denied, detail: "PolicyDenied", durationMilliseconds: 3, serviceType: "_airport._tcp")
        let params = OperationParams.SetSSH.enable(noWait: false, localNetworkPreflight: preflight)
        XCTAssertEqual(params["action"], .string("enable"))
        XCTAssertEqual(params["macos_local_network_preflight_result"], .string("denied"))
        XCTAssertEqual(params["macos_local_network_preflight_error"], .string("PolicyDenied"))
        XCTAssertNil(OperationParams.SetSSH.enable(noWait: false)["macos_local_network_preflight_result"])
    }
}
