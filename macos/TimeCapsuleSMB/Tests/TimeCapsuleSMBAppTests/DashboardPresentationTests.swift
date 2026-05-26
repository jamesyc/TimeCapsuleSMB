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

    func testInstallActionAvailabilityBlocksMutatingActionsWhileDeviceIsBusy() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: []))
            ], delayNanoseconds: 100_000_000)
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
            .idle: "Run a checkup to inspect this Time Capsule.",
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
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 100),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )
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
        healthyProfile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 120),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )
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
        netbsd4Profile.lastDeploy = healthyProfile.lastDeploy
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
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 120),
            state: .deployed,
            payloadFamily: "netbsd6_samba4",
            rebootRequested: true,
            verified: true,
            summary: "installed"
        )

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
        XCTAssertEqual(try row(.checkup, in: checking).action, .viewCheckup)

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
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(InstallPlanRow(label: "Remote Actions", value: "1"))
        })
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(InstallPlanRow(label: "Expected Downtime", value: "Several minutes while the Time Capsule reboots."))
        })
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
            section.rows.contains(InstallPlanRow(
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
            anyProtocol: false,
            debugLogging: false,
            mountWait: 30
        )

        let presentation = InstallPlanPresentation(plan: plan, profile: profile, options: options)

        XCTAssertEqual(presentation.title, "Install / Update SMB and Request Reboot")
        XCTAssertTrue(presentation.sections.contains { section in
            section.rows.contains(InstallPlanRow(
                label: "Expected Downtime",
                value: "The app will request reboot and return immediately."
            ))
        })
        XCTAssertEqual(presentation.warnings, [
            "No Wait will return after requesting reboot. Samba activation will not run automatically after SSH returns."
        ])
    }

    func testInstallWorkflowPresentationCoversAllDeployStates() throws {
        let profile = try makeProfile()
        let plan = try testDeployPlanPayload().decode(DeployPlanPayload.self)
        let result = try testDeployResultPayload().decode(DeployResultPayload.self)
        let error = BackendErrorViewModel(operation: "deploy", code: "operation_failed", message: "failed")

        let cases: [(DeployWorkflowState, DeployPlanPayload?, DeployResultPayload?, BackendErrorViewModel?, InstallUserAction?)] = [
            (.idle, nil, nil, nil, .createPlan),
            (.planning, nil, nil, nil, nil),
            (.planReady, plan, nil, nil, .installUpdate),
            (.planStale, plan, nil, nil, .regeneratePlan),
            (.planFailed, nil, nil, error, .createPlan),
            (.deploying, plan, nil, nil, nil),
            (.awaitingConfirmation, plan, nil, nil, nil),
            (.deployed, plan, result, nil, nil),
            (.deployFailed, plan, nil, error, .regeneratePlan)
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
            XCTAssertEqual(presentation.primaryAction, testCase.4, "Unexpected primary action for \(testCase.0)")
        }
    }

    func testInstallWorkflowPresentationRestoresPostInstallViewFromSavedDeploySnapshot() throws {
        var profile = try makeProfile()
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 200),
            state: .deployed,
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
            profile: profile
        )

        let completion = try XCTUnwrap(presentation.completion)
        XCTAssertEqual(presentation.stateTitle, "Deployed")
        XCTAssertEqual(presentation.statusMessage, "Install / Update completed.")
        XCTAssertNil(presentation.primaryAction)
        XCTAssertEqual(completion.title, "Install / Update Verified")
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Reboot Requested", value: "no")))
        XCTAssertTrue(completion.rows.contains(PresentationRow(label: "Message", value: "Installed from previous app session.")))
        XCTAssertEqual(completion.actions, [.reinstall, .openFinder, .runCheckup, .viewDiagnostics])
    }

    func testInstallWorkflowPresentationShowsViewCheckupWhenCheckupIsRunning() throws {
        var profile = try makeProfile()
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 200),
            state: .deployed,
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
        profile.lastDeploy = DeviceDeploySnapshot(
            deployedAt: Date(timeIntervalSince1970: 200),
            state: .deployed,
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
        XCTAssertEqual(planReady.primaryAction, .installUpdate)
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
        XCTAssertEqual(deployFailed.primaryAction, .regeneratePlan)
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
        XCTAssertEqual(presentation.items.first?.title, "Uploading")
    }

    func testInstallProgressPresentationAppearsOnlyWhileDeploying() {
        let stage = OperationStageState(event: BackendEvent(
            type: "stage",
            operation: "deploy",
            stage: "upload_payload",
            description: "Uploading files."
        ))

        let deploying = InstallProgressPresentation(state: .deploying, currentStage: stage)

        XCTAssertEqual(deploying?.title, "Installing / Updating SMB")
        XCTAssertEqual(deploying?.message, "Uploading and applying the managed SMB runtime. This can take a few minutes...")
        XCTAssertEqual(deploying?.detail, "Uploading files.")
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

    func testMaintenanceActionPolicyCoversAllStates() {
        let expectedActivate: [MaintenanceOperationState: MaintenanceUserAction] = [
            .idle: .planActivation,
            .planReady: .runActivation,
            .succeeded: .planActivation,
            .failed: .planActivation
        ]
        let expectedUninstall: [MaintenanceOperationState: MaintenanceUserAction] = [
            .idle: .planUninstall,
            .planReady: .runUninstall,
            .planStale: .planUninstall,
            .succeeded: .planUninstall,
            .failed: .planUninstall
        ]
        let expectedFsck: [MaintenanceOperationState: MaintenanceUserAction] = [
            .idle: .findVolumes,
            .listReady: .planFsck,
            .planReady: .runFsck,
            .planStale: .planFsck,
            .succeeded: .findVolumes,
            .failed: .findVolumes
        ]
        let expectedRepair: [MaintenanceOperationState: MaintenanceUserAction] = [
            .idle: .scanMetadata,
            .scanReady: .repairMetadata,
            .scanStale: .scanMetadata,
            .repaired: .scanMetadata,
            .failed: .scanMetadata
        ]

        for state in MaintenanceOperationState.allCases {
            XCTAssertEqual(primaryAction(.activate, state: state), expectedActivate[state], "Unexpected activate action for \(state).")
            XCTAssertEqual(primaryAction(.uninstall, state: state), expectedUninstall[state], "Unexpected uninstall action for \(state).")
            XCTAssertEqual(primaryAction(.fsck, state: state), expectedFsck[state], "Unexpected fsck action for \(state).")
            XCTAssertEqual(primaryAction(.repairXattrs, state: state), expectedRepair[state], "Unexpected repair action for \(state).")
        }

        XCTAssertNil(primaryAction(.fsck, state: .listReady, hasSelectedFsckTarget: false))
        XCTAssertEqual(primaryAction(.repairXattrs, state: .scanReady, canRepairXattrs: false), .scanMetadata)
        XCTAssertEqual(MaintenanceActionPolicy.secondaryActions(workflow: .fsck, state: .planReady), [.planFsck, .findVolumes])
        XCTAssertEqual(MaintenanceActionPolicy.secondaryActions(workflow: .repairXattrs, state: .scanReady), [.scanMetadata])
        XCTAssertEqual(MaintenanceUserAction.planActivation.title, "Plan Activate")
        XCTAssertEqual(MaintenanceUserAction.runActivation.title, "Activate")
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

        XCTAssertEqual(presentation.cards.map { $0.workflow }, [MaintenanceWorkflow.uninstall, .fsck, .repairXattrs])
        XCTAssertEqual(presentation.cards.first?.isSelected, true)
        XCTAssertEqual(presentation.detail.workflow, .uninstall)
        XCTAssertEqual(presentation.detail.title, "Uninstall")
    }

    func testMaintenancePresentationKeepsActivationForNetBSD4Devices() throws {
        let store = MaintenanceStore(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let profile = try makeProfile(payloadFamily: "netbsd4_samba4")

        let presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

        XCTAssertEqual(presentation.cards.map { $0.workflow }, [MaintenanceWorkflow.activate, .uninstall, .fsck, .repairXattrs])
        XCTAssertEqual(presentation.cards.first?.isSelected, true)
        XCTAssertEqual(presentation.detail.workflow, .activate)
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
        XCTAssertEqual(presentation.detail.primaryAction, .runActivation)
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
        XCTAssertEqual(presentation.detail.primaryAction, .runUninstall)
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Uninstall removes managed SMB files from this Time Capsule."])

        store.refreshFsckTargets(password: "pw")
        try await waitUntilStoreState { store.fsckState == .listReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .fsck)
        XCTAssertEqual(presentation.detail.primaryAction, .planFsck)

        store.planFsck(password: "pw")
        try await waitUntilStoreState { store.fsckState == .planReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.plan?.title, "Disk Repair Plan")
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Disk repair can modify the selected Time Capsule volume."])

        store.repairPath = "/Volumes/Data"
        store.scanRepairXattrs()
        try await waitUntilStoreState { store.repairState == .scanReady && !store.isRunning }
        presentation = MaintenanceDashboardPresentation(store: store, profile: profile)
        XCTAssertEqual(presentation.detail.workflow, .repairXattrs)
        XCTAssertEqual(presentation.detail.primaryAction, .repairMetadata)
        XCTAssertEqual(presentation.detail.plan?.title, "Metadata Scan")
        XCTAssertEqual(presentation.detail.plan?.warnings, ["Metadata repair modifies files under the selected local SMB mount."])
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
            "summary": .string("deployment dry-run plan generated.")
        ])
    }

    private func primaryAction(
        _ workflow: MaintenanceWorkflow,
        state: MaintenanceOperationState,
        hasSelectedFsckTarget: Bool = true,
        canRepairXattrs: Bool = true
    ) -> MaintenanceUserAction? {
        MaintenanceActionPolicy.primaryAction(for: MaintenanceActionContext(
            workflow: workflow,
            state: state,
            hasSelectedFsckTarget: hasSelectedFsckTarget,
            canRepairXattrs: canRepairXattrs
        ))
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
        payloadFamily: String = "netbsd6_samba4",
        syap: String = "119",
        model: String = "Time Capsule",
        deviceGeneration: String = "tc_gen4"
    ) throws -> DeviceProfile {
        DeviceProfile.make(
            id: id,
            configuredDevice: try testConfiguredDevice(
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
