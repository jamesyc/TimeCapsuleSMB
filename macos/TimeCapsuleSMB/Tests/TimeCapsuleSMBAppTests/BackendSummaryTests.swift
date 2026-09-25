import XCTest
@testable import TimeCapsuleSMBApp

final class BackendSummaryTests: XCTestCase {
    private var originalLanguage: AppLanguage = .system

    override func setUp() {
        super.setUp()
        originalLanguage = L10n.currentLanguage
    }

    override func tearDown() {
        L10n.apply(language: originalLanguage)
        super.tearDown()
    }

    func testPlaceholdersFollowArgumentPositions() {
        XCTAssertEqual(BackendSummary.placeholders(in: "No arguments."), [])
        XCTAssertEqual(BackendSummary.placeholders(in: "%d of %lld at %@"), [.int32, .int, .object])
        XCTAssertEqual(BackendSummary.placeholders(in: "%2$@ then %1$ld"), [.int, .object])
        XCTAssertEqual(BackendSummary.placeholders(in: "%1$d and again %1$d"), [.int32])
        XCTAssertEqual(BackendSummary.placeholders(in: "100%% of %d"), [.int32])
        XCTAssertEqual(BackendSummary.placeholders(in: "%#@devices@ on %@"), [.int, .object])
    }

    func testPlaceholdersRejectTemplatesThatCannotBeFormattedSafely() {
        for template in [
            "%s is a C string",
            "%f is a double",
            "trailing %",
            "%1$@ mixed with %d",
            "%2$@ skips the first position",
            "%1$d then %1$@ retypes it"
        ] {
            XCTAssertNil(BackendSummary.placeholders(in: template), template)
        }
    }

    func testFormatChecksArgumentCountAndTypes() {
        let locale = Locale(identifier: "en")
        XCTAssertEqual(BackendSummary.format("%d of %lld", arguments: [.int(1), .int(2)], locale: locale), "1 of 2")
        XCTAssertEqual(BackendSummary.format("%2$@ has %1$d", arguments: [.int(3), .string("dk2")], locale: locale), "dk2 has 3")
        XCTAssertNil(BackendSummary.format("%d of %d", arguments: [.int(1)], locale: locale))
        XCTAssertNil(BackendSummary.format("%d", arguments: [.int(1), .int(2)], locale: locale))
        XCTAssertNil(BackendSummary.format("%@", arguments: [.int(1)], locale: locale))
        XCTAssertNil(BackendSummary.format("%d", arguments: [.string("1")], locale: locale))
        XCTAssertNil(BackendSummary.format("%s", arguments: [.string("x")], locale: locale))
    }

    func testLocalizedFallsBackToTheTextWithoutAUsableKey() {
        L10n.apply(language: .simplifiedChinese)

        XCTAssertEqual(BackendSummary(key: nil, text: "As sent.").localized, "As sent.")
        XCTAssertEqual(BackendSummary(key: "backend.summary.not_a_key", text: "As sent.").localized, "As sent.")
        XCTAssertEqual(
            BackendSummary(key: "backend.summary.fsck_failed", arguments: [.string("8")], text: "As sent.").localized,
            "As sent."
        )
        XCTAssertEqual(
            BackendSummary(key: "backend.summary.fsck_failed", arguments: [.int(8)], text: "As sent.").localized,
            "fsck_hfs 以状态 8 退出；磁盘可能仍需修复。"
        )
    }

    func testBackendSummaryRequiresRepresentableArguments() {
        XCTAssertEqual(
            BackendSummary.backend(key: "hfs_volumes_found", arguments: [.number(2)], text: "t"),
            BackendSummary(key: "backend.summary.hfs_volumes_found", arguments: [.int(2)], text: "t")
        )
        for arguments: [JSONValue] in [[.bool(true)], [.null], [.number(1.5)], [.object([:])]] {
            XCTAssertEqual(BackendSummary.backend(key: "hfs_volumes_found", arguments: arguments, text: "t").key, nil)
        }
        XCTAssertEqual(BackendSummary.backend(key: "", arguments: nil, text: "t").key, nil)
        XCTAssertNil(BackendSummary(payload: .object(["other": .string("x")])))
        XCTAssertNil(BackendSummary(payload: nil))
    }

    func testSummaryRoundTripsThroughCodable() throws {
        let summary = BackendSummary(
            key: "backend.summary.flash.apple_some_match_version",
            arguments: [.int(1), .int(2), .string("7.8.1")],
            text: "1 of 2 candidate firmware banks match Apple stock firmware 7.8.1."
        )

        let decoded = try JSONDecoder().decode(BackendSummary.self, from: JSONEncoder().encode(summary))

        XCTAssertEqual(decoded, summary)
    }

