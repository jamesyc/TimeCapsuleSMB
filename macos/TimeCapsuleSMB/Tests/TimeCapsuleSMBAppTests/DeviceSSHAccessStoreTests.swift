import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceSSHAccessStoreTests: XCTestCase {
    func testRefreshRunsSSHAccessStatusAndStoresSnapshot() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "stage", operation: "set-ssh", stage: "probe_ssh"),
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceSSHAccessStore(coordinator: coordinator, now: { Date(timeIntervalSince1970: 123) })
        let profile = try makeProfile(host: "10.0.0.2")

        store.refresh(profile: profile)
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }

        XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh"])
        XCTAssertEqual(runner.calls[0].context?.profileID, profile.id)
        XCTAssertEqual(runner.calls[0].params["action"], .string("status"))
        XCTAssertEqual(store.snapshot(for: profile)?.payload.isSSHDisabledLikely, true)
        XCTAssertEqual(store.snapshot(for: profile)?.refreshedAt, Date(timeIntervalSince1970: 123))
        XCTAssertNil(store.error(for: profile))
    }

    func testNoticeAppearsOnlyWhenACPReachableAndSSHClosed() async throws {
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceSSHAccessStore(coordinator: coordinator)
        let profile = try makeProfile(host: "10.0.0.2")

        store.refresh(profile: profile)
        try await waitUntilStoreState { store.snapshot(for: profile) != nil }

        let notice = store.notice(for: profile, staleEndpointNotice: nil)
        XCTAssertEqual(notice?.profileID, profile.id)
        XCTAssertEqual(notice?.host, "10.0.0.2")
        let discoveredDevice = DiscoveredDevice(
            payload: try testDiscoveredDevice(host: "10.0.0.3").decode(DiscoveredDevicePayload.self),
            index: 0
        )
        XCTAssertNil(store.notice(for: profile, staleEndpointNotice: StaleEndpointNotice(
            profileID: profile.id,
            deviceName: profile.title,
            configuredHost: "10.0.0.2",
            currentHost: "10.0.0.3",
            discoveredDevice: discoveredDevice
        )))
    }

    func testAutomaticRefreshSkipsRecentSnapshot() async throws {
        var now = Date(timeIntervalSince1970: 100)
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload())
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ])
        ])
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let store = DeviceSSHAccessStore(coordinator: coordinator, now: { now })
        let profile = try makeProfile(host: "10.0.0.2")

        store.refreshIfNeeded(profile: profile)
        try await waitUntilStoreState { runner.calls.count == 1 && store.snapshot(for: profile) != nil }
        store.refreshIfNeeded(profile: profile)

        XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh"])

        now = Date(timeIntervalSince1970: 161)
        store.refreshIfNeeded(profile: profile)
        try await waitUntilStoreState { runner.calls.count == 2 }

        XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh", "set-ssh"])
    }

    func testApplyMaintenancePayloadUpdatesSnapshot() async throws {
        let coordinator = OperationCoordinator(backend: BackendClient(runner: StoreTestRunner(responses: [])))
        let store = DeviceSSHAccessStore(coordinator: coordinator, now: { Date(timeIntervalSince1970: 200) })
        let profile = try makeProfile(host: "10.0.0.2")
        let payload = try testSSHAccessPayload(sshPortReachable: true, summary: "SSH is reachable.").decode(SSHAccessPayload.self)

        store.apply(payload: payload, profile: profile)

        XCTAssertEqual(store.snapshot(for: profile)?.payload.sshPortReachable, true)
        XCTAssertEqual(store.snapshot(for: profile)?.refreshedAt, Date(timeIntervalSince1970: 200))
    }

    private func makeProfile(host: String = "10.0.0.2") throws -> DeviceProfile {
        DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: host),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
    }
}
