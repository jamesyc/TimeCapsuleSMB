import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ContentViewSmokeTests: XCTestCase {
    func testIdentityConflictRecoveryOpensSavedDeviceAndForgetsRedundantEntry() async throws {
        let fixture = try await AppViewFixture()
        let first = try await fixture.saveProfile(id: "known", host: "root@192.0.2.10")
        let second = try await fixture.saveProfile(id: "alias", host: "root@fd00::10")
        let registry = fixture.appStore.deviceRegistry
        let snapshot = DeviceCheckupSnapshot(checkedAt: Date(timeIntervalSince1970: 1000), state: .passed,
            passCount: 1, warnCount: 0, failCount: 0, summary: "passed")
        await registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: "02:aa:bb:cc:dd:ee", for: first.id)
        await registry.updateCheckup(snapshot, runtimeState: nil, airportMAC: "02:aa:bb:cc:dd:ee", for: second.id)
        XCTAssertEqual(registry.identityErrorMessage, L10n.format("discovery.identity_owned", first.title))
        let recoveryProfile = try XCTUnwrap(registry.conflictingProfile)
        fixture.appStore.select(recoveryProfile)
        XCTAssertEqual(fixture.appStore.route, .device(first.id))
        try assertRendersNonBlank(fixture.contentView)
        try await fixture.appStore.forget(second)
        XCTAssertEqual(registry.profiles.map(\.id), [first.id])
        XCTAssertNil(registry.identityErrorMessage)
        XCTAssertNil(registry.conflictingProfile)
        XCTAssertEqual(registry.profile(id: first.id)?.network.airportMAC, "02:aa:bb:cc:dd:ee")
    }

    func testReconnectFeedbackRendersInEverySupportedLanguage() async throws {
        let fixture = try await AppViewFixture(responses: [.init(events: [
            BackendEvent(type: "result", operation: "discover", ok: true,
                payload: testDiscoverPayload(records: [testDeviceRecord(ipv4: ["10.0.0.9"])]))
        ])], discoveryWaitsForReadiness: false)
        let profile = try await fixture.registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: DiscoveredDevice(record: try testDeviceRecord().decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available, preferredID: "reconnect")
        let flow = fixture.composition.addDeviceStore
        flow.runDiscover()
        try await waitUntilStoreState { flow.state == .discoveryReady }
        flow.reconnectSuggestedProfile()
        let previous = L10n.currentLanguage
        defer { L10n.apply(language: previous) }
        for language in AppLanguage.allCases where language != .system {
            L10n.apply(language: language)
            let message = try XCTUnwrap(flow.reconnectMessage)
            XCTAssertTrue(message.contains(profile.title))
            XCTAssertNotEqual(message, "discovery.reconnect_selected")
            try assertRendersNonBlank(AddDeviceView(store: flow), size: CGSize(width: 720, height: 650))
        }
    }

    func testRendersEmptyShellTopLevelRoutes() async throws {
        let fixture = try await AppViewFixture()
        for route in [AppRoute.allDevices, .activity, .appSettings, .addDevice] {
            fixture.appStore.navigate(to: route)
            try assertRendersNonBlank(fixture.contentView, minimumDistinctPixelCount: 4)
        }
    }

    func testRendersOverviewWithUnsupportedDiscoveredDevice() async throws {
        let fixture = try await AppViewFixture(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [], devices: [
                        testDiscoveredDevice(name: "Office Capsule", host: "10.0.0.2", supportedModel: true),
                        testDiscoveredDevice(
                            id: "bonjour:express",
                            name: "Living Room Express",
                            host: "10.0.0.40",
                            hostname: "express.local.",
                            syap: "115",
                            supportedModel: false,
                            fullname: "Living Room Express._airport._tcp.local."
                        )
                    ]))
                ])
            ],
            discoveryWaitsForReadiness: false
        )
        fixture.appStore.deviceDiscovery.refresh(timeout: 6)
        try await waitUntilStoreState { fixture.appStore.deviceDiscovery.state == .ready }
        XCTAssertEqual(fixture.appStore.deviceDiscovery.unsavedDevices.filter(\.isUnsupportedModel).count, 1)
        fixture.appStore.navigate(to: .allDevices)

        try assertRendersNonBlank(fixture.contentView)
    }

    func testRendersDeviceDashboardRoute() async throws {
        let fixture = try await AppViewFixture()
        let profile = try await fixture.saveProfile(id: "device-one")
        fixture.appStore.select(profile)

        try assertRendersNonBlank(fixture.contentView)
    }

    func testRendersAfterSelectedDeviceIsDeleted() async throws {
        let fixture = try await AppViewFixture()
        let first = try await fixture.saveProfile(id: "device-one", host: "root@10.0.0.2")
        let second = try await fixture.saveProfile(id: "device-two", host: "root@10.0.0.3")
        fixture.appStore.select(first)

        try await fixture.appStore.forget(first)

        XCTAssertEqual(fixture.appStore.route, .device(second.id))
        try assertRendersNonBlank(fixture.contentView)
    }

    func testRendersReadinessBlockedSurface() async throws {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = StoreTestRunner(responses: [])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let readiness = AppReadinessStore(
            backend: coordinator.backend,
            runtimeResolver: BlockingRuntimeResolver(),
            helperPathProvider: { "" }
        )
        let appStore = AppStore(
            appReadinessStore: readiness,
            appSettingsStore: AppSettingsStore(settingsURL: temp.url.appendingPathComponent("app-settings.json")),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore()
        )
        let composition = AppViewComposition(appStore: appStore)

        readiness.start()

        guard case .blocked = readiness.state else {
            return XCTFail("Expected readiness to be blocked.")
        }
        try assertRendersNonBlank(ContentView(composition: composition, startsAutomatically: false))
    }

    func testRendersWithPendingConfirmation() async throws {
        let fixture = try await AppViewFixture(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "deploy",
                    code: "confirmation_required",
                    message: "Continue install?",
                    details: .object([
                        "title": .string("Continue install?"),
                        "message": .string("Deploy TimeCapsuleSMB now."),
                        "action_title": .string("Deploy"),
                        "confirmation_id": .string("confirm-123")
                    ])
                )
            ])
        ])
        let profile = try await fixture.saveProfile(id: "device-one")
        _ = fixture.appStore.operationCoordinator.run(
            operation: "deploy",
            params: ["dry_run": .bool(false)],
            profile: profile
        )
        try await waitUntilStoreState {
            fixture.appStore.operationCoordinator.pendingConfirmation != nil
        }

        fixture.appStore.select(profile)

        XCTAssertNotNil(fixture.appStore.operationCoordinator.pendingConfirmation)
        try assertRendersNonBlank(fixture.contentView)
    }
}

private struct BlockingRuntimeResolver: AppRuntimeResolving {
    func resolve(helperPath: String?) throws -> HelperResolution {
        HelperResolution(
            executableURL: URL(fileURLWithPath: "/tmp/tcapsule"),
            distributionRootURL: nil,
            toolsBinURL: nil,
            mode: .developmentCheckout,
            attemptedPaths: []
        )
    }

    func runtimeIssues(for resolution: HelperResolution) -> [BundleRuntimeIssue] {
        [
            BundleRuntimeIssue(
                code: .helperMissing,
                severity: .error,
                message: "Test helper is missing."
            )
        ]
    }
}
