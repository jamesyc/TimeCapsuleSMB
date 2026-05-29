import XCTest
@testable import TimeCapsuleSMBApp

final class DiagnosticsExportBuilderTests: XCTestCase {
    func testExportIncludesReleaseReadinessAndDeviceContext() {
        let text = DiagnosticsExportBuilder().build(context: makeContext())

        XCTAssertTrue(text.contains("TimeCapsuleSMB Diagnostics"))
        XCTAssertTrue(text.contains("Generated: 2026-05-26T12:00:00Z"))
        XCTAssertTrue(text.contains("- Version: 2.1.4"))
        XCTAssertTrue(text.contains("- State: Ready"))
        XCTAssertTrue(text.contains("- Helper Version: 2.1.4 (20125)"))
        XCTAssertTrue(text.contains("- Validation Counts: checks=1, fail=0, pass=1"))
        XCTAssertTrue(text.contains("- Name: Office Capsule"))
        XCTAssertTrue(text.contains("- Active device:profile-one: deploy"))
        XCTAssertTrue(text.contains("- Pending Confirmation: none"))
    }

    func testExportRedactsSecretsInSettingsEventsAndErrors() {
        var context = makeContext()
        context.events = [
            BackendEvent(
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
            )
        ]

        let text = DiagnosticsExportBuilder().build(context: context)

        XCTAssertFalse(text.contains("super-secret"))
        XCTAssertFalse(text.contains("abc123"))
        XCTAssertTrue(text.contains("<redacted>"))
        XCTAssertTrue(text.contains("10.0.0.2"))
    }

    func testExportBoundsBackendEvents() {
        var context = makeContext()
        context.events = (0..<55).map {
            BackendEvent(type: "stage", operation: "doctor", stage: "stage-\($0)")
        }

        let text = DiagnosticsExportBuilder(maxEvents: 2).build(context: context)

        XCTAssertFalse(text.contains("stage-52"))
        XCTAssertTrue(text.contains("stage-53"))
        XCTAssertTrue(text.contains("stage-54"))
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
                summary: "helper capabilities resolved."
            ),
            validation: InstallValidationPayload(
                schemaVersion: 1,
                ok: true,
                checks: [InstallCheckPayload(id: "python_modules", ok: true, message: "required Python modules import", details: nil)],
                counts: ["checks": 1, "pass": 1, "fail": 0],
                summary: "install validation passed."
            ),
            runtimeIssues: [],
            updateState: .current,
            updatePayload: versionPayload(),
            updateError: nil,
            selectedProfile: profile(),
            activeOperations: [.device("profile-one"): ActiveOperation(operation: "deploy", profileID: "profile-one", context: nil)],
            pendingConfirmation: nil,
            events: [BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["summary": .string("doctor passed.")]))]
        )
    }

    private func versionPayload(source: String = "network") -> VersionCheckPayload {
        VersionCheckPayload(
            schemaVersion: 1,
            shouldBlock: false,
            checkedURL: "https://example.invalid/version.json",
            message: "Current.",
            downloadURL: "https://example.invalid/download",
            localVersionCode: 20125,
            currentVersion: 20125,
            minSupportedVersion: 20000,
            latestTag: "v2.1.4",
            source: source,
            summary: source == "unavailable" ? "version metadata is unavailable." : "TimeCapsuleSMB is up to date."
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
