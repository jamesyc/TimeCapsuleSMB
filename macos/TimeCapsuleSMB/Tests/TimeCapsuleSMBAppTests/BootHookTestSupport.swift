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

/// The banks of the helper's backup payload `name`.
private func helperBanks(_ name: String) -> [JSONValue] {
    guard case .object(let backup) = helperResultPayload(name), case .array(let banks)? = backup["banks"] else {
        XCTFail("\(name) has no banks")
        return []
    }
    return banks
}

/// A backup of a stock primary bank, the active one, and an older stock
/// secondary bank, as the helper reports it; `activeBank` and `banks` replace
/// its own.
func testBootHookBackupPayload(activeBank: String? = "primary", banks: [JSONValue]? = nil) -> JSONValue {
    var overrides: [String: JSONValue] = ["active_bank": activeBank.map(JSONValue.string) ?? .null]
    if let banks {
        overrides["banks"] = .array(banks)
        overrides["counts"] = .object(["banks": .number(Double(banks.count))])
    }
    return helperResultPayload("flash_backup", overrides)
}

/// A bank as the helper reports it, taken from its backups by LOGIN
/// classification: "stock", "already_patched", "unknown", or `nil` for a bank
/// it could not analyze. `name`, `backupValid` and `activeCandidate` replace
/// the bank's own, the values the tests vary; the rest stays the source bank's,
/// whose failure lists the app does not read. A bank is an active candidate
/// only if it is also a valid backup.
func testBootHookBank(
    name: String,
    backupValid: Bool,
    activeCandidate: Bool? = nil,
    login: String?
) -> JSONValue {
    let source: (backup: String, index: Int)
    switch login {
    case "stock": source = ("flash_backup", 0)
    case "already_patched": source = ("flash_backup_patched_primary", 0)
    case "unknown": source = ("flash_backup_unrecognized_banks", 0)
    case nil: source = ("flash_backup_unrecognized_banks", 1)
    default:
        XCTFail("no helper bank has LOGIN classification \(login ?? "")")
        return .null
    }
    let banks = helperBanks(source.backup)
    guard source.index < banks.count, case .object(var bank) = banks[source.index] else {
        return .null
    }
    bank["name"] = .string(name)
    bank["backup_valid"] = .bool(backupValid)
    bank["active_candidate"] = .bool(activeCandidate ?? backupValid)
    return .object(bank)
}

/// A patch or restore plan for the backup's stock primary bank, with the
/// firmware payload the helper built and saved for it.
func testBootHookPlanPayload(mode: FlashPlanMode) -> JSONValue {
    helperResultPayload("flash_\(mode.rawValue)_write_plan")
}

/// A patch plan for a primary bank that is already patched: it writes nothing.
func testBootHookSatisfiedPatchPlanPayload() -> JSONValue {
    helperResultPayload("flash_plan_satisfied")
}

/// A restore plan that rewrites the secondary (backup) bank, which is not a
/// valid backup, with Apple's newest firmware.
func testBootHookSecondaryRestorePlanPayload() -> JSONValue {
    helperResultPayload("flash_restore_secondary_write_plan")
}

/// Check Apple Firmware against 7.8.1: the active bank, or with `allBanks`
/// both candidate banks, all matching or none.
func testAppleCheckPayload(matched: Bool, allBanks: Bool = false) -> JSONValue {
    if allBanks {
        return helperResultPayload(matched ? "flash_apple_all_match_version" : "flash_apple_none_match_version")
    }
    return helperResultPayload(matched ? "flash_apple_stock_match_version" : "flash_apple_stock_mismatch_version")
}

/// Validate Apple Restore Firmware for 7.8.1, product 119, with the payload
/// the helper validated and the path it saved it to.
func testAppleRestoreFirmwarePayload() -> JSONValue {
    helperResultPayload("flash_restore_validated_version_product")
}

