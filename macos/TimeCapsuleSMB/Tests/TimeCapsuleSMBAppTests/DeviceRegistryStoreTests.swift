import XCTest
@testable import TimeCapsuleSMBApp

@MainActor
final class DeviceRegistryStoreTests: XCTestCase {
    func testStateInventoryIsExplicit() {
        XCTAssertEqual(DeviceRegistryState.allCases, [.idle, .loading, .empty, .loaded, .saving, .failed])
    }

    func testMissingRegistryStartsEmpty() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.devicesDirectoryURL.path))
    }

    func testFailedDeployDiagnosticTextSurvivesRegistryReload() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await store.updateDeployState(testDeployState(
            status: .failed,
            errorCode: "remote_error",
            errorMessage: "Deployment failed.",
            diagnosticText: "remote_manager_log_tail: service failed"
        ), for: profile.id)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url)
        await reloaded.load()

        XCTAssertEqual(
            reloaded.profile(id: profile.id)?.lastDeployState?.diagnosticText,
            "remote_manager_log_tail: service failed"
        )
    }

    func testLateProgressCannotOverwriteCompletedDeployThroughEitherWritePath() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        var completed = testDeployState(status: .succeeded)
        completed.operationID = "completed-operation"
        await store.updateInstallOperationState(
            deployState: completed,
            runtimeState: testRuntimeState(state: .installedVerified),
            for: profile.id
        )

        var lateProgress = testDeployState(status: .deploying, finishedAt: nil, verified: nil)
        lateProgress.operationID = completed.operationID
        let installing = testRuntimeState(state: .installing, verified: nil)
        await store.updateInstallOperationState(
            deployState: lateProgress,
            runtimeState: installing,
            for: profile.id
        )
        await store.updateDeployState(lateProgress, for: profile.id)
        XCTAssertEqual(store.profile(id: profile.id)?.lastDeployState?.status, .succeeded)
        XCTAssertEqual(store.profile(id: profile.id)?.runtimeState?.state, .installedVerified)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url)
        await reloaded.load()
        XCTAssertEqual(reloaded.profile(id: profile.id)?.lastDeployState?.status, .succeeded)

        lateProgress.operationID = "next-operation"
        await store.updateDeployState(lateProgress, for: profile.id)
        XCTAssertEqual(store.profile(id: profile.id)?.lastDeployState?.status, .deploying)
        await store.updateInstallOperationState(
            deployState: lateProgress,
            runtimeState: installing,
            for: profile.id
        )
        XCTAssertEqual(store.profile(id: profile.id)?.lastDeployState?.status, .deploying)
        XCTAssertEqual(store.profile(id: profile.id)?.runtimeState?.state, .installing)
    }

    func testCorruptRegistryEntersFailedStateWithoutDeletingFile() async throws {
        let temp = try TemporaryDirectory()
        let registryURL = temp.url.appendingPathComponent("devices.json")
        try "{ not json".write(to: registryURL, atomically: true, encoding: .utf8)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        XCTAssertEqual(store.state, .failed)
        XCTAssertNotNil(store.error)
        XCTAssertTrue(FileManager.default.fileExists(atPath: registryURL.path))
        XCTAssertEqual(try String(contentsOf: registryURL), "{ not json")
    }

    func testLegacyStoredPathAndKeychainAccountAreDerivedAfterLoad() async throws {
        let temp = try TemporaryDirectory()
        let registryURL = temp.url.appendingPathComponent("devices.json")
        try """
        [
          {
            "id": "device-one",
            "displayName": "Office",
            "network": {
              "configuredSSHTarget": "10.0.0.2",
              "addresses": []
            },
            "configPath": "/legacy/path/.env",
            "keychainAccount": "legacy-account",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T00:00:00Z",
            "settings": {},
            "passwordState": "available"
          }
        ]
        """.write(to: registryURL, atomically: true, encoding: .utf8)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)

        await store.load()

        let profile = try XCTUnwrap(store.profiles.first)
        XCTAssertEqual(profile.configPath, temp.url.appendingPathComponent("Devices/device-one/.env").path)
        XCTAssertEqual(profile.keychainAccount, "device-one")
        _ = try await store.updateProfile(profile)
        let persistedJSON = try String(contentsOf: registryURL)
        XCTAssertFalse(persistedJSON.contains("\"configPath\""))
        XCTAssertFalse(persistedJSON.contains("\"keychainAccount\""))
    }

    func testCreateUpdateAndDeleteProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()

        var profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        XCTAssertEqual(store.state, .loaded)
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(profile.configPath, temp.url.appendingPathComponent("Devices/device-one/.env").path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: URL(fileURLWithPath: profile.configPath).deletingLastPathComponent().path))
        let persistedJSON = try String(contentsOf: store.registryURL)
        XCTAssertFalse(persistedJSON.contains("\"configPath\""))
        XCTAssertFalse(persistedJSON.contains("\"keychainAccount\""))

        profile.displayName = "Renamed Capsule"
        profile.settings.debugLogging = true
        let updated = try await store.updateProfile(profile)
        XCTAssertEqual(updated.displayName, "Renamed Capsule")
        XCTAssertEqual(store.profiles.first?.settings.debugLogging, true)

        try await store.delete(updated)
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
        XCTAssertFalse(FileManager.default.fileExists(atPath: URL(fileURLWithPath: updated.configPath).deletingLastPathComponent().path))
        let stagingURL = temp.url.appendingPathComponent("Devices/.Staging", isDirectory: true)
        let stagedArtifacts = (try? FileManager.default.contentsOfDirectory(atPath: stagingURL.path)) ?? []
        XCTAssertEqual(stagedArtifacts, [])
    }

    func testExplicitReconnectUpdatesConfiguredEndpointAndKeepsNamesAsSuggestions() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "tcapsule.local."),
            discoveredDevice: try discovered(record: testDeviceRecord(hostname: "tcapsule.local.", fullname: "Office._airport._tcp.local.")),
            passwordState: .available, preferredID: "first")
        let duplicate = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: " TCAPSULE.LOCAL. ", model: "Updated Model"),
            discoveredDevice: nil, passwordState: .missing, preferredID: "duplicate", existingProfileID: first.id)
        XCTAssertEqual(duplicate.id, first.id)
        XCTAssertEqual(duplicate.model, "Updated Model")
        let moved = try discovered(record: testDeviceRecord(hostname: "tcapsule.local.", ipv4: ["10.0.0.9"], fullname: "Office._airport._tcp.local."))
        XCTAssertNil(store.matchingProfile(for: moved))
        XCTAssertEqual(store.suggestedProfile(for: moved)?.id, first.id)
        let other = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: moved.host),
            discoveredDevice: moved, passwordState: .available, preferredID: "other")
        XCTAssertNotEqual(other.id, first.id)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testEqualFullnamesWithConflictingHostsKeepSeparateProfiles() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(hostname: "first.local.", ipv4: ["10.0.0.2"], fullname: "Shared._airport._tcp.local.")),
            passwordState: .available, preferredID: "first")
        let second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try discovered(record: testDeviceRecord(hostname: "second.local.", ipv4: ["10.0.0.3"], fullname: "Shared._airport._tcp.local.")),
            passwordState: .available, preferredID: "second")
        XCTAssertNotEqual(first.id, second.id)
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: first.id)?.host, "10.0.0.2")
        XCTAssertEqual(store.profile(id: second.id)?.host, "10.0.0.3")
        await store.load()
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testAmbiguousDiscoveryDoesNotMergeOrDeleteExistingProfiles() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(hostname: "first.local.", ipv4: ["10.0.0.2"], fullname: "First._airport._tcp.local.")),
            passwordState: .available, preferredID: "first")
        let second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try discovered(record: testDeviceRecord(hostname: "second.local.", ipv4: ["10.0.0.3"], fullname: "Second._airport._tcp.local.")),
            passwordState: .available, preferredID: "second")
        let ambiguous = try discovered(record: testDeviceRecord(hostname: "third.local.", ipv4: ["10.0.0.2", "10.0.0.3"], fullname: "Third._airport._tcp.local."))
        XCTAssertNil(store.matchingProfile(for: ambiguous))
        XCTAssertEqual(store.profileMatch(for: ambiguous), .conflict([first.id, second.id]))
        // The app's selection rejects ambiguity before constructing a save.
        // Final persistence must still protect the chosen configured endpoint.
        do {
            _ = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: ambiguous.connectionTarget),
                discoveredDevice: ambiguous, passwordState: .available, preferredID: "third")
            XCTFail("Ambiguous addresses must require choosing a profile explicitly")
        } catch {
            XCTAssertTrue(error is DeviceRegistryError)
        }
        XCTAssertEqual(Set(store.profiles.map(\.id)), [first.id, second.id])
        await store.load()
        XCTAssertEqual(Set(store.profiles.map(\.id)), [first.id, second.id])
    }

    func testHostnameOnlyPeersKeepSeparateProfiles() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let firstDevice = try hostnamePeer(host: "10.0.0.2", fullname: "First._airport._tcp.local.")
        let secondDevice = try hostnamePeer(host: "10.0.0.3", fullname: "Second._airport._tcp.local.")
        let first = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: firstDevice.host),
            discoveredDevice: firstDevice, passwordState: .available, preferredID: "first")
        do {
            let second = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: secondDevice.host),
                discoveredDevice: secondDevice, passwordState: .available, preferredID: "second")
            XCTAssertNotEqual(first.id, second.id)
            await store.load()
            XCTAssertEqual(Set(store.profiles.map(\.id)), [first.id, second.id])
            XCTAssertEqual(store.matchingProfile(for: firstDevice)?.id, first.id)
            XCTAssertEqual(store.matchingProfile(for: secondDevice)?.id, second.id)
        } catch {
            XCTFail("Distinct endpoints should permit similarly named peers: \(error)")
        }
    }

    func testAmbiguousConcurrentPeerSavesKeepBothProfiles() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try hostnamePeer(host: "10.0.0.2", fullname: "Shared._airport._tcp.local.")
        let second = try hostnamePeer(host: "10.0.0.3", fullname: "Shared._airport._tcp.local.")
        async let one = store.storeTestProfile(configuredDevice: testConfiguredDevice(host: first.host),
            discoveredDevice: first, passwordState: .available, preferredID: "first")
        async let two = store.storeTestProfile(configuredDevice: testConfiguredDevice(host: second.host),
            discoveredDevice: second, passwordState: .available, preferredID: "second")
        let saved = try await [one, two]
        XCTAssertEqual(Set(saved.map(\.id)).count, 2)
        await store.load()
        XCTAssertEqual(Set(store.profiles.map(\.id)), Set(saved.map(\.id)))
    }

    func testSameNamesStaySuggestionsWithoutHardwareEvidence() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let originalDevice = try hostnamePeer(host: "10.0.0.2", fullname: "Shared._airport._tcp.local.")
        let original = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: originalDevice.host),
            discoveredDevice: originalDevice, passwordState: .available, preferredID: "original")
        let unrelated = try hostnamePeer(host: "10.0.0.9", fullname: "Shared._airport._tcp.local.")
        XCTAssertNil(store.matchingProfile(for: unrelated))
        XCTAssertEqual(store.suggestedProfile(for: unrelated)?.id, original.id)
        let other = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: unrelated.host),
            discoveredDevice: unrelated, passwordState: .available, preferredID: "other")
        XCTAssertNotEqual(other.id, original.id)
        await store.load()
        XCTAssertEqual(Set(store.profiles.map(\.id)), [original.id, other.id])
        XCTAssertNil(store.suggestedProfile(for: try hostnamePeer(host: "10.0.0.10", fullname: "Shared._airport._tcp.local.")))
    }

    func testLegacyCollisionFlagIsIgnoredAndIdentityDoesNotAccumulateAddresses() throws {
        let data = Data(#"{"configuredSSHTarget":"10.0.0.2","addresses":[],"namesAreAmbiguous":true}"#.utf8)
        let previous = try JSONDecoder().decode(DeviceNetworkIdentity.self, from: data)
        XCTAssertNil(previous.airportMAC)
        let fresh = try hostnamePeer(host: "10.0.0.3", fullname: "Shared._airport._tcp.local.")
        let updated = DeviceNetworkIdentity.make(configuredSSHTarget: "10.0.0.3", discoveredDevice: fresh,
            existing: previous, airportMAC: "02-AA-BB-CC-DD-EE")
        XCTAssertEqual(updated.airportMAC, "02:aa:bb:cc:dd:ee")
        XCTAssertEqual(updated.addressValues, ["10.0.0.3"])
        XCTAssertFalse(updated.matches(previous))
    }

    func testConfiguredEndpointConflictDoesNotDeleteEitherProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil, passwordState: .available, preferredID: "first")
        let second = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil, passwordState: .available, preferredID: "second")
        var update = second
        update.host = first.host
        do {
            _ = try await store.updateProfile(update)
            XCTFail("Expected an endpoint conflict")
        } catch {
            guard case .duplicateProfile = error as? DeviceRegistryError else { return XCTFail("Unexpected error: \(error)") }
        }
        XCTAssertEqual(store.profile(id: first.id), first)
        XCTAssertEqual(store.profile(id: second.id), second)
        // Observed addresses reserve no endpoint and may be reused by DHCP.
        update = second
        update.network.setAddressValues(update.network.addressValues + first.network.addressValues)
        _ = try await store.updateProfile(update)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testConcurrentDuplicateSavesRejectConflictingProfileWithoutMerging() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: { Date(timeIntervalSince1970: 1000) })
        await store.load()

        let first = try await store.makeConfiguredDeviceProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2", model: "Original Capsule"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await store.makeConfiguredDeviceProfile(
            configuredDevice: testConfiguredDevice(host: " 10.0.0.2 ", model: "Updated Capsule"),
            discoveredDevice: nil,
            passwordState: .missing,
            preferredID: "device-two"
        )

        func save(_ profile: DeviceProfile) async -> Result<DeviceProfile, Error> {
            do { return .success(try await store.saveProfile(profile)) }
            catch { return .failure(error) }
        }
        async let one = save(first)
        async let two = save(second)
        let outcomes = await [one, two]
        let saved = outcomes.compactMap { try? $0.get() }
        XCTAssertEqual(saved.count, 1)
        let winner = try XCTUnwrap(saved.first)
        for outcome in outcomes {
            if case .failure(let error) = outcome {
                guard case .duplicateProfile(let field, _, let id) = error as? DeviceRegistryError else {
                    return XCTFail("Unexpected save error: \(error)")
                }
                XCTAssertEqual(field, "host")
                XCTAssertEqual(id, winner.id)
            }
        }
        XCTAssertEqual(store.profiles.count, 1)
        XCTAssertEqual(store.profiles, [winner])

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let persisted = try decoder.decode([DeviceProfile].self, from: Data(contentsOf: store.registryURL))
        XCTAssertEqual(persisted.count, 1)
        XCTAssertEqual(persisted.first?.id, winner.id)
        XCTAssertEqual(persisted.first?.network, winner.network)
        XCTAssertEqual(persisted.first?.model, winner.model)
        XCTAssertEqual(persisted.first?.passwordState, winner.passwordState)
        await store.load()
        XCTAssertEqual(store.profiles, [winner])
    }

    func testUpdateProfileDoesNotMergeDuplicateHostIntoAnotherProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        var conflictingUpdate = second
        conflictingUpdate.host = " root@10.0.0.2. "

        do {
            _ = try await store.updateProfile(conflictingUpdate)
            XCTFail("Expected duplicate host update to fail.")
        } catch {
            XCTAssertEqual(
                error as? DeviceRegistryError,
                .duplicateProfile(field: "host", value: "10.0.0.2", conflictingProfileID: first.id)
            )
        }
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: first.id)?.host, "10.0.0.2")
        XCTAssertEqual(store.profile(id: second.id)?.host, "10.0.0.3")
    }

    func testUpdateProfileAllowsSharedBonjourNameWithDistinctEndpoints() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try hostnamePeer(host: "10.0.0.2", fullname: "Shared._airport._tcp.local."), passwordState: .available, preferredID: "first")
        var second = try await store.storeTestProfile(configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try hostnamePeer(host: "10.0.0.3", fullname: "Other._airport._tcp.local."), passwordState: .available, preferredID: "second")
        second.network.bonjourFullname = first.network.bonjourFullname
        _ = try await store.updateProfile(second)
        XCTAssertEqual(store.profiles.count, 2)
        XCTAssertEqual(store.profile(id: second.id)?.network.bonjourFullname, "Shared._airport._tcp.local.")
        XCTAssertEqual(store.profile(id: first.id), first)
    }

    func testUpdateProfileIgnoresLinkLocalAddressConflicts() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        _ = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "office.local.",
                ipv4: ["10.0.0.2", "169.254.44.9"],
                fullname: "Office._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: try discovered(record: testDeviceRecord(
                hostname: "den.local.",
                ipv4: ["10.0.0.3"],
                fullname: "Den._airport._tcp.local."
            )),
            passwordState: .available,
            preferredID: "device-two"
        )

        second.addresses = ["169.254.44.9"]
        let updated = try await store.updateProfile(second)

        XCTAssertEqual(updated.addresses, ["169.254.44.9", "10.0.0.3"])
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testObservedAddressesDoNotReserveOtherProfilesEndpoints() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        var second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        second.addresses = ["10.0.0.2"]

        let updated = try await store.updateProfile(second)
        XCTAssertEqual(updated.addresses, ["10.0.0.2", "10.0.0.3"])
        XCTAssertEqual(store.profile(id: first.id), first)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testDeleteRestoresConfigDirectoryWhenRegistryPersistFails() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        let profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        try "TC_HOST=root@10.0.0.2\n".write(to: profile.configURL, atomically: true, encoding: .utf8)
        try FileManager.default.removeItem(at: store.registryURL)
        try FileManager.default.createDirectory(at: store.registryURL, withIntermediateDirectories: false)

        do {
            try await store.delete(profile)
            XCTFail("Expected registry delete failure.")
        } catch {
            XCTAssertNotNil(error)
        }

        XCTAssertNotNil(store.profile(id: profile.id))
        XCTAssertTrue(FileManager.default.fileExists(atPath: profile.configURL.deletingLastPathComponent().path))
        XCTAssertEqual(try String(contentsOf: profile.configURL, encoding: .utf8), "TC_HOST=root@10.0.0.2\n")
    }

    func testUpdateProfileMissingIDFailsWithoutCreatingProfile() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url)
        await store.load()
        var profile = DeviceProfile.make(
            id: "missing",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: temp.url,
            date: Date(timeIntervalSince1970: 10)
        )
        profile.displayName = "Unsaved"

        do {
            _ = try await store.updateProfile(profile)
            XCTFail("Expected missing profile update to fail.")
        } catch {
            XCTAssertEqual(error as? DeviceRegistryError, .profileNotFound("missing"))
        }
        XCTAssertEqual(store.state, .empty)
        XCTAssertEqual(store.profiles, [])
    }

    func testUpdateProfilePreservesOtherProfilesForLocalEdits() async throws {
        let temp = try TemporaryDirectory()
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: {
            Date(timeIntervalSince1970: 100)
        })
        await store.load()
        var first = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        let second = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.3"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-two"
        )

        first.displayName = "Office"
        first.settings.mountWaitSeconds = 45
        let updated = try await store.updateProfile(first)

        XCTAssertEqual(updated.displayName, "Office")
        XCTAssertEqual(updated.settings.mountWaitSeconds, 45)
        XCTAssertEqual(store.profile(id: second.id), second)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testLoadMarksInProgressDeployStateInterrupted() async throws {
        let temp = try TemporaryDirectory()
        let start = Date(timeIntervalSince1970: 200)
        let interruptedAt = Date(timeIntervalSince1970: 300)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: { start })
        await store.load()
        let profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        var inProgress = testDeployState(
            status: .deploying,
            startedAt: start,
            updatedAt: start,
            finishedAt: nil,
            stage: "read_mast",
            verified: nil,
            summary: "Old summary",
            errorMessage: "Old error",
            diagnosticText: "Old diagnostic"
        )
        inProgress.operationID = "interrupted-attempt"
        inProgress.summaryRef = BackendSummary(key: "deploy.result.default_message", text: inProgress.summary)
        await store.updateDeployState(inProgress, for: profile.id)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url, now: { interruptedAt })
        await reloaded.load()

        let deployState = try XCTUnwrap(reloaded.profile(id: profile.id)?.lastDeployState)
        XCTAssertEqual(deployState.status, .interrupted)
        XCTAssertEqual(deployState.operationID, inProgress.operationID)
        XCTAssertEqual(deployState.startedAt, start)
        XCTAssertEqual(deployState.updatedAt, interruptedAt)
        XCTAssertEqual(deployState.finishedAt, interruptedAt)
        XCTAssertEqual(deployState.stage, "read_mast")
        XCTAssertEqual(deployState.errorCode, "operation_interrupted")
        XCTAssertEqual(deployState.summary, "")
        XCTAssertNil(deployState.summaryRef)
        XCTAssertNil(deployState.errorMessage)
        XCTAssertNil(deployState.diagnosticText)
        XCTAssertEqual(deployState.localizedSummary, "The Samba installation or update was interrupted before it completed.")
        let runtimeState = try XCTUnwrap(reloaded.profile(id: profile.id)?.runtimeState)
        XCTAssertEqual(runtimeState.state, .installInterrupted)
        XCTAssertEqual(runtimeState.stage, "read_mast")
        XCTAssertEqual(runtimeState.errorCode, "operation_interrupted")
        XCTAssertEqual(runtimeState.localizedSummary, "The Samba installation or update was interrupted before it completed.")
    }

    func testInterruptedRuntimeStateOverridesSuccessfulCheckupAfterReload() async throws {
        let temp = try TemporaryDirectory()
        let start = Date(timeIntervalSince1970: 200)
        let interruptedAt = Date(timeIntervalSince1970: 300)
        let store = DeviceRegistryStore(applicationSupportURL: temp.url, now: { start })
        await store.load()
        let profile = try await store.storeTestProfile(
            configuredDevice: testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            passwordState: .available,
            preferredID: "device-one"
        )
        await store.updateCheckup(DeviceCheckupSnapshot(
            checkedAt: Date(timeIntervalSince1970: 100),
            state: .passed,
            passCount: 3,
            warnCount: 0,
            failCount: 0,
            summary: "healthy"
        ), for: profile.id)
        await store.updateDeployState(testDeployState(
            status: .deploying,
            startedAt: start,
            updatedAt: start,
            finishedAt: nil,
            stage: "read_mast",
            verified: nil,
            summary: ""
        ), for: profile.id)
        var installing = testRuntimeState(
            state: .installing,
            stage: "read_mast",
            verified: nil,
            summary: "Old summary",
            errorMessage: "Old error"
        )
        installing.summaryRef = BackendSummary(key: "deploy.result.default_message", text: installing.summary)
        await store.updateRuntimeState(installing, for: profile.id)

        let reloaded = DeviceRegistryStore(applicationSupportURL: temp.url, now: { interruptedAt })
        await reloaded.load()

        let reloadedProfile = try XCTUnwrap(reloaded.profile(id: profile.id))
        XCTAssertEqual(reloadedProfile.lastCheckup?.state, .passed)
        XCTAssertEqual(reloadedProfile.lastDeployState?.status, .interrupted)
        XCTAssertEqual(reloadedProfile.runtimeState?.state, .installInterrupted)
        XCTAssertEqual(reloadedProfile.runtimeState?.source, .appRecovery)
        XCTAssertEqual(reloadedProfile.runtimeState?.stage, installing.stage)
        XCTAssertEqual(reloadedProfile.runtimeState?.summary, "")
        XCTAssertNil(reloadedProfile.runtimeState?.summaryRef)
        XCTAssertNil(reloadedProfile.runtimeState?.errorMessage)
        XCTAssertEqual(DeviceStatusPolicy.status(
            for: reloadedProfile,
            passwordState: .available,
            activeOperation: nil
        ), .failed)
    }

    private func discovered(record: JSONValue) throws -> DiscoveredDevice {
        let resolved = try record.decode(BonjourResolvedServicePayload.self)
        return DiscoveredDevice(record: resolved, index: 0)
    }

    private func hostnamePeer(host: String, fullname: String) throws -> DiscoveredDevice {
        let record = testDeviceRecord(hostname: "SHARED.local.", ipv4: [host], fullname: fullname)
        let ordinary = try discovered(record: record)
        return DiscoveredDevice(id: host, name: ordinary.name, connectionTarget: host, sshHost: "root@\(host)",
            hostname: ordinary.hostname, networkAddresses: ordinary.networkAddresses, syap: ordinary.syap,
            model: ordinary.model, rawRecord: record)
    }
}
