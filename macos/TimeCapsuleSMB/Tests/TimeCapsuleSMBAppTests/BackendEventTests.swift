import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class BackendEventTests: XCTestCase {
    func testBackendEventDecodesContractFields() throws {
        let data = """
        {"schema_version":1,"request_id":"req-1","type":"error","operation":"deploy","code":"remote_error","message":"failed","debug":{"stderr":"detail"},"recovery":{"title":"No HFS volumes found","retryable":true,"actions":["retry"]}}
        """.data(using: .utf8)!

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.schemaVersion, 1)
        XCTAssertEqual(event.requestId, "req-1")
        XCTAssertEqual(event.type, "error")
        XCTAssertEqual(event.operation, "deploy")
        XCTAssertEqual(event.code, "remote_error")
        XCTAssertEqual(event.message, "failed")
        XCTAssertEqual(event.debug, .object(["stderr": .string("detail")]))
        XCTAssertEqual(event.recovery, .object([
            "title": .string("No HFS volumes found"),
            "retryable": .bool(true),
            "actions": .array([.string("retry")])
        ]))
    }

    func testBackendEventDecodesStagePolicyFields() throws {
        let data = """
        {"schema_version":1,"type":"stage","operation":"deploy","stage":"upload_payload","risk":"remote_write","cancellable":false,"description":"Upload managed Samba payload files."}
        """.data(using: .utf8)!

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.stage, "upload_payload")
        XCTAssertEqual(event.risk, "remote_write")
        XCTAssertEqual(event.cancellable, false)
        XCTAssertEqual(event.description, "Upload managed Samba payload files.")
    }

    func testBackendEventSummaryUsesLocalizedFallbackTemplates() {
        let stage = BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload")
        let check = BackendEvent(type: "check", operation: "doctor", message: "smbd is running")
        let success = BackendEvent(type: "result", operation: "deploy", ok: true)
        let failure = BackendEvent(type: "result", operation: "deploy", ok: false)
        let error = BackendEvent(type: "error", operation: "deploy")

        XCTAssertEqual(stage.summary, "deploy: upload_payload")
        XCTAssertEqual(check.summary, "INFO smbd is running")
        XCTAssertEqual(success.summary, "deploy: Finished")
        XCTAssertEqual(failure.summary, "deploy: Failed")
        XCTAssertEqual(error.summary, "deploy: Error")
    }

    func testBackendEventResultSummaryPrefersPayloadText() {
        let summary = BackendEvent(
            type: "result",
            operation: "deploy",
            ok: true,
            payload: .object(["summary": .string("Deployment completed on the Time Capsule.")])
        )
        let message = BackendEvent(
            type: "result",
            operation: "activate",
            ok: true,
            payload: .object(["message": .string("Activation completed without reboot.")])
        )
        let legacySummaryText = BackendEvent(
            type: "result",
            operation: "repair-xattrs",
            ok: true,
            payload: .object(["summary_text": .string("Found 2 metadata issue(s), 1 repairable.")])
        )
        let blankSummaryFallsBack = BackendEvent(
            type: "result",
            operation: "doctor",
            ok: true,
            payload: .object(["summary": .string("   ")])
        )

        XCTAssertEqual(summary.summary, "Deployment completed on the Time Capsule.")
        XCTAssertEqual(message.summary, "Activation completed without reboot.")
        XCTAssertEqual(legacySummaryText.summary, "Found 2 metadata issue(s), 1 repairable.")
        XCTAssertEqual(blankSummaryFallsBack.summary, "doctor: Finished")
    }

    func testBackendEventLocalizesKnownResultSummaries() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let event = BackendEvent(
            type: "result",
            operation: "doctor",
            ok: true,
            payload: .object(["summary": .string("Doctor checks passed.")])
        )

        L10n.apply(language: .english)
        XCTAssertEqual(event.localizedPayloadSummaryText, "Doctor checks passed.")
        XCTAssertEqual(event.localizedSummary, "Doctor checks passed.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(event.localizedPayloadSummaryText, "诊断检查通过。")
        XCTAssertEqual(event.localizedSummary, "诊断检查通过。")
    }

    func testBackendSummaryLocalizationCoversRuntimeWaitMessages() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let boot = BackendEvent(
            type: "result",
            operation: "deploy",
            ok: true,
            payload: .object(["summary": .string("Waiting a few seconds for device to boot...")])
        )
        let activate = BackendEvent(
            type: "result",
            operation: "activate",
            ok: true,
            payload: .object(["summary": .string("Waiting a few seconds for device to activate...")])
        )

        L10n.apply(language: .english)
        XCTAssertEqual(boot.localizedPayloadSummaryText, "Waiting a few seconds for device to boot...")
        XCTAssertEqual(activate.localizedPayloadSummaryText, "Waiting a few seconds for device to activate...")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(boot.localizedPayloadSummaryText, "正在等待设备完成启动...")
        XCTAssertEqual(activate.localizedPayloadSummaryText, "正在等待设备完成激活...")
    }

    func testBackendEventLocalizesStructuredResultSummaries() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let repair = BackendEvent(
            type: "result",
            operation: "repair-xattrs",
            ok: true,
            payload: .object([
                "summary_text": .string("Found 2 metadata issue(s), 1 repairable."),
                "finding_count": .number(2),
                "repairable_count": .number(1)
            ])
        )
        let fsck = BackendEvent(
            type: "result",
            operation: "fsck",
            ok: true,
            payload: .object([
                "summary": .string("Dry-run plan generated for fsck."),
                "target": .object(["device": .string("/dev/dk2"), "mountpoint": .string("/Volumes/Data")])
            ])
        )
        let flash = BackendEvent(
            type: "result",
            operation: "flash",
            ok: true,
            payload: .object([
                "summary": .string("Flash patch write validated; manual power cycle required."),
                "mode": .string("patch"),
                "write_status": .string("validated"),
                "write_validated": .bool(true),
                "post_write_action": .string("manual_power_cycle"),
                "reboot_requested": .bool(false),
                "rebooted": .bool(false)
            ])
        )

        L10n.apply(language: .english)
        XCTAssertEqual(repair.localizedPayloadSummaryText, "Found 2 metadata issue(s), 1 repairable.")
        XCTAssertEqual(fsck.localizedPayloadSummaryText, "Dry-run plan generated for fsck.")
        XCTAssertEqual(flash.localizedPayloadSummaryText, "Flash patch write validated; manual power cycle required.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(repair.localizedPayloadSummaryText, "发现 2 个元数据问题，其中 1 个可修复。")
        XCTAssertEqual(fsck.localizedPayloadSummaryText, "已生成 fsck dry-run 计划。")
        XCTAssertEqual(flash.localizedPayloadSummaryText, "Flash patch 写入已验证；需要手动断电重启。")
    }

    func testJSONValueRoundTripsNestedObjects() throws {
        let value = JSONValue.object([
            "operation": .string("capabilities"),
            "params": .object([
                "dry_run": .bool(true),
                "mount_wait": .number(30),
                "items": .array([.string("one"), .null])
            ])
        ])

        let data = try JSONEncoder().encode(value)
        let decoded = try JSONDecoder().decode(JSONValue.self, from: data)

        XCTAssertEqual(decoded, value)
    }
}
