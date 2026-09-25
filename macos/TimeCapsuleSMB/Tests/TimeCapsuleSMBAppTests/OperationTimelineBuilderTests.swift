import XCTest
@testable import TimeCapsuleSMBApp

final class OperationTimelineBuilderTests: XCTestCase {
    func testBuildsUserFacingTimelineFromStagesResultsAndErrors() {
        let events = [
            BackendEvent(
                type: "stage",
                operation: "deploy",
                stage: "upload_payload",
                risk: "remote_write",
                cancellable: false,
                description: "Upload managed Samba payload files."
            ),
            BackendEvent(
                type: "error",
                operation: "deploy",
                code: "confirmation_required",
                message: "Confirm deployment."
            ),
            BackendEvent(
                type: "result",
                operation: "deploy",
                ok: true,
                payload: .object(["summary": .string("Deployment completed.")])
            )
        ]

        let timeline = OperationTimelineBuilder.timeline(from: events)

        XCTAssertEqual(timeline.map(\.title), ["Upload Payload", "Needs Confirmation", "Done"])
        XCTAssertEqual(timeline[0].risk, "remote_write")
        XCTAssertEqual(timeline[0].cancellable, false)
        XCTAssertEqual(timeline[0].state, .succeeded)
        XCTAssertEqual(timeline[1].state, .warning)
        XCTAssertEqual(timeline[2].state, .succeeded)
        XCTAssertEqual(timeline[2].detail, "Samba installation or update completed.")
    }

