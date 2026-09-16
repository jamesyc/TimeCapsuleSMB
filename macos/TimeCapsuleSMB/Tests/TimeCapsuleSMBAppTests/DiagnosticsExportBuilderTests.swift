import XCTest
@testable import TimeCapsuleSMBApp

final class DiagnosticsExportBuilderTests: XCTestCase {
    func testExportIncludesReleaseReadinessAndDeviceContext() {
        let text = DiagnosticsExportBuilder().build(context: makeContext())

        XCTAssertTrue(text.contains("TimeCapsuleSMB Diagnostics"))
        XCTAssertTrue(text.contains("Generated: 2026-05-26T12:00:00Z"))
        XCTAssertTrue(text.contains("- Version: 2.1.4"))
        XCTAssertTrue(text.contains("- Appearance: system"))
        XCTAssertTrue(text.contains("- Default rsync: false"))
        XCTAssertTrue(text.contains("- Default Internal Share Uses Disk Root: false"))
        XCTAssertTrue(text.contains("- Default SMB Browse Compatibility: false"))
        XCTAssertTrue(text.contains("- Default mDNS Advertise AFP: false"))
        XCTAssertTrue(text.contains("- Default Allow Any SMB Protocol: false"))
        XCTAssertTrue(text.contains("- Default vfs_aio_fork: false"))
        XCTAssertTrue(text.contains("- State: Ready"))
        XCTAssertTrue(text.contains("- Helper Version: 2.1.4 (20125)"))
        XCTAssertTrue(text.contains("- Validation Counts: checks=1, fail=0, pass=1"))
        XCTAssertTrue(text.contains("- Name: Office Capsule"))
        XCTAssertTrue(text.contains("- Selection: selected device"))
        XCTAssertTrue(text.contains("- Profile Internal Share Uses Disk Root: false"))
        XCTAssertTrue(text.contains("- Profile Allow Any SMB Protocol: false"))
        XCTAssertTrue(text.contains("- Profile vfs_aio_fork: false"))
        XCTAssertTrue(text.contains("- Active device:profile-one: deploy"))
        XCTAssertTrue(text.contains("- Pending Confirmation: none"))
    }

    func testExportRedactsSecretsInSettingsEventsAndErrors() {
        var context = makeContext()
        context.eventLanes = [
            (.deviceWorkflow("profile-one", .deploy), [BackendEvent(
                type: "error",
                operation: "deploy",
                code: "failed",
                message: "deploy failed",
                payload: .object([
                    "credentials": .object(["password": .string("super-secret")]),
                    "token": .string("abc123"),
                    "host": .string("10.0.0.2")
                ]),
                debug: .object([
                    "authorization": .string("Bearer abc123"),
                    "path": .string("/tmp/log")
                ])
            )])
        ]

        let text = DiagnosticsExportBuilder().build(context: context)

        XCTAssertFalse(text.contains("super-secret"))
        XCTAssertFalse(text.contains("abc123"))
        XCTAssertTrue(text.contains("<redacted>"))
        XCTAssertTrue(text.contains("10.0.0.2"))
    }

    func testExportBoundsBackendEvents() {
        var context = makeContext()
        context.eventLanes = [(.deviceWorkflow("profile-one", .doctor), (0..<55).map {
            BackendEvent(type: "stage", operation: "doctor", stage: "stage-\($0)")
        })]

        let text = DiagnosticsExportBuilder(maxEvents: 2).build(context: context)

        XCTAssertFalse(text.contains("stage-52"))
        XCTAssertTrue(text.contains("stage-53"))
        XCTAssertTrue(text.contains("stage-54"))
    }

    func testExportKeepsLastDeployDetailsAfterDoctorEventsReplaceDeployEvents() {
        var context = makeContext()
        context.selectedProfileIsFallback = true
        context.selectedProfile?.lastDeployState = DeviceDeployStateSnapshot(
            operationID: "migration-attempt-1",
            startedAt: context.generatedAt,
            updatedAt: context.generatedAt.addingTimeInterval(601),
            finishedAt: context.generatedAt.addingTimeInterval(601),
            status: .failed,
            stage: "migrate_xattrs_copy",
            payloadFamily: nil,
            rebootRequested: false,
            verified: false,
            summary: "Migration failed",
            errorCode: "xattr_migration_failed",
            errorMessage: "Deployment failed.",
            recovery: nil,
            diagnosticText: "phase=copy elapsed_seconds=601 timed_out=false\nopendir failed path=/Volumes/dk2/problem"
        )
        context.eventLanes = [(.deviceWorkflow("profile-one", .doctor), (0..<55).map {
            BackendEvent(type: "stage", operation: "doctor", stage: "check-\($0)")
        })]

        let text = DiagnosticsExportBuilder(maxEvents: 2).build(context: context)

        XCTAssertTrue(text.contains("Last Deploy Operation ID: migration-attempt-1"))
        XCTAssertTrue(text.contains("Selection: most recent saved deployment failure"))
        XCTAssertTrue(text.contains("Last Deploy Stage: migrate_xattrs_copy"))
        XCTAssertTrue(text.contains("Last Deploy Error Code: xattr_migration_failed"))
        XCTAssertTrue(text.contains("Last Deploy Started: 2026-05-26T12:00:00Z"))
        XCTAssertTrue(text.contains("Last Deploy Finished: 2026-05-26T12:10:01Z"))
        XCTAssertTrue(text.contains("elapsed_seconds=601 timed_out=false"))
        XCTAssertTrue(text.contains("opendir failed path=/Volumes/dk2/problem"))
    }

