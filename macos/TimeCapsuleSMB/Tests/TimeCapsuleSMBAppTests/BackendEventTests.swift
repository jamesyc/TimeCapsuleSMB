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

    func testBackendEventLocalizesKeyedResultSummaries() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let event = BackendEvent(
            type: "result",
            operation: "doctor",
            ok: true,
            payload: testSummaryPayload("Doctor checks passed.", key: "doctor_checks_passed")
        )

        L10n.apply(language: .english)
        XCTAssertEqual(event.localizedPayloadSummaryText, "Doctor checks passed.")
        XCTAssertEqual(event.localizedSummary, "Doctor checks passed.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(event.localizedPayloadSummaryText, "诊断检查通过。")
        XCTAssertEqual(event.localizedSummary, "诊断检查通过。")
    }

    func testUnkeyedResultSummaryIsShownAsSent() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)
        // Neither the English text nor the payload's fields are used to guess
        // a key: a known sentence without its key stays in English.
        let event = BackendEvent(
            type: "result",
            operation: "fsck",
            ok: true,
            payload: .object([
                "summary": .string("Disk repair completed with fsck."),
                "device": .string("/dev/dk2"),
                "mountpoint": .string("/Volumes/dk2")
            ])
        )

        XCTAssertEqual(event.localizedPayloadSummaryText, "Disk repair completed with fsck.")
        XCTAssertEqual(event.localizedSummary, "Disk repair completed with fsck.")
    }

    func testFailedResultIsSummarizedFromItsOwnKeyNotItsFields() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        // A failed fsck keeps device and mountpoint, the fields of a
        // successful repair; only its fsck_failed key decides the summary.
        let failed = BackendEvent(type: "result", operation: "fsck", ok: false, payload: testFsckFailedResultPayload(returncode: 8))
        let succeeded = BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckResultPayload(returncode: 0))

        L10n.apply(language: .english)
        let failure = "fsck_hfs exited with status 8; the disk may still need repair."
        XCTAssertEqual(failed.localizedPayloadSummaryText, failure)
        XCTAssertEqual(failed.localizedSummary, failure)
        XCTAssertEqual(BackendErrorViewModel(event: failed).message, failure)
        XCTAssertEqual(succeeded.localizedPayloadSummaryText, "Disk repair completed with fsck.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(failed.localizedPayloadSummaryText, "fsck_hfs 以状态 8 退出；磁盘可能仍需修复。")
        XCTAssertEqual(BackendErrorViewModel(event: failed).message, "fsck_hfs 以状态 8 退出；磁盘可能仍需修复。")
        XCTAssertEqual(succeeded.localizedPayloadSummaryText, "已使用 fsck 完成磁盘修复。")
    }

    func testFailedResultStillTranslatesItsOwnKnownSummary() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let event = BackendEvent(type: "result", operation: "doctor", ok: false, payload: testDoctorPayload(fatal: true, checks: [
            testDoctorCheck(status: "FAIL", message: "smbd is not running", domain: "Runtime")
        ]))

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(event.localizedPayloadSummaryText, L10n.string("backend.summary.doctor_found_fatal"))
        XCTAssertNotEqual(event.localizedPayloadSummaryText, "Doctor found one or more fatal problems.")
    }

    func testBackendEventLocalizesKnownErrorSummaries() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let event = BackendEvent(type: "error", operation: "doctor", code: "auth_failed", message: "Password rejected.")

        L10n.apply(language: .english)
        XCTAssertEqual(event.localizedSummary, "doctor: The device rejected the supplied password or SSH credentials.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(event.localizedSummary, "doctor：设备拒绝了提供的密码或 SSH 凭据。")
    }

    func testKeyedLogMessagesAreLocalizedAndOthersShownAsSent() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let boot = BackendEvent(
            type: "log",
            operation: "deploy",
            level: "info",
            message: "Waiting a few seconds for device to boot...",
            messageKey: "waiting_device_boot",
            messageArgs: []
        )
        let activate = BackendEvent(
            type: "log",
            operation: "activate",
            level: "info",
            message: "Waiting a few seconds for device to activate...",
            messageKey: "waiting_device_activate"
        )
        let plain = BackendEvent(type: "log", operation: "deploy", level: "info", message: "Copying smbd.")

        L10n.apply(language: .english)
        XCTAssertEqual(boot.localizedSummary, "Waiting a few seconds for device to boot...")
        XCTAssertEqual(activate.localizedSummary, "Waiting a few seconds for device to activate...")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(boot.localizedSummary, "正在等待设备完成启动...")
        XCTAssertEqual(activate.localizedSummary, "正在等待设备完成激活...")
        XCTAssertEqual(plain.localizedSummary, "Copying smbd.")
    }

    func testDecodedLogEventKeepsItsMessageKey() throws {
        let data = Data(#"""
        {"type": "log", "operation": "deploy", "level": "info", "message": "Waiting a few seconds for device to boot...",
         "message_key": "waiting_device_boot", "message_args": []}
        """#.utf8)

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.messageKey, "waiting_device_boot")
        XCTAssertEqual(event.messageArgs, [])
        XCTAssertEqual(event.withRequestId("r").messageKey, "waiting_device_boot")
    }

    func testBackendEventLocalizesResultSummaryArguments() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        let repair = BackendEvent(
            type: "result",
            operation: "repair-xattrs",
            ok: true,
            payload: testRepairXattrsPayload(findings: 2, repairable: 1)
        )
        let backup = BackendEvent(
            type: "result",
            operation: "flash",
            ok: true,
            payload: testSummaryPayload(
                "Flash backup saved to /tmp/flash-backup.",
                key: "flash_backup_saved",
                args: [.string("/tmp/flash-backup")]
            )
        )
        let someMatch = BackendEvent(
            type: "result",
            operation: "flash",
            ok: true,
            payload: testSummaryPayload(
                "1 of 2 candidate firmware banks match Apple stock firmware 7.8.1.",
                key: "flash.apple_some_match_version",
                args: [.number(1), .number(2), .string("7.8.1")]
            )
        )

        L10n.apply(language: .english)
        XCTAssertEqual(repair.localizedPayloadSummaryText, "Found 2 metadata issue(s), 1 repairable.")
        XCTAssertEqual(backup.localizedPayloadSummaryText, "Flash backup saved to /tmp/flash-backup.")
        XCTAssertEqual(someMatch.localizedPayloadSummaryText, "1 of 2 candidate firmware banks match Apple stock firmware 7.8.1.")

        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(repair.localizedPayloadSummaryText, "发现 2 个元数据问题，其中 1 个可修复。")
        XCTAssertEqual(backup.localizedPayloadSummaryText, "闪存备份已保存到 /tmp/flash-backup。")
        XCTAssertTrue(someMatch.localizedPayloadSummaryText?.contains("7.8.1") == true)
        XCTAssertNotEqual(someMatch.localizedPayloadSummaryText, "1 of 2 candidate firmware banks match Apple stock firmware 7.8.1.")
    }

    func testMalformedSummaryArgumentsFallBackToTheEnglishText() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)
        let cases: [(String, [JSONValue])] = [
            ("missing argument", [.number(2)]),
            ("extra argument", [.number(2), .number(1), .number(0)]),
            ("string for a count", [.string("2"), .number(1)]),
            ("fractional count", [.number(2.5), .number(1)]),
            ("bool argument", [.bool(true), .number(1)])
        ]

        for (name, args) in cases {
            let event = BackendEvent(
                type: "result",
                operation: "repair-xattrs",
                ok: true,
                payload: testSummaryPayload("Found 2 metadata issue(s), 1 repairable.", key: "repair_xattrs_found", args: args)
            )
            XCTAssertEqual(event.localizedPayloadSummaryText, "Found 2 metadata issue(s), 1 repairable.", name)
        }
        let unknownKey = BackendEvent(
            type: "result",
            operation: "doctor",
            ok: true,
            payload: testSummaryPayload("A summary from a newer helper.", key: "not_in_this_app")
        )
        XCTAssertEqual(unknownKey.localizedPayloadSummaryText, "A summary from a newer helper.")
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