    func testStageBecomesSucceededWhenLaterStageForSameOperationAppears() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "validate_artifacts"),
            BackendEvent(type: "stage", operation: "doctor", stage: "run_checks"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Check Local Files", "Running Checkup", "Upload Payload"])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .running, .running])
    }

    func testSuccessfulResultCompletesLastStage() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "uninstall", stage: "build_uninstall_plan"),
            BackendEvent(type: "stage", operation: "uninstall", stage: "uninstall_payload"),
            BackendEvent(type: "result", operation: "uninstall", ok: true, payload: .object(["summary": .string("removed")]))
        ])

        XCTAssertEqual(timeline.map(\.title), ["Planning Uninstall", "Removing Managed Files", "Done"])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .succeeded, .succeeded])
    }

    func testFailureDoesNotMarkCurrentStageSucceeded() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload"),
            BackendEvent(type: "result", operation: "deploy", ok: false, payload: .object(["summary": .string("upload failed")]))
        ])

        XCTAssertEqual(timeline.map(\.title), ["Upload Payload", "Failed"])
        XCTAssertEqual(timeline.map(\.state), [.failed, .failed])
    }

    func testErrorMarksCurrentStageFailed() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "read_mast"),
            BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: "No deployable HFS disk was found.")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Find Payload Volume", "Needs Attention"])
        XCTAssertEqual(timeline.map(\.state), [.failed, .failed])
    }

    func testOperationTitlesAreUserFacing() {
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("deploy"), "Install / Update")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("doctor"), "Checkup")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("repair-xattrs"), "File Metadata Repair")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("capabilities"), "App Readiness")
        XCTAssertEqual(OperationTimelineBuilder.operationTitle("flash"), "Persistent NetBSD4 Boot Hook")
    }

    func testDeployStartupStagesAreUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "probe_runtime"),
            BackendEvent(type: "stage", operation: "deploy", stage: "post_reboot_boot_settle"),
            BackendEvent(type: "stage", operation: "deploy", stage: "post_reboot_activation"),
            BackendEvent(type: "stage", operation: "deploy", stage: "post_activation_settle"),
            BackendEvent(type: "stage", operation: "deploy", stage: "verify_runtime_activation")
        ])

        XCTAssertEqual(timeline.map(\.title), [
            "Check Boot Startup",
            "Let Device Finish Booting",
            "Start SMB After Reboot",
            "Wait for services to settle",
            "Verify SMB Startup"
        ])
        XCTAssertEqual(
            timeline.first?.detail,
            "Checking whether the device will start TimeCapsuleSMB automatically."
        )
        XCTAssertEqual(
            timeline[1].detail,
            "Waiting briefly after SSH returns before probing boot-time services."
        )
        XCTAssertEqual(
            timeline[3].detail,
            "Waiting briefly after activation before probing runtime readiness."
        )
    }

    func testConfigureAcpPortProbeStageIsUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "configure", stage: "acp_port_probe")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Checking AirPort ACP"])
    }

    func testActivateRuntimeProbeStageIsUserFacing() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "activate", stage: "probe_runtime"),
            BackendEvent(type: "stage", operation: "activate", stage: "post_activation_settle")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Check Existing Runtime", "Wait for services to settle"])
        XCTAssertEqual(
            timeline.first?.detail,
            "Checking whether TimeCapsuleSMB is already running before activating it."
        )
        XCTAssertEqual(
            timeline[1].detail,
            "Waiting briefly after activation before probing runtime readiness."
        )
    }

    func testDeployCleanupStageWarnsAboutOldFileDeletion() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "pre_upload_actions")
        ])

        XCTAssertEqual(timeline.map(\.title), ["Stop Existing Runtime"])
    }

    func testDeployUploadStagesAreUserFacingAndPresentTense() {
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_xattr_migrator"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_rsync"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_boot_files"),
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_runtime_config")
        ])

        XCTAssertEqual(timeline.map(\.title), [
            "Upload smbd",
            "Upload metadata migrator",
            "Upload rsync",
            "Upload Boot Files",
            "Upload Runtime Config"
        ])
    }

    func testDeployRebootStagesUseLocalizedDetails() {
        XCTAssertEqual(
            OperationTimelineBuilder.stageDetail(
                for: "deploy",
                stage: "wait_for_reboot_up",
                fallback: "raw backend detail"
            ),
            "Device went down; waiting for it to come back up."
        )
        XCTAssertEqual(
            OperationTimelineBuilder.stageDetail(
                for: "deploy",
                stage: "verify_runtime_reboot",
                fallback: "raw backend detail"
            ),
            "Device is back online. Waiting for managed runtime to finish starting."
        )
    }

    func testAllKnownDeployStagesHaveLocalizedTitlesAndDetails() {
        let deployStages = [
            "load_config",
            "resolve_managed_target",
            "validate_artifacts",
            "check_compatibility",
            "read_mast",
            "select_payload_home",
            "inventory_legacy_metadata",
            "build_deployment_plan",
            "check_flash_capacity",
            "pre_upload_actions",
            "prepare_deployment_files",
            "upload_payload",
            "upload_smbd",
            "upload_xattr_migrator",
            "inspect_migration_sources",
            "migrate_xattrs_copy",
            "migrate_xattrs_cleanup",
            "replace_software",
            "install_runtime_config",
            "upload_rsync",
            "upload_boot_files",
            "upload_runtime_config",
            "post_upload_actions",
            "verify_payload_upload",
            "flush_payload_upload",
            "enable_boot",
            "flush_boot_hook",
            "verify_payload_upload_after_sync",
            "reboot",
            "wait_for_reboot_down",
            "wait_for_reboot_up",
            "probe_runtime",
            "post_reboot_boot_settle",
            "post_activation_settle",
            "post_reboot_activation",
            "verify_runtime_activation",
            "verify_runtime_reboot"
        ]

        for stage in deployStages {
            let title = OperationTimelineBuilder.stageTitle(for: "deploy", stage: stage)
            let detail = OperationTimelineBuilder.stageDetail(for: "deploy", stage: stage, fallback: nil)
            XCTAssertFalse(title.hasPrefix("timeline."), "\(stage) title should be localized")
            XCTAssertFalse(title.contains("_"), "\(stage) title should not fall back to title-cased stage id")
            XCTAssertNotNil(detail, "\(stage) should have a localized detail")
            XCTAssertFalse(detail?.hasPrefix("timeline.") == true, "\(stage) detail should be localized")
        }
    }

    func testAllKnownActivateStagesHaveLocalizedTitlesAndDetails() {
        let activateStages = [
            "probe_runtime",
            "post_activation_settle"
        ]

        for stage in activateStages {
            let title = OperationTimelineBuilder.stageTitle(for: "activate", stage: stage)
            let detail = OperationTimelineBuilder.stageDetail(for: "activate", stage: stage, fallback: nil)
            XCTAssertFalse(title.hasPrefix("timeline."), "\(stage) title should be localized")
            XCTAssertFalse(title.contains("_"), "\(stage) title should not fall back to title-cased stage id")
            XCTAssertNotNil(detail, "\(stage) should have a localized detail")
            XCTAssertFalse(detail?.hasPrefix("timeline.") == true, "\(stage) detail should be localized")
        }
    }

    func testDeployMetadataMigrationStagesFollowBackendOrderWithLocalizedCopy() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "upload_xattr_migrator", description: "Upload the one-shot HFS metadata migrator."),
            BackendEvent(type: "stage", operation: "deploy", stage: "inspect_migration_sources", description: "Fingerprint legacy Samba metadata before migration."),
            BackendEvent(type: "stage", operation: "deploy", stage: "migrate_xattrs_copy", description: "Copy and verify legacy Samba metadata in native HFS storage."),
            BackendEvent(type: "stage", operation: "deploy", stage: "migrate_xattrs_cleanup", description: "Reverify native HFS metadata and remove migrated legacy storage.")
        ])

        XCTAssertEqual(timeline.map(\.title), [
            "Upload metadata migrator",
            "Inspect legacy metadata",
            "Migrate metadata",
            "Remove migrated metadata"
        ])
        XCTAssertEqual(timeline.map(\.detail), [
            "Copying the one-time metadata migration helper to the device.",
            "Recording the current state of the legacy Samba metadata (xattr.tdb) before migrating it.",
            "Copying legacy Samba metadata into native HFS extended attributes and verifying the copy.",
            "Re-verifying the native HFS metadata, then removing the migrated legacy Samba metadata."
        ])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .succeeded, .succeeded, .running])

        L10n.apply(language: .german)
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "migrate_xattrs_copy"), "Metadaten migrieren")
        L10n.apply(language: .simplifiedChinese)
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "migrate_xattrs_cleanup"), "移除已迁移的元数据")
    }

    func testDeploySoftwareReplacementStagesUseLocalizedCopyInsteadOfBackendText() {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        // Backend order: inventory before migration, replacement before uploads,
        // runtime settings after migration cleanup and before enable_boot.
        let timeline = OperationTimelineBuilder.timeline(from: [
            BackendEvent(type: "stage", operation: "deploy", stage: "inventory_legacy_metadata", description: "raw inventory text"),
            BackendEvent(type: "stage", operation: "deploy", stage: "replace_software", description: "raw replace text"),
            BackendEvent(type: "stage", operation: "deploy", stage: "install_runtime_config", description: "raw config text"),
            BackendEvent(type: "stage", operation: "deploy", stage: "enable_boot")
        ])

        XCTAssertEqual(timeline.map(\.title), [
            "Find existing installations",
            "Remove old software",
            "Install service settings",
            "Upload Boot Files"
        ])
        XCTAssertEqual(Array(timeline.prefix(3).map(\.detail)), [
            "Looking on each disk for earlier installations and legacy Samba metadata (xattr.tdb).",
            "Removing previously installed software files from flash and the disks. Metadata and logs are kept.",
            "Writing service settings (tcapsulesmb.conf) to /mnt/Flash."
        ])
        XCTAssertEqual(timeline.map(\.state), [.succeeded, .succeeded, .succeeded, .running])

        L10n.apply(language: .russian)
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "replace_software"), "Удалить старое ПО")
        L10n.apply(language: .portuguese)
        XCTAssertEqual(
            OperationTimelineBuilder.stageDetail(for: "deploy", stage: "install_runtime_config", fallback: "raw config text"),
            "Gravando a configuração dos serviços (tcapsulesmb.conf) em /mnt/Flash."
        )
    }

    func testRemovedDiscoveryUploadStageIsNotMapped() {
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "upload_discovery"), "Upload Discovery")
        XCTAssertNil(OperationTimelineBuilder.stageDetail(for: "deploy", stage: "upload_discovery", fallback: nil))
    }

    func testRemovedNetBSD4DeployActivationStageIsNotMapped() {
        XCTAssertEqual(OperationTimelineBuilder.stageTitle(for: "deploy", stage: "netbsd4_activation"), "Netbsd4 Activation")
        XCTAssertNil(OperationTimelineBuilder.stageDetail(for: "deploy", stage: "netbsd4_activation", fallback: nil))
    }
}
