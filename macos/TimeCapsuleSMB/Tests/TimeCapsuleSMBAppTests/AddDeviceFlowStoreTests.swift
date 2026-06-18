import Combine
import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class AddDeviceFlowStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(AddDeviceFlowState.allCases, [
            .idle,
            .discovering,
            .discoveryEmpty,
            .discoveryReady,
            .manualEntry,
            .passwordEntry,
            .checkingLocalNetwork,
            .configuring,
            .awaitingConfirmation,
            .savingProfile,
            .saved,
            .authFailed,
            .unsupported,
            .failed
        ])
    }

    func testEntryModeInventoryIsExplicit() {
        XCTAssertEqual(AddDeviceEntryMode.allCases, [.discover, .manual])
    }

    func testInvalidDiscoverTimeoutFailsWithoutRunningHelper() async throws {
        let fixture = try await makeStore(responses: [])
        fixture.store.bonjourTimeout = "bad"

        fixture.store.runDiscover()

        XCTAssertEqual(fixture.store.state, .failed)
        XCTAssertEqual(fixture.store.error?.code, "validation_failed")
        XCTAssertEqual(fixture.runner.calls, [])
    }

    func testDiscoverEmptyReadyAndFailureStates() async throws {
        let empty = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
            ])
        ])
        empty.store.runDiscover()
        try await waitUntilStoreState { empty.store.state == .discoveryEmpty }
        XCTAssertEqual(empty.store.devices, [])

        let ready = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [
                    testDeviceRecord(name: "A", hostname: "a.local.", ipv4: ["10.0.0.2"], fullname: "A._airport._tcp.local."),
                    testDeviceRecord(name: "B", hostname: "b.local.", ipv4: ["10.0.0.3"], fullname: "B._airport._tcp.local.")
                ]))
            ])
        ])
        ready.store.runDiscover()
        try await waitUntilStoreState { ready.store.state == .discoveryReady }
        XCTAssertEqual(ready.store.devices.count, 2)
        XCTAssertNil(ready.store.selectedDeviceID)

        let failed = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "discover", code: "bonjour_failed", message: "mDNS failed")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        failed.store.runDiscover()
        try await waitUntilStoreState { failed.store.state == .failed }
        XCTAssertEqual(failed.store.error?.code, "bonjour_failed")
    }

    func testDiscoverUsesBackendDeviceContractInsteadOfRawBonjourRecords() async throws {
        let records = [
            testDeviceRecord(
                name: "Office Capsule",
                hostname: "office.local.",
                ipv4: ["169.254.44.9", "10.0.0.2"],
                fullname: "Office Capsule._airport._tcp.local."
            ),
            testDeviceRecord(
                name: "Office Capsule",
                hostname: "office.local.",
                ipv4: ["10.0.0.2"],
                fullname: "Office Capsule._smb._tcp.local.",
                serviceType: "_smb._tcp.local.",
                services: ["_smb._tcp.local."]
            ),
            testDeviceRecord(
                name: "Office Capsule",
                hostname: "office.local.",
                ipv4: ["10.0.0.2"],
                fullname: "Office Capsule._adisk._tcp.local.",
                serviceType: "_adisk._tcp.local.",
                services: ["_adisk._tcp.local."]
            ),
            testDeviceRecord(
                name: "Lab Capsule",
                hostname: "lab.local.",
                ipv4: ["10.0.0.3"],
                fullname: "Lab Capsule._airport._tcp.local."
            ),
            testDeviceRecord(
                name: "Lab Capsule",
                hostname: "lab.local.",
                ipv4: ["10.0.0.3"],
                fullname: "Lab Capsule._smb._tcp.local.",
                serviceType: "_smb._tcp.local.",
                services: ["_smb._tcp.local."]
            ),
            testDeviceRecord(
                name: "Printer",
                hostname: "printer.local.",
                ipv4: ["10.0.0.20"],
                syap: "",
                model: "",
                fullname: "Printer._ipp._tcp.local.",
                serviceType: "_ipp._tcp.local.",
                services: ["_ipp._tcp.local."]
            )
        ]
        let devices = [
            testDiscoveredDevice(
                id: "bonjour:lab-capsule._airport._tcp.local",
                name: "Lab Capsule",
                host: "10.0.0.3",
                hostname: "lab.local.",
                fullname: "Lab Capsule._airport._tcp.local.",
                selectedRecord: records[3]
            ),
            testDiscoveredDevice(
                id: "bonjour:office-capsule._airport._tcp.local",
                name: "Office Capsule",
                host: "10.0.0.2",
                hostname: "office.local.",
                addresses: ["169.254.44.9", "10.0.0.2"],
                ipv4: ["169.254.44.9", "10.0.0.2"],
                preferredIPv4: "10.0.0.2",
                fullname: "Office Capsule._airport._tcp.local.",
                selectedRecord: records[0]
            )
        ]
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: records, devices: devices))
            ])
        ])

        fixture.store.runDiscover()

        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        XCTAssertEqual(fixture.store.devices.map(\.name), ["Lab Capsule", "Office Capsule"])
        XCTAssertEqual(fixture.store.devices.map(\.host), ["10.0.0.3", "10.0.0.2"])
        XCTAssertEqual(fixture.store.devices[1].addresses, ["169.254.44.9", "10.0.0.2"])
    }

    func testDiscoverPreservesDualStackAddressesAndUsesRegularIPv4SetupTarget() async throws {
        let record = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office.local.",
            ipv4: ["169.254.44.9", "10.0.0.2"],
            ipv6: ["fd00::2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ])
        ])

        fixture.store.runDiscover()

        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        let device = try XCTUnwrap(fixture.store.devices.first)
        XCTAssertEqual(device.addresses, ["169.254.44.9", "10.0.0.2", "fd00::2"])
        XCTAssertEqual(device.connectionTarget, "10.0.0.2")
        XCTAssertEqual(device.addressSummary, "IPv4 10.0.0.2  IPv6 fd00::2")
        XCTAssertEqual(fixture.store.hostFieldText, "10.0.0.2")
    }

    func testIPv6OnlyDiscoveryConfiguresAndSavesNetworkIdentity() async throws {
        let record = testDeviceRecord(
            name: "IPv6 Capsule",
            hostname: "ipv6-capsule.local.",
            ipv4: [],
            ipv6: ["fd00::2"],
            fullname: "IPv6 Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@fd00::2"))
            ])
        ])

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        let device = try XCTUnwrap(fixture.store.devices.first)
        XCTAssertEqual(device.connectionTarget, "fd00::2")
        XCTAssertEqual(fixture.store.hostFieldText, "fd00::2")

        fixture.store.select(device)
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        let profile = try XCTUnwrap(fixture.store.savedProfile)
        XCTAssertEqual(profile.connectionTarget, "fd00::2")
        XCTAssertEqual(profile.addresses, ["fd00::2"])
        XCTAssertEqual(profile.addressSummary, "IPv6 fd00::2")
        XCTAssertNotNil(fixture.runner.calls[1].params["selected_record"])
        XCTAssertNil(fixture.runner.calls[1].params["host"])
    }

    func testMalformedDiscoverPayloadFailsContract() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])

        fixture.store.runDiscover()

        try await waitUntilStoreState { fixture.store.state == .failed }
        XCTAssertEqual(fixture.store.error?.code, "contract_decode_failed")
        XCTAssertEqual(fixture.store.devices, [])
    }

    func testDiscoveredDeviceModelTextUsesFullModelIdentifier() throws {
        let payload = try testDiscoveredDevice(
            syap: "116",
            model: "TimeCapsule6,116"
        ).decode(DiscoveredDevicePayload.self)

        let device = DiscoveredDevice(payload: payload, index: 0)

        XCTAssertEqual(device.model, "TimeCapsule6,116")
        XCTAssertEqual(device.discoveryModelText, "TimeCapsule6,116")
    }

    func testDiscoveredDeviceModelTextCanUseSelectedRecordModel() throws {
        let selectedRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            syap: "116",
            model: "TimeCapsule6,116"
        )
        let payload = try testDiscoveredDevice(
            syap: "116",
            model: nil,
            selectedRecord: selectedRecord
        ).decode(DiscoveredDevicePayload.self)

        let device = DiscoveredDevice(payload: payload, index: 0)

        XCTAssertEqual(device.model, "TimeCapsule6,116")
        XCTAssertEqual(device.discoveryModelText, "TimeCapsule6,116")
    }

    func testDiscoveredDeviceModelTextDoesNotFallbackToSyAP() throws {
        let selectedRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            syap: "116",
            model: ""
        )
        let payload = try testDiscoveredDevice(
            syap: "116",
            model: nil,
            selectedRecord: selectedRecord
        ).decode(DiscoveredDevicePayload.self)

        let device = DiscoveredDevice(payload: payload, index: 0)

        XCTAssertEqual(device.syap, "116")
        XCTAssertNil(device.model)
        XCTAssertEqual(device.discoveryModelText, "")
    }

    func testModeChoiceSeparatesDiscoverAndManualFlows() async throws {
        let record = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office.local.",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ])
        ])

        XCTAssertEqual(fixture.store.entryMode, .discover)
        XCTAssertFalse(fixture.store.isHostFieldEditable)

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        XCTAssertEqual(fixture.store.selectedDevice?.host, "10.0.0.2")
        XCTAssertEqual(fixture.store.hostFieldText, "10.0.0.2")
        XCTAssertFalse(fixture.store.isHostFieldEditable)

        fixture.store.setEntryMode(.manual)

        XCTAssertEqual(fixture.store.entryMode, .manual)
        XCTAssertTrue(fixture.store.isHostFieldEditable)
        XCTAssertEqual(fixture.store.devices, [])
        XCTAssertNil(fixture.store.selectedDeviceID)
    }

    func testResetClearsPasswordAndSetupInputs() async throws {
        let fixture = try await makeStore(responses: [])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.reset()

        XCTAssertEqual(fixture.store.state, .idle)
        XCTAssertEqual(fixture.store.entryMode, .discover)
        XCTAssertEqual(fixture.store.manualHost, "")
        XCTAssertEqual(fixture.store.password, "")
        XCTAssertEqual(fixture.store.devices, [])
        XCTAssertNil(fixture.store.selectedDeviceID)
    }

    func testManualHostConfigureSuccessSavesProfileAndPassword() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
            ])
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        let profile = try XCTUnwrap(fixture.store.savedProfile)
        XCTAssertEqual(fixture.registry.profiles.count, 1)
        XCTAssertEqual(profile.host, "root@10.0.0.2")
        XCTAssertEqual(profile.passwordState, .available)
        XCTAssertEqual(try fixture.passwordStore.password(for: profile.keychainAccount), "secret")
        XCTAssertEqual(fixture.runner.calls.count, 1)
        XCTAssertEqual(fixture.runner.calls[0].operation, "configure")
        XCTAssertEqual(fixture.runner.calls[0].context?.profileID, profile.id)
        guard case .string(let stagedConfigPath)? = fixture.runner.calls[0].params["config"] else {
            return XCTFail("Expected staged config path.")
        }
        XCTAssertNotEqual(stagedConfigPath, profile.configPath)
        XCTAssertTrue(stagedConfigPath.contains("/.Staging/"))
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configPath))
        XCTAssertEqual(fixture.runner.calls[0].params["host"], .string("root@10.0.0.2"))
        XCTAssertEqual(fixture.runner.calls[0].params["persist_password"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["password"], .string("secret"))
        XCTAssertEqual(fixture.runner.calls[0].params["debug_logging"], .bool(false))
    }

    func testConfigureRunsAfterAllowedLocalNetworkPreflightAndForwardsTelemetry() async throws {
        let checker = FixedLocalNetworkPreflightChecker(status: .allowed)
        let fixture = try await makeStore(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
                ])
            ],
            localNetworkPreflightChecker: checker
        )

        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.runner.calls.count == 1 }
        XCTAssertEqual(checker.checkCount, 1)
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_result"], .string("allowed"))
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_duration_ms"], .number(7))
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_service"], .string("_airport._tcp"))
    }

    func testConfigureContinuesAfterUnknownLocalNetworkPreflight() async throws {
        let checker = FixedLocalNetworkPreflightChecker(status: .unknown, detail: "timeout")
        let fixture = try await makeStore(
            responses: [
                .init(events: [
                    BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
                ])
            ],
            localNetworkPreflightChecker: checker
        )

        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.runner.calls.count == 1 }
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_result"], .string("unknown"))
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_error"], .string("timeout"))
    }

    func testConfigureDeniedLocalNetworkPreflightUsesBackendTelemetryFailure() async throws {
        let checker = FixedLocalNetworkPreflightChecker(status: .denied, detail: "policy denied")
        let opener = TestRecordingURLOpener()
        let fixture = try await makeStore(
            responses: [
                .init(events: [
                    BackendEvent(
                        type: "error",
                        operation: "configure",
                        code: "local_network_permission_denied",
                        message: "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
                        recovery: .object([
                            "title": .string("Local Network access blocked"),
                            "message": .string("macOS is blocking TimeCapsuleSMB from accessing devices on your local network."),
                            "actions": .array([]),
                            "action_ids": .array([.string("open_system_settings"), .string("retry")]),
                            "retryable": .bool(true),
                            "suggested_operation": .string("configure")
                        ])
                    )
                ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
            ],
            localNetworkPreflightChecker: checker,
            urlOpener: opener
        )

        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .failed }
        XCTAssertEqual(fixture.runner.calls.count, 1)
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_result"], .string("denied"))
        XCTAssertEqual(fixture.runner.calls[0].params["macos_local_network_preflight_error"], .string("policy denied"))
        XCTAssertEqual(fixture.store.error?.code, "local_network_permission_denied")

        fixture.store.handleRecoveryAction(RecoveryAction(title: "Open System Settings", kind: .openSystemSettings))

        XCTAssertEqual(opener.openedURLs, LocalNetworkRecovery.settingsURL.map { [$0] } ?? [])
    }

    func testPublishesWhenSetupBackendFinishesAfterConfigureResult() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
            ])
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"
        let finishPublished = expectation(description: "AddDeviceFlowStore publishes after setup backend running state clears")
        var didFulfill = false
        var cancellables: Set<AnyCancellable> = []
        fixture.store.objectWillChange
            .sink { [weak store = fixture.store] _ in
                Task { @MainActor in
                    guard !didFulfill,
                          store?.state == .saved,
                          store?.isRunning == false else {
                        return
                    }
                    didFulfill = true
                    finishPublished.fulfill()
                }
            }
            .store(in: &cancellables)

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        await fulfillment(of: [finishPublished], timeout: 2)
        XCTAssertFalse(fixture.store.isRunning)
        _ = cancellables
    }

    func testConfigureSSHEnableConfirmationCanBeConfirmedAndSavesProfile() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "configure",
                    code: "confirmation_required",
                    message: "SSH is closed.",
                    details: .object([
                        "confirmation_id": .string("confirm-ssh"),
                        "presentation_id": .string("configure.enable_ssh_reboot"),
                        "presentation_values": .object(["device_name": .string("Office Capsule")])
                    ])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
            ])
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState {
            fixture.store.state == .awaitingConfirmation &&
            fixture.store.coordinator.pendingConfirmation != nil &&
            !fixture.store.coordinator.lane(for: .candidateHost("10.0.0.2")).backend.isRunning
        }
        XCTAssertFalse(fixture.store.canConfigure)
        XCTAssertEqual(fixture.registry.profiles, [])

        fixture.store.coordinator.confirmPending()

        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.runner.calls.count, 2)
        XCTAssertEqual(fixture.runner.calls[1].params["confirmation_id"], .string("confirm-ssh"))
        XCTAssertEqual(fixture.store.savedProfile?.host, "root@10.0.0.2")
        XCTAssertEqual(fixture.registry.profiles.count, 1)
    }

    func testConfigureSSHEnableConfirmationCancellationReturnsToPasswordEntry() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "configure",
                    code: "confirmation_required",
                    message: "SSH is closed.",
                    details: .object([
                        "confirmation_id": .string("confirm-ssh"),
                        "presentation_id": .string("configure.enable_ssh_reboot"),
                        "presentation_values": .object(["device_name": .string("Office Capsule")])
                    ])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()
        try await waitUntilStoreState {
            fixture.store.state == .awaitingConfirmation &&
            fixture.store.coordinator.pendingConfirmation != nil &&
            !fixture.store.coordinator.lane(for: .candidateHost("10.0.0.2")).backend.isRunning
        }

        fixture.store.coordinator.cancelPendingConfirmation()

        try await waitUntilStoreState { fixture.store.state == .passwordEntry }
        XCTAssertNil(fixture.store.error)
        XCTAssertNil(fixture.store.savedProfile)
        XCTAssertEqual(fixture.store.manualHost, "10.0.0.2")
        XCTAssertEqual(fixture.store.password, "secret")
        XCTAssertTrue(fixture.store.canConfigure)
        XCTAssertEqual(fixture.registry.profiles, [])
    }

    func testNewManualProfileUsesAppDefaultDeviceSettings() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
            ])
        ])
        let defaultSettings = DeviceProfileSettings(
            nbnsEnabled: false,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            anyProtocol: true,
            fruitMetadataNetatalk: true,
            debugLogging: true,
            mountWaitSeconds: 45,
            ataIdleSeconds: 600,
            ataStandby: 900
        )
        var appSettings = AppSettings.default
        appSettings.defaultDeviceSettings = defaultSettings
        fixture.store.applyAppSettings(appSettings)
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.store.savedProfile?.settings, defaultSettings)
        XCTAssertEqual(fixture.runner.calls[0].params["debug_logging"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[0].params["internal_share_use_disk_root"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[0].params["smb_bind_lan_only"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["smb_browse_compatibility"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[0].params["any_protocol"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[0].params["fruit_metadata_netatalk"], .bool(true))
        XCTAssertEqual(fixture.runner.calls[0].params["ata_idle_seconds"], .number(600))
        XCTAssertEqual(fixture.runner.calls[0].params["ata_standby"], .number(900))
    }

    func testExistingProfileSettingsAreNotClobberedByAppDefaults() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "10.0.0.2"))
            ])
        ])
        let existing = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        var editedExisting = existing
        editedExisting.settings = DeviceProfileSettings(
            nbnsEnabled: false,
            internalShareUseDiskRoot: false,
            smbBrowseCompatibility: false,
            anyProtocol: false,
            fruitMetadataNetatalk: false,
            debugLogging: false,
            mountWaitSeconds: 99,
            ataIdleSeconds: 111,
            ataStandby: nil
        )
        _ = try await fixture.registry.updateProfile(editedExisting)
        var appSettings = AppSettings.default
        appSettings.defaultDeviceSettings = DeviceProfileSettings(
            nbnsEnabled: true,
            internalShareUseDiskRoot: true,
            smbBrowseCompatibility: true,
            anyProtocol: true,
            fruitMetadataNetatalk: true,
            debugLogging: true,
            mountWaitSeconds: 1,
            ataIdleSeconds: 2,
            ataStandby: 3
        )
        fixture.store.applyAppSettings(appSettings)
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.store.savedProfile?.settings, editedExisting.settings)
        XCTAssertEqual(fixture.runner.calls[0].params["debug_logging"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["internal_share_use_disk_root"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["smb_bind_lan_only"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["smb_browse_compatibility"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["any_protocol"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["fruit_metadata_netatalk"], .bool(false))
        XCTAssertEqual(fixture.runner.calls[0].params["ata_idle_seconds"], .number(111))
        XCTAssertEqual(fixture.runner.calls[0].params["ata_standby"], .string(""))
    }

    func testConfigureRejectedWhileAnotherOperationRunsSavesNothing() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "doctor", ok: true, payload: .object(["ok": .bool(true)]))
            ], pauseAfterEvents: true)
        ])
        let existing = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        _ = fixture.store.coordinator.run(
            operation: "doctor",
            context: existing.runtimeContext,
            activeDeviceID: existing.id,
            laneKey: .device(existing.id)
        )
        try await waitUntilStoreState { fixture.runner.calls.count == 1 }
        XCTAssertTrue(fixture.store.coordinator.lane(for: existing).backend.isRunning)
        fixture.store.runConfigure()

        XCTAssertEqual(fixture.store.state, .failed)
        XCTAssertEqual(fixture.store.error?.code, "operation_rejected")
        XCTAssertEqual(fixture.registry.profiles, [existing])
        XCTAssertEqual(fixture.runner.calls.count, 1)
        fixture.runner.finishAll()
        try await waitUntilStoreState { !fixture.store.coordinator.lane(for: existing).backend.isRunning }
    }

    func testManualConfigureCanRunWhileAppDiscoveryLaneIsBusy() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: []))
            ], pauseAfterEvents: true),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.2"))
            ])
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.coordinator.run(operation: "discover", laneKey: .app)
        try await waitUntilStoreState { fixture.store.coordinator.appLane.backend.isRunning }
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.runner.calls.count == 2 }
        XCTAssertEqual(fixture.runner.calls.map(\.operation), ["discover", "configure"])
        XCTAssertTrue(fixture.store.coordinator.appLane.backend.isRunning)
        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.registry.profiles.count, 1)
        fixture.runner.finishAll()
    }

    func testSelectedBonjourConfigureSuccessSavesProfileMetadata() async throws {
        let record = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office.local.",
            ipv4: ["10.0.0.5"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "10.0.0.5"))
            ])
        ])

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        let device = try XCTUnwrap(fixture.store.devices.first)
        fixture.store.select(device)
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        let profile = try XCTUnwrap(fixture.store.savedProfile)
        XCTAssertEqual(profile.bonjourFullname, "Office Capsule._airport._tcp.local.")
        XCTAssertEqual(profile.hostname, "office.local.")
        XCTAssertEqual(profile.addresses, ["10.0.0.5"])
        XCTAssertNotNil(fixture.runner.calls[1].params["selected_record"])
        XCTAssertNil(fixture.runner.calls[1].params["host"])
    }

    func testConfigureAuthFailurePreservesDiscoverySelection() async throws {
        let record = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office.local.",
            ipv4: ["10.0.0.5"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ]),
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "auth_failed", message: "bad password")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        let selectedID = fixture.store.selectedDeviceID
        fixture.store.password = "bad"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .authFailed }
        XCTAssertEqual(fixture.store.selectedDeviceID, selectedID)
        XCTAssertEqual(fixture.store.devices.count, 1)
        XCTAssertEqual(fixture.registry.profiles, [])
    }

    func testMalformedConfigurePayloadFailsContractAndSavesNothing() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: .object(["schema_version": .string("wrong")]))
            ])
        ])
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .failed }
        XCTAssertEqual(fixture.store.error?.code, "contract_decode_failed")
        XCTAssertEqual(fixture.registry.profiles, [])
        XCTAssertNil(fixture.store.savedProfile)
    }

    func testAuthFailureAndUnsupportedDeviceSaveNothing() async throws {
        let auth = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "auth_failed", message: "bad password")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        auth.store.startManualEntry()
        auth.store.manualHost = "10.0.0.2"
        auth.store.password = "bad"
        auth.store.runConfigure()
        try await waitUntilStoreState { auth.store.state == .authFailed }
        XCTAssertEqual(auth.registry.profiles, [])
        XCTAssertNil(auth.store.savedProfile)

        let unsupported = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "error", operation: "configure", code: "unsupported_device", message: "unsupported")
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: ""))
        ])
        unsupported.store.startManualEntry()
        unsupported.store.manualHost = "10.0.0.3"
        unsupported.store.password = "pw"
        unsupported.store.runConfigure()
        try await waitUntilStoreState { unsupported.store.state == .unsupported }
        XCTAssertEqual(unsupported.registry.profiles, [])
        XCTAssertNil(unsupported.store.savedProfile)
    }

    func testDuplicateHostUpdatesExistingProfileAfterConfigureSucceeds() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(
                    host: "10.0.0.2",
                    model: "Updated Capsule"
                ))
            ])
        ])
        let existing = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "Original Capsule"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "existing-device"
        )
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "new-secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.registry.profiles.count, 1)
        XCTAssertEqual(fixture.store.savedProfile?.id, existing.id)
        XCTAssertEqual(fixture.store.savedProfile?.model, "Updated Capsule")
        XCTAssertEqual(fixture.runner.calls[0].context?.profileID, existing.id)
    }

    func testKeychainSaveFailureDoesNotSaveProfile() async throws {
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "10.0.0.2"))
            ])
        ])
        fixture.passwordStore.saveFailure = .save
        fixture.store.startManualEntry()
        fixture.store.manualHost = "10.0.0.2"
        fixture.store.password = "secret"

        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .failed }
        XCTAssertEqual(fixture.store.error?.code, "profile_save_failed")
        XCTAssertNil(fixture.store.savedProfile)
        XCTAssertEqual(fixture.registry.profiles, [])
    }

    func testSelectingAlreadySavedDiscoveryRoutesToExistingProfile() async throws {
        let record = testDeviceRecord(
            name: "Office Capsule",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [record]))
            ])
        ])
        let existing = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try DiscoveredDevice(record: record.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "existing-device"
        )

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        fixture.store.select(try XCTUnwrap(fixture.store.devices.first))

        XCTAssertEqual(fixture.store.state, .saved)
        XCTAssertEqual(fixture.store.savedProfile?.id, existing.id)
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    func testSavedDiscoveryConfigureUsesCurrentRecordAfterIpChanges() async throws {
        let oldRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.2"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let currentRecord = testDeviceRecord(
            name: "Office Capsule",
            hostname: "office-capsule.local.",
            ipv4: ["10.0.0.80"],
            fullname: "Office Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [currentRecord]))
            ]),
            .init(events: [
                BackendEvent(type: "result", operation: "configure", ok: true, payload: testConfigurePayload(host: "root@10.0.0.80"))
            ])
        ])
        let existing = try await fixture.registry.saveConfiguredDevice(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try DiscoveredDevice(record: oldRecord.decode(BonjourResolvedServicePayload.self), index: 0),
            passwordState: .available,
            preferredID: "existing-device"
        )

        fixture.store.runDiscover()
        try await waitUntilStoreState { fixture.store.state == .discoveryReady }
        fixture.store.select(try XCTUnwrap(fixture.store.devices.first))
        fixture.store.password = "secret"
        fixture.store.runConfigure()

        try await waitUntilStoreState { fixture.store.state == .saved }
        XCTAssertEqual(fixture.store.savedProfile?.id, existing.id)
        XCTAssertEqual(fixture.runner.calls[1].params["selected_record"], currentRecord)
        XCTAssertNil(fixture.runner.calls[1].params["host"])
        XCTAssertEqual(fixture.runner.calls[1].context?.profileID, existing.id)
    }

    func testSelectingSharedDiscoveryDeviceFromOverviewPromptsForPasswordWithoutRediscovering() async throws {
        let discovered = testDiscoveredDevice(
            name: "Office Capsule",
            host: "10.0.0.2",
            model: "TimeCapsule6,116"
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(type: "result", operation: "discover", ok: true, payload: testDiscoverPayload(records: [], devices: [discovered]))
            ])
        ])
        fixture.store.discovery.refresh(timeout: 0.1)
        try await waitUntilStoreState { fixture.store.discovery.state == .ready }
        let device = try XCTUnwrap(fixture.store.discovery.devices.first)

        fixture.store.select(device)

        XCTAssertEqual(fixture.store.state, .passwordEntry)
        XCTAssertEqual(fixture.store.devices, [device])
        XCTAssertEqual(fixture.store.selectedDeviceID, device.id)
        XCTAssertEqual(fixture.store.hostFieldText, "10.0.0.2")
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    func testSharedDiscoverySelectionKeepsListOrderAndPreselectsClickedDevice() async throws {
        let firstPayload = testDiscoveredDevice(
            id: "bonjour:first",
            name: "First Capsule",
            host: "10.0.0.2",
            hostname: "first.local.",
            fullname: "First Capsule._airport._tcp.local."
        )
        let secondPayload = testDiscoveredDevice(
            id: "bonjour:second",
            name: "Second Capsule",
            host: "10.0.0.3",
            hostname: "second.local.",
            fullname: "Second Capsule._airport._tcp.local."
        )
        let fixture = try await makeStore(responses: [
            .init(events: [
                BackendEvent(
                    type: "result",
                    operation: "discover",
                    ok: true,
                    payload: testDiscoverPayload(records: [], devices: [firstPayload, secondPayload])
                )
            ])
        ])
        fixture.store.discovery.refresh(timeout: 0.1)
        try await waitUntilStoreState { fixture.store.discovery.state == .ready }
        let first = fixture.store.discovery.devices[0]
        let second = fixture.store.discovery.devices[1]

        fixture.store.select(second)

        XCTAssertEqual(fixture.store.state, .passwordEntry)
        XCTAssertEqual(fixture.store.devices, [first, second])
        XCTAssertEqual(fixture.store.selectedDeviceID, second.id)
        XCTAssertEqual(fixture.store.hostFieldText, "10.0.0.3")
        XCTAssertEqual(fixture.runner.calls.count, 1)
    }

    private func makeStore(
        responses: [StoreTestRunner.Response],
        localNetworkPreflightChecker: LocalNetworkPreflightChecking? = nil,
        urlOpener: URLOpening = WorkspaceURLOpener()
    ) async throws -> (
        store: AddDeviceFlowStore,
        runner: PausingStoreTestRunner,
        registry: DeviceRegistryStore,
        passwordStore: InMemoryPasswordStore
    ) {
        let temp = try TemporaryDirectory()
        let registry = DeviceRegistryStore(applicationSupportURL: temp.url)
        await registry.load()
        let runner = PausingStoreTestRunner(responses: responses)
        let coordinator = OperationCoordinator(backend: BackendClient(runner: runner))
        let passwordStore = InMemoryPasswordStore()
        let store = AddDeviceFlowStore(
            coordinator: coordinator,
            registry: registry,
            passwordStore: passwordStore,
            localNetworkPreflightChecker: localNetworkPreflightChecker,
            urlOpener: urlOpener
        )
        return (store, runner, registry, passwordStore)
    }
}