    func testExportKeepsLatestRealFailureFromEachLaneOutsideEventLimit() {
        var context = makeContext()
        context.eventLanes = [
            (.deviceWorkflow("profile-one", .deploy), [
                BackendEvent(requestId: "deploy-request", type: "error", operation: "deploy", code: "remote_error", message: "deploy evidence")
            ]),
            (.deviceWorkflow("profile-one", .doctor), [
                BackendEvent(requestId: "prompt-request", type: "error", operation: "doctor", code: "confirmation_required", message: "confirm"),
                BackendEvent(requestId: "doctor-request", type: "result", operation: "doctor", ok: false, payload: .object(["summary": .string("checkup evidence")]))
            ]),
            (.deviceWorkflow("profile-one", .fsck), [
                BackendEvent(requestId: "cancel-request", type: "error", operation: "fsck", code: "cancelled", message: "cancelled")
            ])
        ]

        let text = DiagnosticsExportBuilder(maxEvents: 1).build(context: context)

        XCTAssertTrue(text.contains("request_id=deploy-request"))
        XCTAssertTrue(text.contains("request_id=doctor-request"))
        XCTAssertTrue(text.contains("checkup evidence"))
        XCTAssertFalse(text.contains("request_id=prompt-request"))
        XCTAssertFalse(text.contains("request_id=cancel-request"))
        XCTAssertTrue(text.contains("- Lane: device:profile-one:deploy"))
        XCTAssertFalse(text.contains("- Lane: device:profile-one:doctor"))
    }

    private func makeContext() -> DiagnosticsExportContext {
        DiagnosticsExportContext(
            generatedAt: Date(timeIntervalSince1970: 1_779_796_800),
            appVersion: "2.1.4",
            appBuild: "20125",
            applicationSupportPath: "/Users/test/Library/Application Support/TimeCapsuleSMB",
            helperPath: "",
            appSettings: .default,
            readinessState: .ready,
            readinessVersionPayload: versionPayload(),
            capabilities: CapabilitiesPayload(
                schemaVersion: 1,
                apiSchemaVersion: 1,
                helperVersion: "2.1.4",
                helperVersionCode: 20125,
                operations: ["deploy", "doctor"],
                distributionRoot: "/Applications/TimeCapsuleSMB.app/Contents/Resources/Distribution",
                artifactManifestSHA256: "abc",
                confirmationSchemaVersion: 1,
                summary: "Helper capabilities resolved."
            ),
            validation: InstallValidationPayload(
                schemaVersion: 1,
                ok: true,
                checks: [InstallCheckPayload(id: "python_modules", ok: true, message: "required Python modules import", details: nil)],
                counts: ["checks": 1, "pass": 1, "fail": 0],
                summary: "Install validation passed."
            ),
            runtimeIssues: [],
            updateState: .current,
            updatePayload: updatePayload(),
            updateError: nil,
            selectedProfile: profile(),
            selectedProfileIsFallback: false,
            activeOperations: [.device("profile-one"): ActiveOperation(operation: "deploy", profileID: "profile-one", context: nil)],
            pendingConfirmation: nil,
            eventLanes: [(.deviceWorkflow("profile-one", .doctor), [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["summary": .string("Doctor checks passed.")]))
            ])]
        )
    }

    private func versionPayload(source: String = "network") -> VersionCheckPayload {
        VersionCheckPayload(
            schemaVersion: 1,
            shouldBlock: false,
            updateAvailable: false,
            checkedURL: "https://example.invalid/version.json",
            message: "Current.",
            downloadURL: "https://example.invalid/download",
            localVersionCode: 20125,
            currentVersion: 20125,
            minSupportedVersion: 20000,
            latestTag: "v2.1.4",
            source: source,
            summary: source == "unavailable" ? "Version metadata is unavailable." : "TimeCapsuleSMB is up to date."
        )
    }

    private func updatePayload(source: String = "network") -> UpdateCheckPayload {
        UpdateCheckPayload(
            schemaVersion: 1,
            shouldBlock: false,
            updateAvailable: false,
            checkedURL: "https://example.invalid/version.json",
            message: "Current.",
            downloadURL: "https://example.invalid/download",
            localVersionCode: 20125,
            currentVersion: 20125,
            minSupportedVersion: 20000,
            latestTag: "v2.1.4",
            source: source,
            summary: source == "unavailable" ? "Version metadata is unavailable." : "TimeCapsuleSMB is up to date.",
            release: ReleaseInfoPayload(
                tag: "v2.1.4",
                name: "v2.1.4",
                publishedAt: "2026-01-01T00:00:00Z",
                notes: "- notes",
                htmlURL: "https://example.invalid/rel",
                prerelease: false,
                asset: ReleaseAssetPayload(name: "TimeCapsuleSMB.app.zip", size: 1, downloadURL: "https://example.invalid/app.zip", sha256: "ab")
            )
        )
    }

    private func profile() -> DeviceProfile {
        DeviceProfile(
            id: "profile-one",
            displayName: "Office Capsule",
            host: "root@10.0.0.2",
            bonjourName: "Office Capsule",
            bonjourFullname: "Office Capsule._airport._tcp.local.",
            hostname: "office-capsule.local.",
            addresses: ["10.0.0.2"],
            syap: "119",
            model: "TimeCapsule8,119",
            osName: "NetBSD",
            osRelease: "6.0",
            arch: "evbarm",
            elfEndianness: "little",
            payloadFamily: "netbsd6",
            deviceGeneration: "gen5",
            configPath: "/tmp/profile-one/.env",
            keychainAccount: "profile-one",
            createdAt: Date(timeIntervalSince1970: 0),
            updatedAt: Date(timeIntervalSince1970: 0),
            lastCheckup: nil,
            lastDeployState: nil,
            settings: .default,
            passwordState: .available
        )
    }
}
