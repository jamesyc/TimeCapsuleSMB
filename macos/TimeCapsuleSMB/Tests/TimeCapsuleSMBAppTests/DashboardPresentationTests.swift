import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DashboardPresentationTests: XCTestCase {
    func testCheckupPresentationHeadlineFollowsState() throws {
        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "ssh ok", domain: "Device"),
            testDoctorCheck(status: "WARN", message: "bonjour missing", domain: "Finder")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .warning)

        XCTAssertEqual(presentation.headline, "Checkup found warnings.")
        XCTAssertEqual(presentation.summaryRows.first, PresentationRow(label: "Pass", value: "1"))
        XCTAssertEqual(presentation.domains.first?.domain, .finderBonjour)
        XCTAssertEqual(presentation.domains.first?.status, .warning)
    }

    func testCheckupPresentationLocalizesKnownDoctorCheckCodes() throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .english)

        let payload = try testDoctorPayload(checks: [
            testDoctorCheck(
                status: "FAIL",
                message: "SMB is configured to bind to LAN-only interface(s) 192.168.1.0/24, but this Mac has no address on those runtime Samba network(s). Disable Bind SMB to LAN Only for this profile and redeploy, or connect from the Time Capsule LAN side.",
                domain: "SMB Auth",
                code: "smb_bind_lan_only_unreachable"
            )
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let presentation = CheckupPresentation(summary: summary, state: .failed)
        let row = try XCTUnwrap(presentation.domains.first?.rows.first)

        XCTAssertEqual(presentation.domains.first?.domain, .smbAuth)
        XCTAssertEqual(
            row.message,
            "SMB is bound to the Time Capsule LAN, but this Mac is on another network. Turn off Bind SMB to LAN Only for this device and run Install / Update SMB, or connect from the Time Capsule LAN side."
        )
        XCTAssertFalse(row.message.contains("configured to bind"))
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
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.runCheckup, store: store))

        _ = coordinator.run(operation: "doctor", context: nil, activeDeviceID: "device-one", laneKey: laneKey)
        try await waitUntilStoreState { store.isBusy }

        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.createPlan, store: store))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.reinstall, store: store))
        XCTAssertFalse(InstallActionAvailabilityPolicy.isEnabled(.runCheckup, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.openFinder, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.viewCheckup, store: store))
        XCTAssertTrue(InstallActionAvailabilityPolicy.isEnabled(.viewDiagnostics, store: store))
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
            testDoctorCheck(status: "PASS", message: "ssh ok", domain: "Device"),
            testDoctorCheck(status: "WARN", message: "bonjour warning", domain: "Bonjour"),
            testDoctorCheck(status: "FAIL", message: "smb failed", domain: "SMB"),
            doctorCheckWithoutDomain(status: "INFO", message: "misc info")
        ]).decode(DoctorPayload.self)
        let summary = DoctorSummary(payload: payload)

        let signals = DoctorCheckDomainPolicy.signals(from: summary)

        XCTAssertEqual(signals.map(\.domain), [.smbAuth, .finderBonjour, .connection, .general])
        XCTAssertEqual(signals.first?.severity, .failed)
        XCTAssertEqual(DoctorCheckDomainPolicy.signal(for: .connection, summary: summary)?.passCount, 1)
        XCTAssertEqual(DoctorCheckDomainPolicy.signal(for: .general, summary: summary)?.infoCount, 1)
        XCTAssertNil(DoctorCheckDomainPolicy.signal(for: .disk, summary: summary))

        let lowerStatusSummary = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: " warn ", message: "disk warning", domain: "Disk")
        ]).decode(DoctorPayload.self))
        XCTAssertEqual(DoctorCheckDomainPolicy.signal(for: .disk, summary: lowerStatusSummary)?.warnCount, 1)
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
            payload: try testReachabilityPayload(status: "partial", summary: "SSH reachable, SMB port closed.")
                .decode(ReachabilityPayload.self)
        )
        let partialRow = try row(.connection, in: DeviceDashboardOverviewPresentation(summary: summary, reachabilitySnapshot: partial))
        XCTAssertEqual(partialRow.status, .warning)
        XCTAssertEqual(partialRow.detail, "SSH reachable, SMB port closed.")

        let unreachable = DeviceReachabilitySnapshot(
            refreshedAt: Date(timeIntervalSince1970: 3),
            payload: try testReachabilityPayload(status: "unreachable", summary: "Could not reach SSH or SMB.")
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

    func testOverviewPresentationAggregatesServiceCheckupDomainsForHealthRow() throws {
        var profile = try makeProfile()
        profile.lastDeployState = testDeployState(
            startedAt: Date(timeIntervalSince1970: 100),
            updatedAt: Date(timeIntervalSince1970: 100),
            finishedAt: Date(timeIntervalSince1970: 100)
        )
        profile.runtimeState = testRuntimeState()
        let checkup = DoctorSummary(payload: try testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "runtime ok", domain: "Runtime"),
            testDoctorCheck(status: "WARN", message: "bonjour warning", domain: "Bonjour"),
            testDoctorCheck(status: "FAIL", message: "smb failed", domain: "SMB"),
            testDoctorCheck(status: "PASS", message: "time machine ok", domain: "Time Machine")
        ]).decode(DoctorPayload.self))
        let summary = DeviceDashboardSummary(
            profile: profile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: nil
        )

        let presentation = DeviceDashboardOverviewPresentation(summary: summary, currentCheckupSummary: checkup)

        XCTAssertEqual(try row(.runtime, in: presentation).status, .good)
        XCTAssertEqual(presentation.healthSections.map(\.domain), [.connection, .runtime, .checkup])
        XCTAssertEqual(try row(.checkup, in: presentation).status, .failed)
        XCTAssertEqual(try row(.checkup, in: presentation).detail, "PASS 1, WARN 1, FAIL 1")
        XCTAssertEqual(try row(.checkup, in: presentation).action, .viewCheckup)
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

        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")
        let hostWarning = DeviceDashboardOverviewPresentation(summary: DeviceDashboardSummary(
            profile: healthyProfile,
            passwordState: .available,
            displayStatus: .healthy,
            primaryAction: .openSMB,
            hostWarning: warning
        ))
        XCTAssertEqual(try row(.checkup, in: hostWarning).status, .warning)
        XCTAssertEqual(try row(.checkup, in: hostWarning).detail, "Time Machine warning.")
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

    func testInstallPlanPresentationShowsDeviceImpactAndWarnings() throws {
        let plan = try netbsd4DeployPlan().decode(DeployPlanPayload.self)
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        let warning = HostCompatibilityWarning(title: "macOS Warning", message: "Time Machine warning.")

        let presentation = InstallPlanPresentation(plan: plan, profile: profile, hostWarning: warning)

        XCTAssertEqual(presentation.title, "Install / Update SMB, Reboot, and Start Runtime")
        XCTAssertFalse(presentation.sections.contains { $0.title == "Files" })
        let target = try XCTUnwrap(presentation.sections.first { $0.title == "Target" })
        XCTAssertTrue(target.rows.contains(PresentationRow(label: "Payload", value: "netbsd4_samba4")))
        XCTAssertFalse(target.rows.contains { $0.label == "Disk" || $0.label == "Payload Directory" })
        let actions = try XCTUnwrap(presentation.sections.first { $0.title == "Device Actions" })
        XCTAssertTrue(actions.rows.contains(PresentationRow(label: "Uploads", value: "1")))
        XCTAssertTrue(actions.rows.contains(PresentationRow(label: "Remote Actions", value: "1")))
        XCTAssertTrue(actions.rows.contains(PresentationRow(label: "Expected Downtime", value: "Several minutes while the device reboots.")))
        XCTAssertEqual(presentation.warnings.count, 2)
    }

    func testInstallPlanPresentationUsesActivateNowMode() throws {
        let plan = try testDeployPlanPayload(
            requiresReboot: false,
            startupMode: .activateNow
        ).decode(DeployPlanPayload.self)
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        let presentation = InstallPlanPresentation(plan: plan, profile: profile)

        XCTAssertEqual(presentation.title, "Install / Update SMB and Start Runtime")
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(PresentationRow(
                label: "Expected Downtime",
                value: "Usually under a minute while Samba starts without rebooting."
            ))
        })
        XCTAssertEqual(presentation.warnings, [])
    }

    func testInstallPlanPresentationShowsNoWaitPostRebootImpact() throws {
        let plan = try netbsd4DeployPlan().decode(DeployPlanPayload.self)
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")
        let options = DeployOptions(
            nbnsEnabled: true,
            noReboot: false,
            noWait: true,
            internalShareUseDiskRoot: false,
            smbBrowseCompatibility: false,
            anyProtocol: false,
            debugLogging: false,
            mountWait: 30
        )

        let presentation = InstallPlanPresentation(plan: plan, profile: profile, options: options)

        XCTAssertEqual(presentation.title, "Install / Update SMB and Request Reboot")
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(PresentationRow(
                label: "Expected Downtime",
                value: "The app will request reboot and return immediately."
            ))
        })
        XCTAssertEqual(presentation.warnings, [
            "No Wait will return after requesting reboot. Samba activation will not run automatically after SSH returns."
        ])
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
            plan: nil,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        XCTAssertEqual(presentation.stateTitle, "Deploy Failed")
        XCTAssertEqual(presentation.statusMessage, "No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart.")
        XCTAssertEqual(presentation.error?.recovery?.title, "No HFS volumes found")
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
            plan: nil,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        XCTAssertEqual(presentation.stateTitle, "Deploy Failed")
        XCTAssertEqual(presentation.statusMessage, "Deploy was interrupted before it completed.")
        XCTAssertEqual(presentation.error?.code, "operation_interrupted")
        XCTAssertEqual(presentation.timeline?.items.first?.title, "Find Payload Volume")
        XCTAssertEqual(presentation.timeline?.items.first?.state, .failed)
        XCTAssertNil(presentation.completion)
    }

    func testInstallWorkflowPresentationCoversAllDeployStates() throws {
        let profile = try makeProfile()
        let plan = try testDeployPlanPayload().decode(DeployPlanPayload.self)
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let cases: [(DeployWorkflowState, DeployPlanPayload?, DeployResultPayload?, BackendErrorViewModel?, [InstallUserAction])] = [
            (.idle, nil, nil, nil, [.installUpdate]),
            (.planning, nil, nil, nil, [.installUpdate]),
            (.planReady, plan, nil, nil, [.installUpdate]),
            (.planStale, plan, nil, nil, [.installUpdate]),
            (.planFailed, nil, nil, error, [.installUpdate]),
            (.deploying, plan, nil, nil, [.installUpdate]),
            (.awaitingConfirmation, plan, nil, nil, [.installUpdate]),
            (.deployed, plan, result, nil, []),
            (.deployFailed, plan, nil, error, [.installUpdate])
        ]

        for testCase in cases {
            let presentation = InstallWorkflowPresentation(
                state: testCase.0,
                plan: testCase.1,
                result: testCase.2,
                error: testCase.3,
                events: [],
                currentStage: nil,
                profile: profile
            )
            XCTAssertEqual(presentation.actions, testCase.4, "Unexpected actions for \(testCase.0)")
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
            plan: nil,
            result: nil,
            error: nil,
            events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ],
            currentStage: nil,
            profile: profile
        )

        let completion = try XCTUnwrap(presentation.completion)
        XCTAssertEqual(presentation.stateTitle, "Deployed")
        XCTAssertEqual(presentation.statusMessage, "Install / Update completed.")
        XCTAssertEqual(presentation.actions, [])
        XCTAssertEqual(completion.title, "Install / Update Verified")
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Reboot Requested", value: "no")))
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Message", value: "Install completed.")))
        XCTAssertEqual(completion.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
        let timeline = try XCTUnwrap(presentation.timeline)
        XCTAssertEqual(timeline.items.map(\.title), ["Done"])
        XCTAssertEqual(timeline.items.first?.detail, "Deployment completed.")
        XCTAssertEqual(timeline.items.first?.state, .succeeded)
    }

    func testInstallWorkflowPresentationKeepsTimelineAfterSuccessfulDeploy() throws {
        let profile = try makeProfile()
        let result = try testDeployResultPayload(payloadFamily: "netbsd6_samba4")
            .decode(DeployResultPayload.self)
        let presentation = InstallWorkflowPresentation(
            state: .deployed,
            plan: nil,
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
            plan: nil,
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
        let plan = try testDeployPlanPayload().decode(DeployPlanPayload.self)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let planReady = InstallWorkflowPresentation(
            state: .planReady,
            plan: plan,
            result: nil,
            error: nil,
            events: [],
            currentStage: nil,
            profile: profile
        )
        XCTAssertEqual(planReady.stateTitle, "Plan Ready")
        XCTAssertEqual(planReady.actions, [.installUpdate])
        XCTAssertNil(planReady.completion)

        let deployFailed = InstallWorkflowPresentation(
            state: .deployFailed,
            plan: plan,
            result: nil,
            error: error,
            events: [],
            currentStage: nil,
            profile: profile
        )
        XCTAssertEqual(deployFailed.stateTitle, "Deploy Failed")
        XCTAssertEqual(deployFailed.actions, [.installUpdate])
        XCTAssertNotNil(deployFailed.timeline)
        XCTAssertNil(deployFailed.completion)
    }

    func testInstallCompletionPresentationShowsVerificationAndNextActions() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd4_samba4", verified: true, netbsd4: true)
            .decode(DeployResultPayload.self)

        let presentation = InstallCompletionPresentation(result: result)

        XCTAssertEqual(presentation.title, "Install / Update Verified")
        XCTAssertTrue(presentation.rows.contains(PresentationRow(label: "Verified", value: "yes")))
        XCTAssertEqual(presentation.warnings, [
            "NetBSD4 devices may need Activate after a later reboot unless the boot hook is patched."
        ])
        XCTAssertEqual(presentation.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
        XCTAssertEqual(InstallUserAction.installUpdate.systemImage, "square.and.arrow.down.on.square")
        XCTAssertEqual(InstallUserAction.reinstall.systemImage, "arrow.clockwise")
        XCTAssertEqual(InstallUserAction.reinstall.title, "Reinstall")
    }

    func testInstallCompletionPresentationReplacesRunCheckupWithViewCheckupWhileChecking() throws {
        let result = try testDeployResultPayload(payloadFamily: "netbsd6_samba4", verified: true, netbsd4: false)
            .decode(DeployResultPayload.self)

        let presentation = InstallCompletionPresentation(result: result, isCheckupRunning: true)

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
        XCTAssertEqual(deploying?.message, "Uploading and applying the managed SMB runtime. This can take a few minutes...")
        XCTAssertEqual(deploying?.detail, "Uploading managed SMB payload files.")
        for state in DeployWorkflowState.allCases where state != .deploying {
            XCTAssertNil(InstallProgressPresentation(state: state, currentStage: stage), "\(state) should not show a blocking progress modal.")
        }
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
        XCTAssertEqual(MaintenanceActionPolicy.actions(for: .repairXattrs), [.scanMetadata, .repairMetadata])
        XCTAssertEqual(MaintenanceUserAction.checkSSHAccess.title, "Check SSH")
        XCTAssertFalse(MaintenanceUserAction.checkSSHAccess.isCommitAction)
        XCTAssertTrue(MaintenanceUserAction.enableSSHAccess.isCommitAction)
        XCTAssertEqual(MaintenanceUserAction.runActivation.title, "Activate")
        XCTAssertTrue(MaintenanceUserAction.runActivation.isCommitAction)
    }

    func testMaintenanceStatusMessagesCoverAllStates() {
        for state in MaintenanceOperationState.allCases {
            XCTAssertFalse(state.maintenanceStatusMessage(for: .activate).isEmpty)
            XCTAssertFalse(state.maintenanceStatusMessage(for: .repairXattrs).isEmpty)
        }

        XCTAssertEqual(MaintenanceOperationState.listReady.maintenanceStatusMessage(for: .fsck), "Choose a volume, then plan disk repair.")
        XCTAssertEqual(MaintenanceOperationState.scanReady.maintenanceStatusMessage(for: .repairXattrs), "Review the scan before repairing metadata.")
        XCTAssertEqual(MaintenanceOperationState.scanReady.maintenanceStatusMessage(for: .activate), "Scan Ready")
    }

    func testMaintenancePresentationHidesActivationForDevicesThatDoNotNeedIt() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd6_samba4")

        let presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

        XCTAssertEqual(presentation.cards.map { $0.workflow }, [MaintenanceWorkflow.sshAccess, .uninstall, .fsck, .repairXattrs])
        XCTAssertEqual(presentation.cards.first?.isSelected, true)
        XCTAssertEqual(presentation.detail.workflow, .sshAccess)
        XCTAssertEqual(presentation.detail.title, "SSH Access")
    }

    func testMaintenancePresentationKeepsActivationForNetBSD4Devices() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        let presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

        XCTAssertEqual(presentation.cards.map { $0.workflow }, [MaintenanceWorkflow.sshAccess, .activate, .uninstall, .fsck, .repairXattrs])
        XCTAssertEqual(presentation.cards.first?.isSelected, false)
        XCTAssertEqual(presentation.detail.workflow, .activate)
    }

    func testMaintenancePresentationShowsRunActionsDisabledBeforePlanning() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        var presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .activate)
        XCTAssertEqual(presentation.detail.actions, [.runActivation])
        XCTAssertTrue(presentation.detail.isEnabled(.runActivation))

        store.selectedWorkflow = .uninstall
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.actions, [.runUninstall])
        XCTAssertTrue(presentation.detail.isEnabled(.runUninstall))

        store.selectedWorkflow = .fsck
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.actions, [.findVolumes, .planFsck, .runFsck])
        XCTAssertTrue(presentation.detail.isEnabled(.findVolumes))
        XCTAssertFalse(presentation.detail.isEnabled(.planFsck))
        XCTAssertFalse(presentation.detail.isEnabled(.runFsck))

        store.selectedWorkflow = .repairXattrs
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.actions, [.scanMetadata, .repairMetadata])
        XCTAssertFalse(presentation.detail.isEnabled(.scanMetadata))
        XCTAssertFalse(presentation.detail.isEnabled(.repairMetadata))
    }

    func testMaintenancePresentationBuildsWorkflowPlansAndCompletions() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: true))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [testFsckTargetPayload(name: "Data")]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        store.planActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
        var presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .activate)
        XCTAssertEqual(presentation.detail.actions, [.runActivation])
        XCTAssertTrue(presentation.detail.isEnabled(.runActivation))
        XCTAssertEqual(presentation.detail.plan?.title, "Activation Plan")
        XCTAssertEqual(presentation.detail.plan?.rows.first, PresentationRow(label: "Device", value: profile.title))

        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.completion?.title, "Activation Complete")
        XCTAssertTrue(presentation.detail.completion?.rows.contains(PresentationRow(label: "Already Active", value: "yes")) == true)

        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .uninstall)
        XCTAssertEqual(presentation.detail.actions, [.runUninstall])
        XCTAssertTrue(presentation.detail.isEnabled(.runUninstall))
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Uninstall removes installed files from this device."])

        store.refreshFsckTargets(password: "pw")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .fsck)
        XCTAssertEqual(presentation.detail.actions, [.findVolumes, .planFsck, .runFsck])
        XCTAssertTrue(presentation.detail.isEnabled(.findVolumes))
        XCTAssertTrue(presentation.detail.isEnabled(.planFsck))
        XCTAssertFalse(presentation.detail.isEnabled(.runFsck))

        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.plan?.title, "Disk Repair Plan")
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Disk repair can modify the selected volume."])

        store.repairPath = "/Volumes/Data"
        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .repairXattrs)
        XCTAssertEqual(presentation.detail.actions, [.scanMetadata, .repairMetadata])
        XCTAssertTrue(presentation.detail.isEnabled(.scanMetadata))
        XCTAssertTrue(presentation.detail.isEnabled(.repairMetadata))
        XCTAssertEqual(presentation.detail.plan?.title, "Metadata Scan")
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Metadata repair modifies files under the selected local SMB mount."])
    }

    func testMaintenancePresentationKeepsTimelineAfterWorkflowCompletes() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ]),
            .init(events: [
                BackendEvent(type: "stage", operation: "activate", stage: "probe_runtime"),
                BackendEvent(type: "stage", operation: "activate", stage: "run_activation"),
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationResultPayload(alreadyActive: false))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: testUninstallPlanPayload())
            ])
        ])
        let store = MaintenanceStore(backend: BackendClient(runner: runner))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        store.planActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .planReady && !store.isRunning }
        store.runActivation(password: "pw")
        try await waitUntilStoreState { store.activateState == .succeeded && !store.isRunning }

        var presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.timeline?.items.map(\.title), [
            "Check Existing Runtime",
            "Starting SMB",
            "Done"
        ])
        XCTAssertEqual(presentation.detail.timeline?.items.map(\.state), [.succeeded, .succeeded, .succeeded])

        store.planUninstall(password: "pw")
        try await waitUntilStoreState { store.uninstallState == .planReady && !store.isRunning }
        store.selectedWorkflow = .activate
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

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

    private func netbsd4DeployPlan() -> JSONValue {
        .object([
            "schema_version": .number(1),
            "host": .string("root@10.0.0.2"),
            "volume_root": .string("/Volumes/dk2"),
            "payload_dir": .string("/Volumes/dk2/.samba4"),
            "payload_family": .string("netbsd4_samba4"),
            "netbsd4": .bool(true),
            "requires_reboot": .bool(true),
            "reboot_required": .bool(true),
            "startup_mode": .string("reboot_then_activate"),
            "uploads": .array([.object(["description": .string("smbd")])]),
            "pre_upload_actions": .array([]),
            "post_upload_actions": .array([]),
            "activation_actions": .array([.object(["description": .string("start smbd")])]),
            "post_deploy_checks": .array([]),
            "summary": .string("Deployment dry-run plan generated.")
        ])
    }

    private func doctorCheckWithoutDomain(status: String, message: String) -> JSONValue {
        .object([
            "status": .string(status),
            "message": .string(message),
            "details": .object([:])
        ])
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