/// How a validated write ended, named by the helper's payload for it.
enum BootHookWriteOutcome: String {
    case notNeeded = "flash_write_not_needed"
    /// Ended after it began writing but before the write was validated.
    case unvalidated = "flash_write_completed"
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

func testBootHookWritePayload(_ outcome: BootHookWriteOutcome, _ overrides: [String: JSONValue] = [:]) -> JSONValue {
    helperResultPayload(outcome.rawValue, overrides)
}

enum BootHookTestStep: Equatable {
    case backup
    case plan(FlashPlanMode)
    case write(FlashPlanMode)
}

/// Why a fixture step did not reach its result.
enum BootHookFixtureFailure: Error, Equatable {
    /// The store refused to start the step, with this message.
    case refused(BootHookTestStep, String)
    /// The helper failed the step, with this message.
    case failed(BootHookTestStep, String)
    /// The step ended without an error but left no payload.
    case endedWithoutPayload(BootHookTestStep)
}

/// A maintenance store for a NetBSD 4 device whose helper is `runner`. `onDeviceLane` puts it on the device's lanes, so the
/// coordinator's device lock and confirmations apply.
@MainActor
struct BootHookTestFixture<Runner: HelperRunning> {
    let runner: Runner
    let coordinator: OperationCoordinator
    let maintenanceStore: MaintenanceStore
    let profile: DeviceProfile

    /// The boot hook's own store, where its steps keep their results.
    var store: BootHookStore { maintenanceStore.bootHookStore }

    var presentation: FlashPresentation { FlashPresentation(store: maintenanceStore) }

    init(
        runner: Runner,
        onDeviceLane: Bool = false
    ) throws {
        self.runner = runner
        self.profile = try testNetBSD4Profile()
        coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        maintenanceStore = MaintenanceStore(
            coordinator: coordinator,
            laneKey: onDeviceLane ? .deviceWorkflow(self.profile.id, .maintenance) : .app
        )
    }

    /// Presses each step's button with password "pw" and waits for its run to
    /// end. Throws when a step is refused, fails, or ends without its payload.
    func run(_ steps: BootHookTestStep...) async throws {
        for step in steps {
            let start: OperationStartResult
            let hasPayload: () -> Bool
            switch step {
            case .backup:
                start = maintenanceStore.backUpAndInspectFirmware(password: "pw", profile: profile)
                hasPayload = { store.backup != nil }
            case .plan(let mode):
                start = maintenanceStore.planFirmware(mode: mode, profile: profile)
                hasPayload = { store.plan != nil }
            case .write(let mode):
                start = maintenanceStore.writeFirmware(mode: mode, password: "pw", profile: profile)
                hasPayload = { store.writeResult != nil }
            }
            // A store refused while busy records nothing, so waiting would
            // find the running step's state instead of this one's.
            if case .rejected(let message) = start {
                throw BootHookFixtureFailure.refused(step, message)
            }
            // A started step clears its error and payload first, and the store
            // handles the run's last event before the backend stops running.
            // So once the store is idle, both are this step's, and a failure
            // throws at once rather than waiting out the timeout for a payload.
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
        responses: [StoreTestRunner.Response] = []
    ) throws {
        try self.init(runner: StoreTestRunner(responses: responses))
    }

    static func afterBackup(_ backup: JSONValue) async throws -> Self {
        let fixture = try Self(responses: testBootHookResponses(backup: backup))
        try await fixture.run(.backup)
        return fixture
    }

    /// Backs up, then plans in the mode `plan` reports.
    static func afterPlan(_ plan: JSONValue) async throws -> Self {
        let mode = try XCTUnwrap(plan.stringValue(for: "mode").flatMap(FlashPlanMode.init(rawValue:)), "the plan names no mode")
        let fixture = try Self(responses: testBootHookResponses(plan: plan))
        try await fixture.run(.backup, .plan(mode))
        return fixture
    }

    /// Backs up, plans a write of `mode` and writes it.
    static func afterWrite(_ mode: FlashPlanMode, _ outcome: BootHookWriteOutcome? = nil) async throws -> Self {
        let outcome = outcome ?? .usual(for: mode)
        let fixture = try Self(responses: testBootHookResponses(
            plan: outcome == .restoredSecondaryBank ? testBootHookSecondaryRestorePlanPayload() : testBootHookPlanPayload(mode: mode),
            write: testBootHookWritePayload(outcome)
        ))
        try await fixture.run(.backup, .plan(mode), .write(mode))
        return fixture
    }
}
