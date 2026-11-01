import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

func testBootHookResult(_ payload: JSONValue) -> BackendEvent {
    BackendEvent(type: "result", operation: "flash", ok: true, payload: payload)
}

/// The helper refusing a flash step after it started.
func testBootHookError(_ message: String) -> BackendEvent {
    BackendEvent(type: "error", operation: "flash", code: "validation_failed", message: message)
}

/// One successful helper reply for each step: a backup, then a plan and a
/// write when given. `failure` adds one more run that sends those events, for
/// a step that fails.
func testBootHookResponses(
    backup: JSONValue = testBootHookBackupPayload(),
    plan: JSONValue? = nil,
    write: JSONValue? = nil,
    failure: [BackendEvent]? = nil,
    file: StaticString = #filePath,
    line: UInt = #line
) -> [StoreTestRunner.Response] {
    // A plan reply decodes leniently, so a write reply in the plan's slot
    // would quietly become a plan that requests no write.
    if write != nil, plan == nil {
        XCTFail("a write runs only after a plan", file: file, line: line)
    }
    let replies = [backup, plan, write].compactMap { $0 }.map { StoreTestRunner.Response(events: [testBootHookResult($0)]) }
    return replies + (failure.map { [StoreTestRunner.Response(events: $0)] } ?? [])
}

/// The result payloads the helper itself builds, from
/// `Fixtures/summary_payloads.json` (written by
/// `tests/fixtures/summary_payloads.py` from the helper's payload functions).
private let helperResultPayloads: [String: JSONValue] = {
    guard let url = Bundle.module.url(forResource: "summary_payloads", withExtension: "json", subdirectory: "Fixtures"),
          let data = try? Data(contentsOf: url),
          case .array(let rows)? = try? JSONDecoder().decode(JSONValue.self, from: data) else {
        XCTFail("Fixtures/summary_payloads.json is missing from the test bundle")
        return [:]
    }
    var payloads: [String: JSONValue] = [:]
    for case .object(let row) in rows {
        if case .string(let name)? = row["name"],
           case .object(let event)? = row["event"],
           let payload = event["payload"] {
            payloads[name] = payload
        }
    }
    return payloads
}()

/// What a helper payload's `summary` says in these fixtures. The app shows a
/// result's `summary_key` in the user's language and the English `summary`
/// only when there is no key, so a message read from `summary` shows this.
let helperEnglishSummaryStandIn = "(the helper's English summary)"

/// The helper's payload named `name`, with `overrides` replacing its
/// top-level fields.
private func helperResultPayload(_ name: String, _ overrides: [String: JSONValue] = [:]) -> JSONValue {
    guard case .object(var payload)? = helperResultPayloads[name] else {
        XCTFail("summary_payloads.json has no result payload named \(name)")
        return .object(overrides)
    }
    payload["summary"] = .string(helperEnglishSummaryStandIn)
    payload.merge(overrides) { _, override in override }
    return .object(payload)
}

/// The fixture's payloads come from an empty backup; the real one names the
/// device it read and its banks.
func testBootHookBackupPayload(activeBank: String? = "primary", banks: [JSONValue]? = nil) -> JSONValue {
    let banks = banks ?? [
        testBootHookBank(name: "primary", backupValid: true, login: "stock"),
        testBootHookBank(name: "secondary", backupValid: true, login: "stock")
    ]
    return helperResultPayload("flash_backup", [
        "host": .string("10.0.0.2"),
        "syap": .string("119"),
        "active_bank": activeBank.map(JSONValue.string) ?? .null,
        "banks": .array(banks),
        "counts": .object(["banks": .number(Double(banks.count))])
    ])
}

/// A bank as the helper reports it; `login: nil` is a bank whose analysis failed.
/// A bank is an active candidate only if it is also a valid backup.
func testBootHookBank(
    name: String,
    backupValid: Bool,
    activeCandidate: Bool? = nil,
    login: String?
) -> JSONValue {
    .object([
        "name": .string(name),
        "device": .string(name == "primary" ? "/dev/rflash0.raw" : "/dev/rflash1.raw"),
        "size": .number(128),
        "sha256": .string("abc"),
        "backup_valid": .bool(backupValid),
        "active_candidate": .bool(activeCandidate ?? backupValid),
        "would_write": .bool(false),
        "write_decision": .string("no write"),
        "login": login.map { .object(["classification": .string($0)]) } ?? .null
    ])
}

