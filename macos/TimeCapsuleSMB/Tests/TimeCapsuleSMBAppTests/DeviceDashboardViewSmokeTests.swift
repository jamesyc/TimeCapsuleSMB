import SwiftUI
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceDashboardViewSmokeTests: XCTestCase {
    func testRendersEveryDashboardTabInIdleState() async throws {
        let fixture = try await AppViewFixture()
        let profile = try await fixture.saveProfile(id: "device-one")
        let session = fixture.dashboardSession(for: profile)

        for tab in DeviceDashboardTab.allCases {
            session.selectedTab = tab
            try assertRendersNonBlank(dashboardView(fixture: fixture, profile: profile, session: session))
        }
    }

    func testRendersInstallPlanningPlanReadyDeployingConfirmationFailedAndCompletedStates() async throws {
        try await renderInstallState(
            responses: [
                .init(
                    events: [BackendEvent(type: "stage", operation: "deploy", stage: "build_deployment_plan")],
                    delayNanoseconds: 250_000_000
                )
            ],
            expectedState: .planning,
            runDeploy: false
        )
        try await renderInstallState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
                ])
            ],
            expectedState: .planReady,
            runDeploy: false
        )
        try await renderInstallState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
                ]),
                .init(
                    events: [BackendEvent(type: "stage", operation: "deploy", stage: "upload_smbd")],
                    delayNanoseconds: 250_000_000
                )
            ],
            expectedState: .deploying,
            runDeploy: true
        )
        try await renderInstallState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
                ]),
                .init(events: [
                    BackendEvent(
                        type: "error",
                        operation: "deploy",
                        code: "confirmation_required",
                        message: "Deployment needs confirmation."
                    )
                ])
            ],
            expectedState: .awaitingConfirmation,
            runDeploy: true
        )
        try await renderInstallState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
                ]),
                .init(events: [
                    BackendEvent(type: "error", operation: "deploy", code: "remote_error", message: "Upload failed.")
                ])
            ],
            expectedState: .deployFailed,
            runDeploy: true
        )
        try await renderInstallState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployPlanPayload())
                ]),
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: testDeployResultPayload())
                ])
            ],
            expectedState: .deployed,
            runDeploy: true
        )
    }

    func testRendersCheckupRunningPassedWarningFailedAndRunFailedStates() async throws {
        try await renderCheckupState(
            responses: [
                .init(
                    events: [BackendEvent(type: "stage", operation: "doctor", stage: "run_checks")],
                    delayNanoseconds: 250_000_000
                )
            ],
            expectedState: .running
        )
        try await renderCheckupState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                        testDoctorCheck(status: "PASS", message: "smbd is running", domain: "Runtime")
                    ]))
                ])
            ],
            expectedState: .passed
        )
        try await renderCheckupState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                        testDoctorCheck(status: "WARN", message: "SMB needs attention", domain: "Runtime")
                    ]))
                ])
            ],
            expectedState: .warning
        )
        try await renderCheckupState(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: false, payload: testDoctorPayload(fatal: true, checks: [
                        testDoctorCheck(status: "FAIL", message: "smbd is not running", domain: "Runtime")
                    ]))
                ])
            ],
            expectedState: .failed
        )
        try await renderCheckupState(
            responses: [
                .init(events: [
                    BackendEvent(type: "error", operation: "doctor", code: "auth_failed", message: "Password rejected.")
                ])
            ],
            expectedState: .runFailed
        )
    }

    func testRendersMaintenanceWorkflowIdleAndResultStates() async throws {
        let idleFixture = try await AppViewFixture()
        let idleProfile = try await idleFixture.saveProfile(id: "device-one")
        let idleSession = idleFixture.dashboardSession(for: idleProfile)
        idleSession.selectedTab = .maintenance
        for workflow in MaintenanceWorkflow.allCases {
            idleSession.maintenanceStore.selectedWorkflow = workflow
            try assertRendersNonBlank(dashboardView(fixture: idleFixture, profile: idleProfile, session: idleSession))
        }

        let activation = try await AppViewFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "activate", ok: true, payload: testActivationPlanPayload())
            ])
        ])
        let activationProfile = try await activation.saveProfile(id: "activation-device")
        let activationSession = activation.dashboardSession(for: activationProfile)
        activationSession.maintenanceStore.planActivation(password: "pw", profile: activationProfile)
        try await waitUntilStoreState { activationSession.maintenanceStore.activateState == .planReady }
        activationSession.selectedTab = .maintenance
        try assertRendersNonBlank(dashboardView(fixture: activation, profile: activationProfile, session: activationSession))

        let fsck = try await AppViewFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "fsck", ok: true, payload: testFsckListPayload(targets: [
                    testFsckTargetPayload(name: "Data")
                ]))
            ])
        ])
        let fsckProfile = try await fsck.saveProfile(id: "fsck-device")
        let fsckSession = fsck.dashboardSession(for: fsckProfile)
        fsckSession.maintenanceStore.refreshFsckTargets(password: "pw", profile: fsckProfile)
        try await waitUntilStoreState { fsckSession.maintenanceStore.fsckState == .listReady }
        fsckSession.selectedTab = .maintenance
        try assertRendersNonBlank(dashboardView(fixture: fsck, profile: fsckProfile, session: fsckSession))

        let repair = try await AppViewFixture(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "repair-xattrs", ok: true, payload: testRepairXattrsPayload(findings: 2, repairable: 1))
            ])
        ])
        let repairProfile = try await repair.saveProfile(id: "repair-device")
        let repairSession = repair.dashboardSession(for: repairProfile)
        repairSession.maintenanceStore.repairPath = "/Volumes/Data"
        repairSession.maintenanceStore.scanRepairXattrs()
        try await waitUntilStoreState { repairSession.maintenanceStore.repairState == .scanReady }
        repairSession.selectedTab = .maintenance
        try assertRendersNonBlank(dashboardView(fixture: repair, profile: repairProfile, session: repairSession))
    }

    func testRendersSettingsPasswordReplacementAttention() async throws {
        let fixture = try await AppViewFixture()
        let profile = try await fixture.saveProfile(id: "device-one", passwordState: .missing, password: nil)
        let session = fixture.dashboardSession(for: profile)

        session.runCheckup(profile: profile)

        XCTAssertEqual(session.selectedTab, .settings)
        try assertRendersNonBlank(dashboardView(fixture: fixture, profile: profile, session: session))
    }

    private func renderInstallState(
        responses: [StoreTestRunner.Response],
        expectedState: DeployWorkflowState,
        runDeploy: Bool
    ) async throws {
        let fixture = try await AppViewFixture(responses: responses)
        let profile = try await fixture.saveProfile(id: "device-one")
        let session = fixture.dashboardSession(for: profile)

        session.runInstallPlan(profile: profile)
        if runDeploy {
            try await waitUntilStoreState { session.deployStore.state == .planReady }
            session.runInstall(profile: profile)
        }
        if expectedState != .planning && expectedState != .deploying {
            try await waitUntilStoreState { session.deployStore.state == expectedState }
        }

        XCTAssertEqual(session.deployStore.state, expectedState)
        try assertRendersNonBlank(dashboardView(fixture: fixture, profile: profile, session: session))
    }

    private func renderCheckupState(
        responses: [StoreTestRunner.Response],
        expectedState: DoctorWorkflowState
    ) async throws {
        let fixture = try await AppViewFixture(responses: responses)
        let profile = try await fixture.saveProfile(id: "device-one")
        let session = fixture.dashboardSession(for: profile)

        session.runCheckup(profile: profile)
        if expectedState != .running {
            try await waitUntilStoreState { session.doctorStore.state == expectedState }
        }

        XCTAssertEqual(session.doctorStore.state, expectedState)
        try assertRendersNonBlank(dashboardView(fixture: fixture, profile: profile, session: session))
    }

    private func dashboardView(
        fixture: AppViewFixture,
        profile: DeviceProfile,
        session: DeviceDashboardSession
    ) -> some View {
        DeviceDashboardView(
            profile: profile,
            session: session,
            appStore: fixture.appStore,
            appSettingsStore: fixture.appStore.appSettingsStore,
            reachabilityStore: fixture.appStore.reachabilityStore,
            operationCoordinator: fixture.appStore.operationCoordinator,
            backend: fixture.appStore.backend,
            showDiagnostics: {}
        )
    }
}
