import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ContentViewSmokeTests: XCTestCase {
    func testRendersEmptyShellTopLevelRoutes() async throws {
        let fixture = try await AppViewFixture()
        for route in [AppRoute.allDevices, .activity, .appSettings, .addDevice] {
            fixture.appStore.navigate(to: route)
            try assertRendersNonBlank(fixture.contentView, minimumDistinctPixelCount: 4)
        }
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