/// A patch or restore plan. The helper sends one satisfied plan, which writes
/// nothing, for either mode.
func testBootHookPlanPayload(mode: FlashPlanMode, writeRequested: Bool, alreadySatisfied: Bool = false) -> JSONValue {
    if alreadySatisfied {
        // A satisfied plan writes nothing, whatever `writeRequested` says.
        return helperResultPayload("flash_plan_satisfied", [
            "mode": .string(mode.rawValue),
            "flash_plan": .object(["already_satisfied": .bool(true), "mode": .string(mode.rawValue)])
        ])
    }
    return helperResultPayload("flash_\(mode.rawValue)\(writeRequested ? "_write" : "")_plan")
}

/// Check Apple Firmware against 7.8.1: the active bank, or with `allBanks`
/// every candidate bank, all matching or none.
func testAppleCheckPayload(matched: Bool, allBanks: Bool = false) -> JSONValue {
    let match = testAppleFirmwareMatch(matched: matched)
    if allBanks {
        return helperResultPayload(matched ? "flash_apple_all_match_version" : "flash_apple_none_match_version", [
            "apple_firmware_match": match,
            "apple_match_status": .string(matched ? "all_candidates_match" : "no_candidates_match"),
            "apple_firmware_matches": .array(["primary", "secondary"].map { bank in
                .object(["bank": .string(bank), "match": match])
            })
        ])
    }
    let name = matched ? "flash_apple_stock_match_version" : "flash_apple_stock_mismatch_version"
    return helperResultPayload(name, ["apple_firmware_match": match])
}

private func testAppleFirmwareMatch(matched: Bool) -> JSONValue {
    .object([
        "matched": .bool(matched),
        "template_source": .string("catalog"),
        "template_product_id": .string("119"),
        "template_version": .string("7.8.1"),
        "template_sha256": .string("template-sha"),
        "inner_sha256": .string("inner-sha"),
        "inner_size": .number(123),
        "key_id": .string("key-one"),
        "inner_model": .number(119),
        "inner_version": .string("0x00070801")
    ])
}

/// Validate Apple Restore Firmware for 7.8.1, product 119; `withPayloadDetails`
/// adds what the helper reports about the payload it validated.
func testAppleRestoreFirmwarePayload(withPayloadDetails: Bool = false) -> JSONValue {
    guard withPayloadDetails else {
        return helperResultPayload("flash_restore_validated_version_product")
    }
    return helperResultPayload("flash_restore_validated_version_product", [
        "firmware_payload": .object([
            "template_source": .string("catalog"),
            "template_path": .string("/tmp/firmware.basebinary"),
            "template_product_id": .string("119"),
            "template_version": .string("7.8.1"),
            "template_sha256": .string("template-sha"),
            "payload_sha256": .string("payload-sha"),
            "payload_size": .number(456),
            "expected_prefix_sha256": .string("prefix-sha"),
            "expected_prefix_size": .number(123),
            "key_id": .string("key-one"),
            "inner_model": .number(119),
            "inner_version": .string("0x00070801"),
            "inner_payload_size": .number(123)
        ]),
        "firmware_payload_path": .string("/tmp/flash-backup/primary.download_only.basebinary")
    ])
}

/// How a validated write ended, named by the helper's payload for it.
enum BootHookWriteOutcome: String {
    case patchNeedsPowerCycle = "flash_patch_write_validated"
    case restoreRebooted = "flash_restore_write_rebooted"
    case restoreNeedsManualReboot = "flash_restore_write_manual_reboot"
    case restoredSecondaryBank = "flash_restore_secondary_write_validated"

    /// What a write of `mode` does unless told otherwise: a patch leaves the
    /// restart to the user, a restore reboots the device.
    static func usual(for mode: FlashPlanMode) -> Self {
        mode == .restore ? .restoreRebooted : .patchNeedsPowerCycle
    }
}

func testBootHookWritePayload(_ outcome: BootHookWriteOutcome) -> JSONValue {
    helperResultPayload(outcome.rawValue)
}

