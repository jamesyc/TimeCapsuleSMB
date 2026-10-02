import XCTest
@testable import TimeCapsuleSMBApp

/// The helper rejects request params it does not read. Every request the app
/// builds must use only the names in `operation_params.json`, which
/// `tests/fixtures/operation_params.py` generates from the helper's operations.
final class OperationParamsContractTests: XCTestCase {
    private struct Contract: Decodable {
        let common: [String]
        let operations: [String: [String]]
    }

    private func contract() throws -> Contract {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "operation_params", withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode(Contract.self, from: Data(contentsOf: url))
    }

    /// One request per builder branch, with every optional param filled in.
    private func requests() throws -> [(operation: String, params: [String: JSONValue])] {
        let profile = DeviceProfile.make(
            id: "device-one",
            configuredDevice: try testConfiguredDevice(host: "10.0.0.2"),
            discoveredDevice: nil,
            applicationSupportURL: URL(fileURLWithPath: "/tmp/timecapsulesmb-tests", isDirectory: true)
        )
        let preflight = LocalNetworkPreflightResult(
            status: .denied,
            detail: "blocked",
            durationMilliseconds: 12,
            serviceType: "_smb._tcp"
        )
        var settingsWithoutStandby = DeviceProfileSettings.default
        settingsWithoutStandby.ataStandby = nil
        return [
            ("version-check", OperationParams.Readiness.versionCheck(url: "https://example.com/version.json")),
            ("set-telemetry", OperationParams.Readiness.setTelemetry(enabled: true)),
            ("discover", OperationParams.Discovery.discover(timeout: 5)),
            ("reachability", OperationParams.Reachability.check(profile: profile)),
            ("set-ssh", OperationParams.SetSSH.status()),
            ("set-ssh", OperationParams.SetSSH.enable(noWait: true)),
            ("configure", OperationParams.Configure.save(
                host: "10.0.0.2",
                password: "pw",
                debugLogging: true,
                internalShareUseDiskRoot: true,
                smbBrowseCompatibility: true,
                mdnsAdvertiseAFP: true,
                anyProtocol: true,
                requireSMBEncryption: true,
                forceDisableSMBSigningAndEncryption: true,
                fruitMetadataNetatalk: true,
                vfsAIOForkEnabled: true,
                ataIdleSeconds: 600,
                ataStandby: 5,
                localNetworkPreflight: preflight
            )),
            ("configure", OperationParams.Configure.save(
                selectedRecord: .object(["name": .string("Time Capsule")]),
                password: "pw",
                debugLogging: false,
                includeAtaStandby: true
            )),
            ("update-config-settings", OperationParams.Configure.updateSettings(.default)),
            ("update-config-settings", OperationParams.Configure.updateSettings(settingsWithoutStandby)),
            ("doctor", OperationParams.Doctor.run(skipSSH: true, skipBonjour: true, skipSMB: true)),
            ("deploy", OperationParams.Deploy.params(noWait: true, debugLogging: true, ataIdleSeconds: 600, ataStandby: 5, mountWait: 30)),
            ("deploy", OperationParams.Deploy.params(noWait: false, debugLogging: false, ataIdleSeconds: 600, ataStandby: nil, mountWait: 30)),
            ("activate", OperationParams.Activation.params()),
            ("uninstall", OperationParams.Uninstall.params(noReboot: true, noWait: true, mountWait: 30)),
            ("fsck", OperationParams.Fsck.listVolumes(mountWait: 30)),
            ("fsck", OperationParams.Fsck.run(dryRun: false, volume: "dk2", noReboot: false, noWait: true, mountWait: 30)),
            ("flash", OperationParams.Flash.backup()),
            ("flash", OperationParams.Flash.plan(backupDir: "/tmp/b", mode: .checkApple, firmwareVersion: "7.8.1", firmwareTemplate: "/tmp/t")),
            ("flash", OperationParams.Flash.write(backupDir: "/tmp/b", mode: .restore, firmwareVersion: "7.8.1", firmwareTemplate: "/tmp/t")),
        ]
    }

    private enum ParamCheck: Equatable {
        case accepted
        case unknownOperation
        case unknownParams([String])
    }

    private func check(_ params: [String: JSONValue], operation: String, against contract: Contract) -> ParamCheck {
        guard let accepted = contract.operations[operation] else {
            return .unknownOperation
        }
        let unknown = Set(params.keys).subtracting(accepted).subtracting(contract.common)
        return unknown.isEmpty ? .accepted : .unknownParams(unknown.sorted())
    }

    func testEveryRequestTheAppBuildsUsesOnlyParamsTheHelperAccepts() throws {
        let contract = try contract()
        for (operation, params) in try requests() {
            XCTAssertEqual(check(params, operation: operation, against: contract), .accepted, operation)
        }
    }

    func testEveryHelperOperationHasABuilderOrIsSentWithoutParams() throws {
        let contract = try contract()
        let covered = Set(try requests().map(\.operation))
        // The app sends these with no params, so there is no builder to check.
        let sentWithoutParams: Set<String> = ["capabilities", "validate-install"]

        XCTAssertEqual(Set(contract.operations.keys).subtracting(covered), sentWithoutParams)
    }

    func testTheCheckReportsMisspelledParamsAndUnknownOperations() throws {
        let contract = try contract()

        XCTAssertEqual(
            check(["no_rebot": .bool(true), "no_wait": .bool(true)], operation: "uninstall", against: contract),
            .unknownParams(["no_rebot"])
        )
        XCTAssertEqual(
            check(["volume": .string("dk2")], operation: "uninstall", against: contract),
            .unknownParams(["volume"]),
            "a param only another operation takes"
        )
        XCTAssertEqual(
            check(["no_reboot": .bool(true), "config": .string("/tmp/.env")], operation: "uninstall", against: contract),
            .accepted
        )
        XCTAssertEqual(check([:], operation: "add-device", against: contract), .unknownOperation)
    }

    @MainActor
    func testParamsTheClientAddsToAConfirmedRequestAreAcceptedByEveryOperation() async throws {
        let contract = try contract()
        let built = OperationParams.Uninstall.params(noReboot: false, noWait: false, mountWait: 30)
        let runner = StoreTestRunner(responses: [
            .init(events: [
                BackendEvent(
                    type: "error",
                    operation: "uninstall",
                    code: "confirmation_required",
                    message: "Confirm uninstall.",
                    details: .object(["confirmation_id": .string("confirm-1")])
                )
            ], result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")),
            .init(events: [
                BackendEvent(type: "result", operation: "uninstall", ok: true, payload: .object([:]))
            ])
        ])
        let client = BackendClient(runner: runner)
        let context = DeviceRuntimeContext(profileID: "device-one", configURL: URL(fileURLWithPath: "/tmp/device-one/.env"))

        client.run(
            operation: "uninstall",
            params: OperationCredentialInjector.injectingPassword("pw", into: built),
            context: context
        )
        try await waitUntilStoreState { client.pendingConfirmation != nil && !client.isRunning }
        client.confirmPending()
        try await waitUntilStoreState { !client.isRunning && runner.calls.count == 2 }

        let added = Set(runner.calls[1].params.keys).subtracting(built.keys)
        XCTAssertFalse(added.isEmpty)
        XCTAssertTrue(added.isSubset(of: Set(contract.common)), "not accepted by every operation: \(added.subtracting(contract.common).sorted())")
        XCTAssertEqual(check(runner.calls[1].params, operation: "uninstall", against: contract), .accepted)
    }
}
