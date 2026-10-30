import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class ContentViewSmokeTests: XCTestCase {
    func testIdentityConflictRecoveryOpensSavedDeviceAndForgetsRedundantEntry() async throws {
        let fixture = try await AppViewFixture()
        let first = try await fixture.saveProfile(id: "known", host: "root@192.0.2.10")
        let second = try await fixture.saveProfile(id: "alias", host: "root@fd00::10")
        let registry = fixture.appStore.deviceRegistry
        // Operations against both entries report the same AirPort.
        let observation = try XCTUnwrap(AirportIdentityObservation(mac: "02:aa:bb:cc:dd:ee"))
        await registry.confirmAirportIdentity(observation, for: first.id)
        await registry.confirmAirportIdentity(observation, for: second.id)
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

    func testClickingASavedDeviceOpensItAndLeavesAddDeviceAsItWas() async throws {
        let office = testDeviceRecord(name: "Office Capsule", ipv4: ["10.0.0.2"], fullname: "Office Capsule._airport._tcp.local.")
        let den = testDeviceRecord(name: "Den Capsule", ipv4: ["10.0.0.3"], fullname: "Den Capsule._airport._tcp.local.")
        let fixture = try await AppViewFixture(responses: [testDiscoverResponse([office, den])], discoveryWaitsForReadiness: false)
        let existing = try await fixture.registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: DiscoveredDevice(record: try office.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available, preferredID: "office")
        let flow = fixture.composition.addDeviceStore
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        fixture.appStore.showAddDevice()
        flow.runDiscover()
        try await waitUntilStoreState { flow.state == .discoveryReady }
        let saved = try XCTUnwrap(flow.devices.first)
        let unsaved = try XCTUnwrap(flow.devices.last)
        flow.select(unsaved)
        flow.password = "secret"

        flow.select(saved)

        try await waitUntilStoreState { fixture.appStore.route == .device(existing.id) }
        // Nothing was saved, so its page has no Saved notice.
        XCTAssertNil(fixture.appStore.justSavedProfileID)
        XCTAssertFalse(devicePageTexts(fixture.contentView).contains(L10n.format("add_device.saved", existing.title)))
        XCTAssertNil(flow.profileToOpen)
        // The AirPort being added is still selected, with its password.
        XCTAssertEqual(flow.selectedDeviceID, unsaved.id)
        XCTAssertEqual(flow.password, "secret")
        XCTAssertEqual(flow.state, .passwordEntry)

        // Back on Add Device, the same click opens the device again.
        fixture.appStore.showAddDevice()
        flow.select(saved)
        try await waitUntilStoreState { fixture.appStore.route == .device(existing.id) }
        XCTAssertNil(fixture.appStore.justSavedProfileID)
        XCTAssertNil(flow.profileToOpen)
        XCTAssertEqual(flow.selectedDeviceID, unsaved.id)
    }

    func testEveryDeviceSwitchChecksSSHOnTheDevicePage() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh"): Array(repeating: .init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ]), count: 3)
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2")
        let den = try await fixture.saveProfile(id: "den", host: "root@10.0.0.3")
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        func checked() -> [String?] {
            runner.calls.filter { $0.operation == "set-ssh" }.map(\.context?.profileID)
        }

        fixture.appStore.navigate(to: .device(office.id))
        try await waitUntilStoreState { checked() == ["office"] }
        // Straight to the other device: SwiftUI keeps the same page view.
        fixture.appStore.navigate(to: .device(den.id))
        try await waitUntilStoreState { checked() == ["office", "den"] }
        // Away and back to the same device: only the page reappearing checks again.
        fixture.appStore.navigate(to: .allDevices)
        let allDevicesTitle = L10n.string("overview.saved_devices.title")
        try await waitUntilStoreState {
            accessibleElements(in: host.contentView).contains { $0.text == allDevicesTitle }
        }
        // SwiftUI removes the device page, ending its task, on the main-queue
        // turn after it draws All Devices; going back sooner keeps the page.
        await runQueuedStoreUpdates()
        fixture.appStore.navigate(to: .device(den.id))
        try await waitUntilStoreState { checked() == ["office", "den", "den"] }

        await runQueuedStoreUpdates()
        XCTAssertEqual(checked(), ["office", "den", "den"])
        XCTAssertEqual(runner.calls.filter { $0.operation == "set-ssh" }.map { $0.params["action"] }, Array(repeating: .string("status"), count: 3))
    }

    func testEachDevicePageChecksSSHThenReachabilityWithItsOwnPassword() async throws {
        let ssh = BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
        let reachable = BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload(
            installState: "current", installedReleaseTag: "v5.0.0", installedVersionCode: 50000, appReleaseTag: "v5.0.0"
        ))
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh"): Array(repeating: .init(events: [ssh]), count: 3),
            .init("reachability"): Array(repeating: .init(events: [reachable]), count: 2)
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2", password: "pw-office")
        let den = try await fixture.saveProfile(id: "den", host: "root@10.0.0.3", password: "pw-den")
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        func runs() -> [String] {
            runner.calls.map { "\($0.operation):\($0.context?.profileID ?? "-")" }
        }

        fixture.appStore.navigate(to: .device(office.id))
        try await waitUntilStoreState { runs() == ["set-ssh:office", "reachability:office"] }
        fixture.appStore.navigate(to: .device(den.id))
        try await waitUntilStoreState { runs().count == 4 }
        XCTAssertEqual(runs(), ["set-ssh:office", "reachability:office", "set-ssh:den", "reachability:den"])
        // Back within a minute: SSH again, reachability not.
        fixture.appStore.navigate(to: .allDevices)
        let allDevicesTitle = L10n.string("overview.saved_devices.title")
        try await waitUntilStoreState { accessibleElements(in: host.contentView).contains { $0.text == allDevicesTitle } }
        await runQueuedStoreUpdates()
        fixture.appStore.navigate(to: .device(den.id))
        try await waitUntilStoreState { runs().count == 5 && !fixture.appStore.operationCoordinator.lane(for: .deviceWorkflow(den.id, .sshStatus)).isBusy }
        await runQueuedStoreUpdates()

        XCTAssertEqual(runs(), ["set-ssh:office", "reachability:office", "set-ssh:den", "reachability:den", "set-ssh:den"])
        let passwords = runner.calls.filter { $0.operation == "reachability" }.map { $0.params["credentials"] }
        XCTAssertEqual(passwords, [.object(["password": .string("pw-office")]), .object(["password": .string("pw-den")])])
        // The page shows the version the login read.
        XCTAssertTrue(devicePageTexts(fixture.contentView).contains("v5.0.0"))
    }

    func testAPageWithoutAWorkingPasswordRunsNoAutomaticReachability() async throws {
        for state in [DevicePasswordState.missing, .invalid] {
            let runner = OperationKeyedStoreTestRunner(responses: [
                .init("set-ssh"): [.init(events: [
                    BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
                ])]
            ])
            let fixture = try await AppViewFixture(runner: runner)
            let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2",
                                                       passwordState: state, password: state == .missing ? nil : "pw")

            // What the page awaits on each visit; a check it starts holds its lane at once.
            await fixture.dashboardSession(for: office).refreshDeviceStatus(profile: office)

            let background = fixture.appStore.operationCoordinator.lane(for: .deviceWorkflow(office.id, .reachabilityStatus))
            XCTAssertFalse(background.isBusy, "\(state)")
            await runQueuedStoreUpdates()
            XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh"], "\(state)")
            XCTAssertNil(fixture.appStore.reachabilityStore.snapshot(for: office), "\(state)")
        }
    }

    func testThePageNeverPromptsForTheKeychain() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh"): [.init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ])]
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2")
        // Reading the saved password would show a keychain prompt.
        fixture.passwordStore.requiresAuthentication = true
        let readsBefore = fixture.passwordStore.passwordReadCount

        await fixture.dashboardSession(for: office).refreshDeviceStatus(profile: office)

        XCTAssertFalse(fixture.appStore.operationCoordinator.lane(for: .deviceWorkflow(office.id, .reachabilityStatus)).isBusy)
        XCTAssertEqual(fixture.passwordStore.passwordReadCount, readsBefore)
        await runQueuedStoreUpdates()
        XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh"])
    }

    func testReachabilityWaitsForTheSSHCheck() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh", profileID: "office"): [.init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ], pauseBeforeEvents: true)],
            .init("reachability"): [.init(events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ])]
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2")
        let host = LiveHostedView(fixture.contentView)
        defer { host.close(); runner.finishAll() }

        fixture.appStore.navigate(to: .device(office.id))
        try await waitUntilStoreState { runner.calls.count == 1 }
        await runQueuedStoreUpdates()
        // Started together, the device would refuse the second background check.
        XCTAssertEqual(runner.calls.map(\.operation), ["set-ssh"])

        runner.finish(.init("set-ssh", profileID: "office"))
        try await waitUntilStoreState { runner.calls.map(\.operation) == ["set-ssh", "reachability"] }
    }

    func testLeavingADeviceDuringItsSSHCheckStartsNoReachabilityForIt() async throws {
        let ssh = BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh", profileID: "office"): [.init(events: [ssh], pauseBeforeEvents: true)],
            .init("set-ssh", profileID: "den"): [.init(events: [ssh])],
            .init("reachability"): [.init(events: [
                BackendEvent(type: "result", operation: "reachability", ok: true, payload: testReachabilityPayload())
            ])]
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let office = try await fixture.saveProfile(id: "office", host: "root@10.0.0.2")
        let den = try await fixture.saveProfile(id: "den", host: "root@10.0.0.3")
        let host = LiveHostedView(fixture.contentView)
        defer { host.close(); runner.finishAll() }
        func runs() -> [String] {
            runner.calls.map { "\($0.operation):\($0.context?.profileID ?? "-")" }
        }

        fixture.appStore.navigate(to: .device(office.id))
        try await waitUntilStoreState { runs() == ["set-ssh:office"] }
        fixture.appStore.navigate(to: .device(den.id))
        try await waitUntilStoreState { runs().contains("reachability:den") }
        // Office's check ends after the switch: its page's task is gone.
        runner.finish(.init("set-ssh", profileID: "office"))
        try await waitUntilStoreState { !fixture.appStore.operationCoordinator.lane(for: .deviceWorkflow(office.id, .sshStatus)).isBusy }
        await runQueuedStoreUpdates()

        XCTAssertEqual(Set(runs()), ["set-ssh:office", "set-ssh:den", "reachability:den"])
    }

    func testABackgroundSSHCheckLeavesTheEnableSSHRunAlone() async throws {
        let sshResult = BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
        let fixture = try await AppViewFixture(responses: [
            .init(events: [BackendEvent(type: "stage", operation: "set-ssh", stage: "enable_ssh"), sshResult]),
            .init(events: [BackendEvent(type: "stage", operation: "set-ssh", stage: "probe_ssh"), sshResult])
        ])
        let profile = try await fixture.saveProfile(id: "office")
        let session = fixture.dashboardSession(for: profile)
        let coordinator = fixture.appStore.operationCoordinator

        XCTAssertNotNil(session.maintenanceStore.enableSSHAccess(password: "pw", profile: profile).operation)
        try await waitUntilStoreState {
            session.maintenanceStore.sshAccessState == .succeeded && !coordinator.isDeviceBusy(profile.id)
        }
        let enableRun = session.maintenanceStore.timelineEvents(for: .sshAccess).map(\.id)
        XCTAssertFalse(enableRun.isEmpty)

        // The device page's check, as on every visit.
        session.refreshSSHAccessStatus(profile: profile)
        try await waitUntilStoreState {
            (fixture.runner as! StoreTestRunner).calls.count == 2
                && !coordinator.lane(for: .deviceWorkflow(profile.id, .sshStatus)).isBusy
        }
        await runQueuedStoreUpdates()

        XCTAssertEqual((fixture.runner as! StoreTestRunner).calls.map { $0.params["action"] }, [.string("enable"), .string("status")])
        XCTAssertEqual(session.maintenanceStore.timelineEvents(for: .sshAccess).map(\.id), enableRun)
        XCTAssertEqual(session.maintenanceStore.sshAccessState, .succeeded)
        // Activity keeps the run the user started and leaves the check out.
        fixture.appStore.activityStore.refresh()
        XCTAssertEqual(fixture.appStore.activityStore.laneSnapshots.map(\.laneKey), [.deviceWorkflow(profile.id, .sshAccess)])
    }

    func testToolbarCancelLeavesABackgroundSSHCheckAlone() async throws {
        let runner = OperationKeyedStoreTestRunner(responses: [
            .init("set-ssh"): [.init(events: [
                BackendEvent(type: "result", operation: "set-ssh", ok: true, payload: testSSHAccessPayload(sshPortReachable: true))
            ], pauseBeforeEvents: true)]
        ])
        let fixture = try await AppViewFixture(runner: runner)
        let profile = try await fixture.saveProfile(id: "office")
        fixture.appStore.navigate(to: .device(profile.id))
        let coordinator = fixture.appStore.operationCoordinator

        fixture.appStore.sshAccessStore.refresh(profile: profile)
        XCTAssertTrue(coordinator.lane(for: .deviceWorkflow(profile.id, .sshStatus)).isBusy)
        XCTAssertFalse(coordinator.isDeviceBusy(profile.id))
        XCTAssertFalse(coordinator.canCancel)
        XCTAssertFalse(fixture.appStore.canCancelSelectedOperation)
        fixture.appStore.cancelSelectedOperation()
        // A gate exists only once its call is recorded; finishing earlier would be lost.
        try await waitUntilStoreState { runner.calls.count == 1 }
        runner.finishAll()

        // The check ran to its result rather than being cancelled.
        try await waitUntilStoreState { fixture.appStore.sshAccessStore.snapshot(for: profile) != nil }
        XCTAssertNil(fixture.appStore.sshAccessStore.error(for: profile))
    }

    func testSavingADeviceOpensItAndStartsAddDeviceOver() async throws {
        let office = testDeviceRecord(name: "Office Capsule", ipv4: ["10.0.0.2"], fullname: "Office Capsule._airport._tcp.local.")
        let den = testDeviceRecord(name: "Den Capsule", ipv4: ["10.0.0.3"], fullname: "Den Capsule._airport._tcp.local.")
        let fixture = try await AppViewFixture(responses: [
            testDiscoverResponse([office, den]),
            testConfigureResponse(host: "root@10.0.0.3")
        ], discoveryWaitsForReadiness: false)
        let flow = fixture.composition.addDeviceStore
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        fixture.appStore.showAddDevice()
        flow.runDiscover()
        try await waitUntilStoreState { flow.state == .discoveryReady }
        flow.select(try XCTUnwrap(flow.devices.last))
        flow.password = "secret"

        flow.runConfigure()

        try await waitUntilStoreState { fixture.registry.profiles.count == 1 && fixture.appStore.route != .addDevice }
        let profile = try XCTUnwrap(fixture.registry.profiles.first)
        XCTAssertEqual(fixture.appStore.route, .device(profile.id))
        XCTAssertEqual(fixture.appStore.justSavedProfileID, profile.id)
        let notice = L10n.format("add_device.saved", profile.title)
        XCTAssertTrue(devicePageTexts(fixture.contentView).contains(notice))
        XCTAssertEqual(flow.state, .discoveryReady)
        XCTAssertNil(flow.savedProfile)
        XCTAssertNil(flow.selectedDeviceID)
        XCTAssertEqual(flow.password, "")

        // The Saved notice is for the moment of saving: leaving the page ends it.
        fixture.appStore.showAddDevice()
        fixture.appStore.select(profile)
        XCTAssertNil(fixture.appStore.justSavedProfileID)
        XCTAssertFalse(devicePageTexts(fixture.contentView).contains(notice))
    }

    func testSavingAManualAddressOpensItAndStartsAddDeviceOverInDiscover() async throws {
        let fixture = try await AppViewFixture(responses: [
            testConfigureResponse(host: "root@10.0.0.9")
        ], discoveryWaitsForReadiness: false)
        let flow = fixture.composition.addDeviceStore
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        fixture.appStore.showAddDevice()
        flow.startManualEntry()
        flow.manualHost = "10.0.0.9"
        flow.password = "secret"

        flow.runConfigure()

        try await waitUntilStoreState { fixture.registry.profiles.count == 1 && fixture.appStore.route != .addDevice }
        let profile = try XCTUnwrap(fixture.registry.profiles.first)
        XCTAssertEqual(profile.host, "root@10.0.0.9")
        XCTAssertEqual(fixture.appStore.route, .device(profile.id))
        XCTAssertTrue(devicePageTexts(fixture.contentView).contains(L10n.format("add_device.saved", profile.title)))
        XCTAssertNil(flow.savedProfile)
        XCTAssertEqual(flow.entryMode, .discover)
        XCTAssertEqual(flow.manualHost, "")
        XCTAssertEqual(flow.password, "")
    }

    func testASaveThatFinishesOnAnotherPageOpensTheDeviceWithTheNotice() async throws {
        let den = testDeviceRecord(name: "Den Capsule", ipv4: ["10.0.0.3"], fullname: "Den Capsule._airport._tcp.local.")
        let runner = PausingStoreTestRunner(responses: [
            testDiscoverResponse([den]),
            testConfigureResponse(host: "root@10.0.0.3", pauseBeforeEvents: true)
        ])
        let fixture = try await AppViewFixture(runner: runner, discoveryWaitsForReadiness: false)
        defer { runner.finishAll() }
        let flow = fixture.composition.addDeviceStore
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        fixture.appStore.showAddDevice()
        flow.runDiscover()
        try await waitUntilStoreState { flow.state == .discoveryReady }
        flow.password = "secret"
        flow.runConfigure()
        try await waitUntilStoreState { flow.isAdding && runner.calls.count == 2 }

        fixture.appStore.navigate(to: .activity)
        runner.finishAll()

        try await waitUntilStoreState { fixture.registry.profiles.count == 1 && fixture.appStore.route != .activity }
        let profile = try XCTUnwrap(fixture.registry.profiles.first)
        XCTAssertEqual(fixture.appStore.route, .device(profile.id))
        // The notice says why the app moved away from the page you were on.
        XCTAssertTrue(devicePageTexts(fixture.contentView).contains(L10n.format("add_device.saved", profile.title)))
    }

    func testManualAddressOfAMovedSavedAirPortOpensThatDevice() async throws {
        let mac = "02:aa:bb:cc:dd:ee"
        let fixture = try await AppViewFixture(responses: [
            testSavedAirportStopResponse(airportMAC: mac),
            testConfigureResponse(host: "root@10.0.0.9", airportMAC: mac)
        ], discoveryWaitsForReadiness: false)
        let saved = try await fixture.registry.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
            discoveredDevice: nil, passwordState: .available, preferredID: "office")
        let flow = fixture.composition.addDeviceStore
        let host = LiveHostedView(fixture.contentView)
        defer { host.close() }
        fixture.appStore.showAddDevice()
        flow.startManualEntry()
        flow.manualHost = "10.0.0.9"
        flow.password = "secret"

        flow.runConfigure()

        try await waitUntilStoreState { fixture.appStore.route == .device(saved.id) && !flow.isRunning }
        XCTAssertEqual(fixture.registry.profiles.map(\.id), [saved.id])
        XCTAssertEqual(fixture.registry.profile(id: saved.id)?.host, "root@10.0.0.9")
        XCTAssertTrue(devicePageTexts(fixture.contentView).contains(L10n.format("add_device.saved", saved.title)))
        XCTAssertNil(flow.error)
        XCTAssertEqual(flow.entryMode, .discover)
        XCTAssertEqual(flow.manualHost, "")
        // Activity and diagnostics keep only the saved device's run: the stopped
        // one was a step of this add, not a failure.
        let stoppedLane = AddDeviceTarget.manual(ManualDeviceTarget(host: "10.0.0.9")).setupLaneKey(existingProfileID: nil)
        XCTAssertTrue(fixture.appStore.operationCoordinator.lane(for: stoppedLane).backend.events.isEmpty)
        try await waitUntilStoreState {
            fixture.appStore.activityStore.laneSnapshots.map(\.laneKey) == [.deviceWorkflow(saved.id, .configure)]
        }
    }

    func testToolbarCancelBeforeAStoppedRunsHelperExitsEndsTheAddFromAnyPage() async throws {
        let mac = "02:aa:bb:cc:dd:ee"
        for page in ["Add Device", "All Devices", "the saved device"] {
            let runner = PausingStoreTestRunner(responses: [
                testSavedAirportStopResponse(airportMAC: mac, pauseAfterEvents: true),
                testConfigureResponse(host: "root@10.0.0.9", airportMAC: mac)
            ])
            let fixture = try await AppViewFixture(runner: runner, discoveryWaitsForReadiness: false)
            defer { runner.finishAll() }
            let saved = try await fixture.registry.storeTestProfile(
                configuredDevice: testConfiguredDevice(host: "10.0.0.2", airportMAC: mac),
                discoveredDevice: nil, passwordState: .available, preferredID: "office")
            try fixture.passwordStore.save("old password", for: saved.keychainAccount)
            let flow = fixture.composition.addDeviceStore
            let stoppedLane = fixture.appStore.operationCoordinator.lane(
                for: AddDeviceTarget.manual(ManualDeviceTarget(host: "10.0.0.9")).setupLaneKey(existingProfileID: nil))
            fixture.appStore.showAddDevice()
            flow.startManualEntry()
            flow.manualHost = "10.0.0.9"
            flow.password = "secret"
            flow.runConfigure()
            // Configure stopped at the saved AirPort; its helper is still sending telemetry.
            try await waitUntilStoreState { stoppedLane.backend.events.contains { $0.code == "device_already_saved" } }
            await runQueuedStoreUpdates()
            XCTAssertTrue(flow.isAdding, page)
            switch page {
            case "All Devices": fixture.appStore.navigate(to: .allDevices)
            case "the saved device": fixture.appStore.select(saved)
            default: break
            }
            XCTAssertTrue(fixture.appStore.canCancelSelectedOperation, page)

            fixture.appStore.cancelSelectedOperation()
            try await waitUntilStoreState { flow.state == .failed && !flow.isRunning }

            // The add ends as a cancelled one does, and never runs as the saved device.
            XCTAssertEqual(runner.calls.count, 1, page)
            XCTAssertEqual(flow.error?.code, "cancelled", page)
            XCTAssertEqual(flow.error?.message, L10n.string("helper.error.cancelled"), page)
            XCTAssertEqual(fixture.registry.profile(id: saved.id)?.host, saved.host, page)
            XCTAssertEqual(try fixture.passwordStore.password(for: saved.keychainAccount), "old password", page)
            XCTAssertEqual(flow.manualHost, "10.0.0.9", page)
            XCTAssertFalse(flow.isAdding, page)
            XCTAssertTrue(stoppedLane.backend.events.isEmpty, page)
            XCTAssertFalse(fixture.appStore.canCancelSelectedOperation, page)
        }
    }

    func testAllDevicesAddButtonsWaitWhileAnAddRuns() async throws {
        let office = testDeviceRecord(name: "Office Capsule", ipv4: ["10.0.0.2"], fullname: "Office Capsule._airport._tcp.local.")
        let den = testDeviceRecord(name: "Den Capsule", ipv4: ["10.0.0.3"], fullname: "Den Capsule._airport._tcp.local.")
        let runner = PausingStoreTestRunner(responses: [
            testDiscoverResponse([office, den]),
            testConfigureResponse(host: "root@10.0.0.3", pauseBeforeEvents: true)
        ])
        let fixture = try await AppViewFixture(runner: runner, discoveryWaitsForReadiness: false)
        defer { runner.finishAll() }
        let flow = fixture.composition.addDeviceStore
        flow.runDiscover()
        try await waitUntilStoreState { flow.state == .discoveryReady }
        fixture.appStore.navigate(to: .allDevices)
        func addButtonsEnabled() -> [Bool] {
            accessibleElements(fixture.contentView)
                .filter { $0.role == .button && $0.text == L10n.string("overview.discovery.add") }
                .map(\.isEnabled)
        }
        XCTAssertEqual(addButtonsEnabled(), [true, true])

        flow.select(try XCTUnwrap(flow.devices.last))
        flow.password = "secret"
        flow.runConfigure()
        try await waitUntilStoreState { flow.isAdding && runner.calls.count == 2 }

        XCTAssertEqual(addButtonsEnabled(), [false, false])

        runner.finishAll()
        try await waitUntilStoreState { !flow.isAdding && fixture.registry.profiles.count == 1 }
        // Den is saved, so only Office still has an Add, and it works again.
        XCTAssertEqual(addButtonsEnabled(), [true])
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
