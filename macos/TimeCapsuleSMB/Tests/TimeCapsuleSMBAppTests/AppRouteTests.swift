import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AppRouteTests: XCTestCase {
    func testNavigationHelpersSetSingleRoute() async throws {
        let fixture = try await makeFixture()
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )

        fixture.appStore.select(profile)
        XCTAssertEqual(fixture.appStore.route, .device(profile.id))
        XCTAssertEqual(fixture.appStore.selectedDeviceID, profile.id)
        XCTAssertFalse(fixture.appStore.showingAddDevice)
        XCTAssertFalse(fixture.appStore.showingActivity)
        XCTAssertFalse(fixture.appStore.showingAppSettings)

        fixture.appStore.showAddDevice()
        XCTAssertEqual(fixture.appStore.route, .addDevice)
        XCTAssertNil(fixture.appStore.selectedDeviceID)
        XCTAssertTrue(fixture.appStore.showingAddDevice)

        fixture.appStore.showActivity()
        XCTAssertEqual(fixture.appStore.route, .activity)
        XCTAssertTrue(fixture.appStore.showingActivity)
        XCTAssertFalse(fixture.appStore.showingAddDevice)

        fixture.appStore.showAppSettings()
        XCTAssertEqual(fixture.appStore.route, .appSettings)
        XCTAssertTrue(fixture.appStore.showingAppSettings)
        XCTAssertFalse(fixture.appStore.showingActivity)

        fixture.appStore.showAllDevices()
        XCTAssertEqual(fixture.appStore.route, .allDevices)
        XCTAssertNil(fixture.appStore.selectedDeviceID)
    }

    func testAllDevicesRouteDoesNotAutoSelectWhenProfilesChange() async throws {
        let fixture = try await makeFixture()
        fixture.appStore.showAllDevices()

        _ = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )

        XCTAssertEqual(fixture.appStore.route, .allDevices)
        XCTAssertNil(fixture.appStore.selectedProfile)
    }

    func testMissingDeviceRouteNormalizesToFirstProfileOrAllDevices() async throws {
        let empty = try await makeFixture()
        empty.appStore.navigate(to: .device("missing"))
        XCTAssertEqual(empty.appStore.route, .allDevices)

        let fixture = try await makeFixture()
        _ = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        _ = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        fixture.appStore.navigate(to: .device("missing"))

        let firstStoredProfile = try XCTUnwrap(fixture.registry.profiles.first)
        XCTAssertEqual(fixture.appStore.route, .device(firstStoredProfile.id))
        XCTAssertEqual(fixture.appStore.selectedProfile?.id, firstStoredProfile.id)
    }

    func testDeletingSelectedProfileRoutesToFirstRemainingProfileOrAllDevices() async throws {
        let fixture = try await makeFixture()
        let first = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )
        fixture.appStore.select(first)

        try await fixture.appStore.forget(first)

        XCTAssertEqual(fixture.appStore.route, .device(second.id))
        XCTAssertEqual(fixture.appStore.selectedProfile?.id, second.id)

        try await fixture.appStore.forget(second)

        XCTAssertEqual(fixture.appStore.route, .allDevices)
        XCTAssertNil(fixture.appStore.selectedProfile)
    }

    func testDiagnosticsSelectedProfileFollowsDeviceRouteOnly() async throws {
        let fixture = try await makeFixture()
        let profile = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )

        fixture.appStore.select(profile)
        XCTAssertEqual(fixture.appStore.diagnosticsExportContext().selectedProfile?.id, profile.id)

        fixture.appStore.showAllDevices()
        XCTAssertNil(fixture.appStore.diagnosticsExportContext().selectedProfile)
    }

    private func makeFixture() async throws -> (
        appStore: AppStore,
        registry: DeviceRegistryStore
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let appStore = AppStore(
            appReadinessStore: AppReadinessStore(backend: coordinator.backend),
            deviceRegistry: registry,
            operationCoordinator: coordinator,
            passwordStore: InMemoryPasswordStore()
        )
        return (appStore, registry)
    }
}