enum BootHookTestStep: Equatable {
    case backup
    case plan(FlashPlanMode)
    case write(FlashPlanMode)
}

/// Why a fixture step did not reach its result.
enum BootHookFixtureFailure: Error, Equatable {
    /// The store refused the step, or the helper failed it, with this message.
    case failed(BootHookTestStep, String)
    /// The step ended without an error but left no payload.
    case endedWithoutPayload(BootHookTestStep)
}

/// A boot hook store for `profile` (a NetBSD 4 device unless given) whose
/// helper is `runner`, refreshed for that device as the dashboard does.
/// `onDeviceLane` puts it on the device's flash lane, so the coordinator's
/// device lock and confirmations apply.
@MainActor
struct BootHookTestFixture<Runner: HelperRunning> {
    let runner: Runner
    let coordinator: OperationCoordinator
    let store: FlashWorkflowStore
    let profile: DeviceProfile

    init(
        runner: Runner,
        profile: DeviceProfile? = nil,
        onDeviceLane: Bool = false,
        buildPolicy: FlashBuildPolicy = .writesEnabled
    ) throws {
        self.runner = runner
        self.profile = try profile ?? testNetBSD4Profile()
        coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        store = FlashWorkflowStore(
            coordinator: coordinator,
            laneKey: onDeviceLane ? .deviceWorkflow(self.profile.id, .flash) : .app,
            buildPolicy: buildPolicy
        )
        store.refresh(profile: self.profile)
    }

    /// Presses each step's button with password "pw" and waits for its run to
    /// end. Throws when a step is refused, fails, or ends without its payload.
    func run(_ steps: BootHookTestStep...) async throws {
        for step in steps {
            let hasPayload: () -> Bool
            switch step {
            case .backup:
                store.backupAndInspect(password: "pw", profile: profile)
                hasPayload = { store.backup != nil }
            case .plan(let mode):
                store.planFlash(mode: mode, profile: profile)
                hasPayload = { store.plan != nil }
            case .write(let mode):
                store.write(mode: mode, password: "pw", profile: profile)
                hasPayload = { store.writeResult != nil }
            }
            // A refused step sets the store's error without starting. A started
            // one clears its error and payload first, and the store handles the
            // run's last event before the backend stops running. So once the
            // store is idle, both are this step's, and a failure throws at once
            // rather than waiting out the timeout for a payload.
            try await waitUntilStoreState { !store.isBusy }
            if let error = store.error {
                throw BootHookFixtureFailure.failed(step, error.diagnosticMessage ?? error.message)
            }
            guard hasPayload() else {
                throw BootHookFixtureFailure.endedWithoutPayload(step)
            }
        }
    }
}

extension BootHookTestFixture where Runner == StoreTestRunner {
    /// A helper that answers each run with the next of `responses`.
    init(
        responses: [StoreTestRunner.Response] = [],
        profile: DeviceProfile? = nil,
        buildPolicy: FlashBuildPolicy = .writesEnabled
    ) throws {
        try self.init(runner: StoreTestRunner(responses: responses), profile: profile, buildPolicy: buildPolicy)
    }

    static func afterBackup(_ backup: JSONValue = testBootHookBackupPayload()) async throws -> Self {
        let fixture = try Self(responses: testBootHookResponses(backup: backup))
        try await fixture.run(.backup)
        return fixture
    }

    /// Backs up, then plans in the mode `plan` reports (patch when it names
    /// none, as the app reads it).
    static func afterPlan(_ plan: JSONValue) async throws -> Self {
        let mode = plan.stringValue(for: "mode").flatMap(FlashPlanMode.init(rawValue:)) ?? .patch
        let fixture = try Self(responses: testBootHookResponses(plan: plan))
        try await fixture.run(.backup, .plan(mode))
        return fixture
    }

    /// Backs up, plans a write of `mode` and writes it.
    static func afterWrite(_ mode: FlashPlanMode, _ outcome: BootHookWriteOutcome? = nil) async throws -> Self {
        let fixture = try Self(responses: testBootHookResponses(
            plan: testBootHookPlanPayload(mode: mode, writeRequested: true),
            write: testBootHookWritePayload(outcome ?? .usual(for: mode))
        ))
        try await fixture.run(.backup, .plan(mode), .write(mode))
        return fixture
    }
}