    func testSavedSummaryPrefersTheStoredKeyThenLegacyEnglishText() {
        L10n.apply(language: .simplifiedChinese)
        let stored = BackendSummary(key: "backend.summary.deploy_completed", text: "Deployment completed.")

        XCTAssertEqual(BackendSummary.saved(stored, text: "anything"), stored)
        XCTAssertEqual(
            BackendSummary.saved(nil, text: " Deployment completed. ")?.localized,
            L10n.string("backend.summary.deploy_completed")
        )
        XCTAssertEqual(
            BackendSummary.saved(nil, text: "NetBSD4 activation complete. Run `activate` after a reboot.")?.localized,
            L10n.string("backend.summary.activation_completed_followup")
        )
        XCTAssertEqual(BackendSummary.saved(nil, text: "PASS 3, WARN 1, FAIL 0")?.localized, "PASS 3, WARN 1, FAIL 0")
        XCTAssertNil(BackendSummary.saved(nil, text: "  "))
    }

    func testProfileSavedBeforeSummaryKeysStillLocalizesItsDeploySummary() throws {
        let data = Data(#"""
        {"startedAt": 0, "updatedAt": 0, "finishedAt": 0, "status": "succeeded",
         "summary": "Deployment completed."}
        """#.utf8)
        let snapshot = try JSONDecoder().decode(DeviceDeployStateSnapshot.self, from: data)

        XCTAssertNil(snapshot.summaryRef)
        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(snapshot.localizedSummary, L10n.string("backend.summary.deploy_completed"))
    }
}

/// The helper's own payloads, generated by `tests/fixtures/summary_payloads.py`,
/// must resolve in every catalog and survive decoding into the app's typed
/// payloads.
final class BackendSummaryContractTests: XCTestCase {
    private struct Row: Decodable {
        let name: String
        let event: BackendEvent
    }

    private func rows() throws -> [Row] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "summary_payloads", withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode([Row].self, from: Data(contentsOf: url))
    }

    private func summary(of event: BackendEvent) -> BackendSummary? {
        if event.type == "log", let message = event.message {
            return .backend(key: event.messageKey, arguments: event.messageArgs, text: message)
        }
        return BackendSummary(payload: event.payload)
    }

    func testEveryHelperSummaryResolvesInEveryLanguage() throws {
        let rows = try rows()
        XCTAssertGreaterThan(rows.count, 60)
        for row in rows {
            let summary = try XCTUnwrap(summary(of: row.event), row.name)
            XCTAssertNotNil(summary.key, row.name)
            for language in AppLanguage.allCases where language != .system {
                let resolved = summary.resolved(language: language)
                XCTAssertNotNil(resolved, "\(row.name) in \(language.rawValue)")
                XCTAssertFalse(resolved?.isEmpty ?? true, "\(row.name) in \(language.rawValue)")
            }
        }
    }

    func testTypedPayloadsKeepTheHelperSummaryKey() throws {
        let decoders: [String: [(JSONValue) throws -> BackendSummary]] = [
            "capabilities": [{ try $0.decode(CapabilitiesPayload.self).summaryRef }],
            "validate-install": [{ try $0.decode(InstallValidationPayload.self).summaryRef }],
            "version-check": [{ try $0.decode(VersionCheckPayload.self).summaryRef }],
            "reachability": [{ try $0.decode(ReachabilityPayload.self).summaryRef }],
            "discover": [{ try $0.decode(DiscoverPayload.self).summaryRef }],
            "configure": [{ try $0.decode(ConfigurePayload.self).summaryRef }],
            "deploy": [{ try $0.decode(DeployResultPayload.self).summaryRef }],
            "doctor": [{ try $0.decode(DoctorPayload.self).summaryRef }],
            "activate": [{ try $0.decode(ActivationResultPayload.self).summaryRef }],
            "uninstall": [{ try $0.decode(MaintenanceResultPayload.self).summaryRef }],
            "set-ssh": [{ try $0.decode(SSHAccessPayload.self).summaryRef }],
            "repair-xattrs": [{ try $0.decode(RepairXattrsPayload.self).summaryRef }],
            "fsck": [
                { try $0.decode(FsckVolumeListPayload.self).summaryRef },
                { try $0.decode(FsckPlanPayload.self).summaryRef },
                { try $0.decode(FsckResultPayload.self).summaryRef }
            ],
            "flash": [
                { try $0.decode(FlashBackupPayload.self).summaryRef },
                { try $0.decode(FlashWritePayload.self).summaryRef },
                { try $0.decode(FlashPlanPayload.self).summaryRef }
            ]
        ]
        var checked = 0
        for row in try rows() where row.event.type == "result" {
            // "Operation exited." replaces the result of any operation, so its
            // payload carries only the summary and has no typed form.
            guard let candidates = decoders[row.event.operation],
                  let payload = row.event.payload,
                  BackendSummary(payload: payload)?.key != "backend.summary.operation_exited" else {
                continue
            }
            let typed = candidates.lazy.compactMap { try? $0(payload) }.first
            XCTAssertEqual(typed, BackendSummary(payload: payload), row.name)
            checked += 1
        }
        XCTAssertGreaterThan(checked, 50)
    }
}
