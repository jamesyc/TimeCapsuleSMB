import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ActivityStoreTests: XCTestCase {
    func testActivitySnapshotTracksActiveOperationTimelineAndDevice() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "deploy",
                    stage: "upload_payload",
                    description: "Upload managed Samba payload files."
                ),
                BackendEvent(
                    type: "result",
                    operation: "deploy",
                    ok: true,
                    payload: testSummaryPayload("Deployment completed.", key: "deploy_completed")
                )
            ], pauseBeforeEvents: true)
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        XCTAssertEqual(activity.snapshot.operationTitle, "No active operation")

        _ = coordinator.run(operation: "deploy", context: context, activeDeviceID: "device-one")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "Install / Update")
        XCTAssertEqual(activity.snapshot.scope, .device("device-one"))

        runner.finishAll()
        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.timeline.map(\.title), ["Upload Payload", "Done"])
        XCTAssertEqual(activity.snapshot.latestMessage, "Samba installation or update completed.")
    }

    func testFailedOperationShowsTheTranslatedFailureInTheActivityStrip() async throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .german)

        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "deploy", stage: "upload_payload"),
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "remote_error",
                    message: "No AirPort ACP service responded at 10.0.1.1:5009.",
                    recovery: recoveryValue(title: "The device reported an error", actions: [], localizationKey: "remote_error")
                )
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        _ = coordinator.run(operation: "deploy", context: context, activeDeviceID: "device-one")

        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.latestMessage, "Installieren / Aktualisieren: Das Gerät hat einen Fehler gemeldet")
    }

    func testAppLanguageChangeRefreshesCachedActivityPresentation() async throws {
        let originalLanguage = L10n.currentLanguage
        defer { L10n.apply(language: originalLanguage) }
        L10n.apply(language: .simplifiedChinese)

        let temp = try TemporaryDirectory()
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "deploy",
                    stage: "upload_payload",
                    description: "Upload managed Samba payload files."
                ),
                BackendEvent(
                    type: "result",
                    operation: "deploy",
                    ok: true,
                    payload: testSummaryPayload("Deployment completed.", key: "deploy_completed")
                )
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let settingsStore = AppSettingsStore(settingsURL: temp.url.appendingPathComponent("settings.json"))
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.appLane.backend),
            appSettingsStore: settingsStore,
            deviceRegistry: DeviceRegistryStore(applicationSupportURL: temp.url),
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore(),
            activityStore: activity
        )

        _ = coordinator.run(
            operation: "deploy",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.operationTitle, "安装 / 更新")
        XCTAssertEqual(activity.snapshot.timeline.map(\.title), ["上传安装文件", "完成"])

        var settings = AppSettings.default
        settings.language = .english
        try await appStore.saveAppSettings(settings)

        XCTAssertEqual(activity.snapshot.operationTitle, "Install / Update")
        XCTAssertEqual(activity.snapshot.timeline.map(\.title), ["Upload Payload", "Done"])
    }

    func testActivitySnapshotTracksBackendOnlyReadinessOperationAsAppScoped() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "capabilities",
                    stage: "start",
                    description: "Inspect helper capabilities."
                ),
                BackendEvent(
                    type: "result",
                    operation: "capabilities",
                    ok: true,
                    payload: .object(["schema_version": .number(1)])
                )
            ], pauseBeforeEvents: true)
        ])
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let activity = ActivityStore(coordinator: coordinator)

        backend.run(operation: "capabilities")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "App Readiness")
        XCTAssertEqual(activity.snapshot.scope, .app)

        runner.finishAll()
        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.timeline.count == 2 }
        XCTAssertEqual(activity.snapshot.scope, .app)
        XCTAssertEqual(activity.snapshot.operationTitle, "App Readiness")
    }

    func testActivitySnapshotTracksDiscoveryAsAppScoped() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "discover",
                    stage: "bonjour_discovery",
                    description: "Browse for AirPort Bonjour services."
                ),
                BackendEvent(
                    type: "result",
                    operation: "discover",
                    ok: true,
                    payload: testDiscoverPayload(records: [])
                )
            ], pauseBeforeEvents: true)
        ])
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let activity = ActivityStore(coordinator: coordinator)

        backend.run(operation: "discover")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "Discovery")
        XCTAssertEqual(activity.snapshot.scope, .app)
        runner.finishAll()
        try await waitUntilStoreState { !activity.snapshot.isRunning }
    }

    func testActivityStoreTracksMultipleActiveLanesAndPrefersDeviceSnapshot() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(
                        type: "result",
                        operation: "discover",
                        ok: true,
                        payload: testDiscoverPayload(records: [])
                    )
                ], pauseBeforeEvents: true)
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(
                        type: "stage",
                        operation: "doctor",
                        stage: "run_checks",
                        description: "Run local and remote diagnostic checks."
                    ),
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: testDoctorPayload(checks: [
                        testDoctorCheck(status: "PASS", message: "ok", domain: "device")
                    ]))
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(operation: "doctor", context: context, activeDeviceID: "device-one", laneKey: .device("device-one"))

        try await waitUntilStoreState {
            activity.laneSnapshots.count == 2 && activity.laneSnapshots.allSatisfy { $0.snapshot.isRunning }
        }
        XCTAssertEqual(activity.snapshot.scope, .device("device-one"))
        XCTAssertEqual(activity.snapshot.operationTitle, "Checkup")
        XCTAssertEqual(Set(activity.laneSnapshots.map(\.laneKey)), [.app, .device("device-one")])
        runner.finishAll()
    }

    func testCompactStatusPrefersSelectedDeviceOverRunningStartupDiscovery() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], pauseBeforeEvents: true)
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(
                        type: "stage",
                        operation: "doctor",
                        stage: "run_checks",
                        description: "Run local and remote diagnostic checks."
                    ),
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.count == 2
        }

        let status = activity.compactStatus(for: ActivityDisplayContext(
            selectedDeviceID: "device-one",
            showingAddDevice: false,
            showingActivity: false
        ))
        XCTAssertEqual(status.scope, .device("device-one"))
        XCTAssertEqual(status.operationTitle, "Checkup")
        XCTAssertEqual(status.activeLaneCount, 2)
        runner.finishAll()
    }

    func testCompactStatusShowsMultipleActiveOperationsWhenNoSelectedLaneCanOwnTheBar() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], pauseBeforeEvents: true)
            ],
            .init("deploy", profileID: "device-two"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "deploy", ok: true, payload: .object(["summary": .string("done")]))
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )
        coordinator.run(
            operation: "deploy",
            context: context("device-two"),
            activeDeviceID: "device-two",
            laneKey: .device("device-two")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.count == 2
        }

        let status = activity.compactStatus(for: .none)
        XCTAssertEqual(status.scope, .unknown)
        XCTAssertEqual(status.operationTitle, "2 active operations")
        XCTAssertEqual(status.latestMessage, "Open Activity for details.")
        XCTAssertEqual(status.activeLaneCount, 2)
        runner.finishAll()
    }

    func testCompactStatusShowsMultipleActiveOperationsOnActivityScreenEvenWhenAppLaneRuns() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], pauseBeforeEvents: true)
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.count == 2
        }

        let status = activity.compactStatus(for: ActivityDisplayContext(
            selectedDeviceID: nil,
            showingAddDevice: false,
            showingActivity: true
        ))
        XCTAssertEqual(status.scope, .unknown)
        XCTAssertEqual(status.operationTitle, "2 active operations")
        runner.finishAll()
    }

    func testCompactStatusShowsSelectedPendingConfirmationBeforeRunningDiscovery() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], pauseBeforeEvents: true)
            ],
            .init("deploy", profileID: "device-one"): [
                .init(events: [
                    confirmationRequiredEvent(operation: "deploy", id: "confirm-deploy")
                ])
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "deploy",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.contains { $0.laneKey == .app && $0.snapshot.isRunning }
                && activity.activeLaneSnapshots.contains { $0.laneKey == .device("device-one") && $0.isPendingConfirmation }
        }

        let status = activity.compactStatus(for: ActivityDisplayContext(
            selectedDeviceID: "device-one",
            showingAddDevice: false,
            showingActivity: false
        ))
        XCTAssertEqual(status.scope, .device("device-one"))
        XCTAssertEqual(status.operationTitle, "Install / Update")
        XCTAssertTrue(status.requiresAttention)
        XCTAssertFalse(status.isRunning)
        runner.finishAll()
    }

    func testCompactStatusUsesAppLaneForAddDeviceDiscoveryUnlessConfigureIsActive() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ], pauseBeforeEvents: true)
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], pauseBeforeEvents: true)
            ],
            .init("configure", profileID: "device-two"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "configure", ok: true, payload: .object(["summary": .string("configured")]))
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let addDeviceContext = ActivityDisplayContext(
            selectedDeviceID: nil,
            showingAddDevice: true,
            showingActivity: false
        )

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.count == 2
        }
        XCTAssertEqual(activity.compactStatus(for: addDeviceContext).scope, .app)
        XCTAssertEqual(activity.compactStatus(for: addDeviceContext).operationTitle, "Discovery")

        coordinator.run(
            operation: "configure",
            context: context("device-two"),
            activeDeviceID: "device-two",
            laneKey: .device("device-two")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.contains { $0.laneKey == .device("device-two") }
        }
        let status = activity.compactStatus(for: addDeviceContext)
        XCTAssertEqual(status.scope, .device("device-two"))
        XCTAssertEqual(status.operationTitle, "Add Device")
        runner.finishAll()
    }

    func testCompactStatusKeepsSelectedDeviceHistoryAfterStartupDiscoveryCompletes() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ])
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ])
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.isEmpty && activity.laneSnapshots.count == 2
        }

        let status = activity.compactStatus(for: ActivityDisplayContext(
            selectedDeviceID: "device-one",
            showingAddDevice: false,
            showingActivity: false
        ))
        XCTAssertEqual(status.scope, .device("device-one"))
        XCTAssertEqual(status.operationTitle, "Checkup")
        XCTAssertEqual(status.latestMessage, "Doctor checks passed.")
        XCTAssertEqual(status.latestTimelineTitle, "Done")
    }

    func testActivityStoreSeparatesActiveAndRecentLaneSnapshots() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("discover"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
                ])
            ],
            .init("doctor", profileID: "device-one"): [
                .init(events: [
                    BackendEvent(type: "result", operation: "doctor", ok: true, payload: doctorPayload())
                ], pauseBeforeEvents: true)
            ]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)

        coordinator.run(operation: "discover", laneKey: .app)
        coordinator.run(
            operation: "doctor",
            context: context("device-one"),
            activeDeviceID: "device-one",
            laneKey: .device("device-one")
        )

        try await waitUntilStoreState {
            activity.activeLaneSnapshots.map(\.laneKey) == [.device("device-one")]
                && activity.recentLaneSnapshots.map(\.laneKey) == [.app]
        }
        runner.finishAll()
    }

    func testSuccessfulAppValidationPresentsAppReadyWithoutDetailMessage() async throws {
        let runner = PausingStoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "stage",
                    operation: "validate-install",
                    stage: "validate_install",
                    description: "Validate local helper and artifact prerequisites."
                ),
                BackendEvent(
                    type: "result",
                    operation: "validate-install",
                    ok: true,
                    payload: .object(["summary": .string("Install validation passed.")])
                )
            ], pauseBeforeEvents: true)
        ])
        let backend = BackendClient(runner: runner)
        let coordinator = OperationCoordinator(backend: backend)
        let activity = ActivityStore(coordinator: coordinator)

        backend.run(operation: "validate-install")

        try await waitUntilStoreState { activity.snapshot.isRunning }
        XCTAssertEqual(activity.snapshot.operationTitle, "App Readiness")
        XCTAssertEqual(activity.snapshot.scope, .app)

        runner.finishAll()
        try await waitUntilStoreState { !activity.snapshot.isRunning && activity.snapshot.operationTitle == "App Ready" }
        XCTAssertEqual(activity.snapshot.scope, .app)
        XCTAssertNil(activity.snapshot.latestMessage)
    }

    func testActivityLeavesOutTheBackgroundSSHCheck() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh", profileID: "device-one"): [.init(events: [
                BackendEvent(type: "stage", operation: "set-ssh", stage: "probe_ssh"),
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ], pauseAfterEvents: true)]
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let activity = ActivityStore(coordinator: coordinator)
        let profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
        let selected = ActivityDisplayContext(selectedDeviceID: profile.id, showingAddDevice: false, showingActivity: false)
        let lane = coordinator.lane(for: .deviceWorkflow(profile.id, .sshStatus))

        DeviceSSHAccessStore(coordinator: coordinator).refresh(profile: profile)
        // Running, with a timeline of its own.
        try await waitUntilStoreState { lane.backend.events.count == 2 && lane.backend.isRunning }
        activity.refresh()
        XCTAssertTrue(activity.laneSnapshots.isEmpty)
        XCTAssertFalse(activity.hasActiveActivity)
        XCTAssertFalse(activity.compactStatus(for: selected).isRunning)
        XCTAssertEqual(activity.compactStatus(for: selected).operationTitle, L10n.string("activity.no_active_operation"))

        runner.finishAll()
        try await waitUntilStoreState { !lane.backend.isRunning }
        activity.refresh()
        XCTAssertTrue(activity.laneSnapshots.isEmpty)
        XCTAssertEqual(activity.compactStatus(for: selected).operationTitle, L10n.string("activity.no_active_operation"))
    }

    private func context(_ profileID: String) -> DeviceRuntimeContext {
        DeviceRuntimeContext(
            profileID: profileID,
            configURL: URL(fileURLWithPath: "/tmp/\(profileID)/.env")
        )
    }

    private func doctorPayload() -> JSONValue {
        testDoctorPayload(checks: [
            testDoctorCheck(status: "PASS", message: "smbd is running", domain: "device")
        ])
    }

    private func confirmationRequiredEvent(operation: String, id: String) -> BackendEvent {
        BackendEvent(
            type: "error",
            operation: operation,
            code: "confirmation_required",
            message: "Confirm operation.",
            details: .object([
                "title": .string("Confirm operation"),
                "message": .string("Continue."),
                "action_title": .string("Continue"),
                "confirmation_id": .string(id)
            ])
        )
    }
}
