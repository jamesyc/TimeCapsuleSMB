import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class StoreTestRunner: HelperRunning, @unchecked Sendable {
    struct Call: Equatable, Sendable {
        let helperPath: String?
        let operation: String
        let params: [String: JSONValue]
        let context: DeviceRuntimeContext?
    }

    struct Response: Sendable {
        let events: [BackendEvent]
        let result: HelperRunResult
        let delayNanoseconds: UInt64

        init(
            events: [BackendEvent],
            result: HelperRunResult = HelperRunResult(exitCode: 0, sawTerminalEvent: true, stderr: ""),
            delayNanoseconds: UInt64 = 0
        ) {
            self.events = events
            self.result = result
            self.delayNanoseconds = delayNanoseconds
        }
    }

    private let queue = DispatchQueue(label: "TimeCapsuleSMBAppTests.StoreTestRunner")
    private var storedResponses: [Response]
    private var storedCalls: [Call] = []

    init(responses: [Response]) {
        self.storedResponses = responses
    }

    var calls: [Call] {
        queue.sync { storedCalls }
    }

    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        let response = queue.sync {
            storedCalls.append(Call(helperPath: helperPath, operation: operation, params: params, context: context))
            if storedResponses.isEmpty {
                return Response(
                    events: [BackendEvent.error(operation: operation, code: "missing_test_response", message: "No test response queued.")],
                    result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
                )
            }
            return storedResponses.removeFirst()
        }

        if response.delayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: response.delayNanoseconds)
        }
        if Task.isCancelled {
            await onEvent(BackendEvent.error(operation: operation, code: "cancelled", message: L10n.string("helper.error.cancelled")))
            return HelperRunResult(exitCode: 130, sawTerminalEvent: true, stderr: "")
        }
        for event in response.events {
            await onEvent(event)
        }
        return response.result
    }
}

final class OperationKeyedStoreTestRunner: HelperRunning, @unchecked Sendable {
    struct Key: Hashable, Sendable {
        let operation: String
        let profileID: String?

        init(_ operation: String, profileID: String? = nil) {
            self.operation = operation
            self.profileID = profileID
        }
    }

    typealias Call = StoreTestRunner.Call
    typealias Response = StoreTestRunner.Response

    private let queue = DispatchQueue(label: "TimeCapsuleSMBAppTests.OperationKeyedStoreTestRunner")
    private var storedResponses: [Key: [Response]]
    private var storedCalls: [Call] = []

    init(responses: [Key: [Response]]) {
        self.storedResponses = responses
    }

    var calls: [Call] {
        queue.sync { storedCalls }
    }

    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        let response = queue.sync {
            storedCalls.append(Call(helperPath: helperPath, operation: operation, params: params, context: context))
            let key = Key(operation, profileID: context?.profileID)
            if var responses = storedResponses[key], !responses.isEmpty {
                let response = responses.removeFirst()
                storedResponses[key] = responses
                return response
            }
            let fallbackKey = Key(operation)
            if var responses = storedResponses[fallbackKey], !responses.isEmpty {
                let response = responses.removeFirst()
                storedResponses[fallbackKey] = responses
                return response
            }
            return Response(
                events: [BackendEvent.error(operation: operation, code: "missing_test_response", message: "No keyed test response queued.")],
                result: HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
            )
        }

        if response.delayNanoseconds > 0 {
            try? await Task.sleep(nanoseconds: response.delayNanoseconds)
        }
        if Task.isCancelled {
            await onEvent(BackendEvent.error(operation: operation, code: "cancelled", message: L10n.string("helper.error.cancelled")))
            return HelperRunResult(exitCode: 130, sawTerminalEvent: true, stderr: "")
        }
        for event in response.events {
            await onEvent(event)
        }
        return response.result
    }
}

@MainActor
func waitUntilStoreState(
    timeoutNanoseconds: UInt64 = 2_000_000_000,
    _ condition: @escaping @MainActor () -> Bool
) async throws {
    let start = DispatchTime.now().uptimeNanoseconds
    while !condition() {
        if DispatchTime.now().uptimeNanoseconds - start > timeoutNanoseconds {
            XCTFail("Timed out waiting for store state change.")
            return
        }
        try await Task.sleep(nanoseconds: 10_000_000)
    }
}

func recoveryValue(
    title: String,
    actions: [String],
    suggestedOperation: String = "doctor",
    actionIDs: [String] = []
) -> JSONValue {
    return .object([
        "title": .string(title),
        "message": .string(title),
        "actions": .array(actions.map(JSONValue.string)),
        "action_ids": .array(actionIDs.map(JSONValue.string)),
        "retryable": .bool(true),
        "suggested_operation": .string(suggestedOperation)
    ])
}

func testDeviceRecord(
    name: String = "Office Capsule",
    hostname: String = "office-capsule.local.",
    ipv4: [String] = ["10.0.0.2"],
    ipv6: [String] = [],
    syap: String = "119",
    model: String = "Time Capsule",
    fullname: String = "Office Capsule._airport._tcp.local.",
    serviceType: String = "_airport._tcp.local.",
    services: [String] = ["_airport._tcp.local."]
) -> JSONValue {
    .object([
        "name": .string(name),
        "hostname": .string(hostname),
        "service_type": .string(serviceType),
        "port": .number(5009),
        "ipv4": .array(ipv4.map(JSONValue.string)),
        "ipv6": .array(ipv6.map(JSONValue.string)),
        "services": .array(services.map(JSONValue.string)),
        "properties": .object([
            "syAP": .string(syap),
            "model": .string(model)
        ]),
        "fullname": .string(fullname)
    ])
}

func testDiscoveredDevice(
    id: String = "bonjour:office-capsule._airport._tcp.local",
    name: String = "Office Capsule",
    host: String = "10.0.0.2",
    hostname: String = "office-capsule.local.",
    addresses: [String]? = nil,
    ipv4: [String]? = nil,
    ipv6: [String] = [],
    preferredIPv4: String? = nil,
    sshHost: String? = nil,
    linkLocalOnly: Bool = false,
    syap: String? = "119",
    model: String? = "Time Capsule",
    fullname: String = "Office Capsule._airport._tcp.local.",
    selectedRecord: JSONValue? = nil
) -> JSONValue {
    let hostIsIPv6 = host.contains(":")
    let resolvedIPv4 = ipv4 ?? (hostIsIPv6 ? [] : [host])
    let resolvedIPv6 = ipv6.isEmpty && hostIsIPv6 ? [host] : ipv6
    let resolvedPreferredIPv4 = preferredIPv4 ?? resolvedIPv4.first { !$0.hasPrefix("169.254.") }
    let resolvedAddresses = addresses ?? (resolvedIPv4 + resolvedIPv6)
    let resolvedSSHHost = sshHost ?? ((resolvedPreferredIPv4 != nil || !resolvedIPv6.isEmpty) ? "root@\(host)" : nil)
    let record = selectedRecord ?? testDeviceRecord(
        name: name,
        hostname: hostname,
        ipv4: resolvedIPv4,
        ipv6: resolvedIPv6,
        syap: syap ?? "",
        model: model ?? "",
        fullname: fullname
    )
    return .object([
        "id": .string(id),
        "name": .string(name),
        "host": .string(host),
        "ssh_host": resolvedSSHHost.map(JSONValue.string) ?? .null,
        "hostname": .string(hostname),
        "addresses": .array(resolvedAddresses.map(JSONValue.string)),
        "ipv4": .array(resolvedIPv4.map(JSONValue.string)),
        "ipv6": .array(resolvedIPv6.map(JSONValue.string)),
        "preferred_ipv4": resolvedPreferredIPv4.map(JSONValue.string) ?? .null,
        "link_local_only": .bool(linkLocalOnly),
        "syap": syap.map(JSONValue.string) ?? .null,
        "model": model.map(JSONValue.string) ?? .null,
        "service_type": .string("_airport._tcp.local."),
        "fullname": .string(fullname),
        "selected_record": record
    ])
}

func testDiscoverPayload(records: [JSONValue], devices: [JSONValue]? = nil) -> JSONValue {
    let deviceValues: [JSONValue]
    if let devices {
        deviceValues = devices
    } else {
        deviceValues = records.map { record -> JSONValue in
            let name = record.stringValue(for: "name") ?? "Office Capsule"
            let hostname = record.stringValue(for: "hostname") ?? "office-capsule.local."
            let fullname = record.stringValue(for: "fullname") ?? "\(name)._airport._tcp.local."
            let ipv4 = testStringArray(record, for: "ipv4")
            let ipv6 = testStringArray(record, for: "ipv6")
            let preferredIPv4 = ipv4.first { !$0.hasPrefix("169.254.") }
            let host = preferredIPv4 ?? ipv6.first ?? hostname
            let sshHost = preferredIPv4 != nil || !ipv6.isEmpty ? "root@\(host)" : nil
            return testDiscoveredDevice(
                id: "bonjour:\(fullname.lowercased())",
                name: name,
                host: host,
                hostname: hostname,
                addresses: ipv4 + ipv6,
                ipv4: ipv4,
                ipv6: ipv6,
                preferredIPv4: preferredIPv4,
                sshHost: sshHost,
                fullname: fullname,
                selectedRecord: record
            )
        }
    }
    return .object([
        "schema_version": .number(1),
        "instances": .array([]),
        "resolved": .array(records),
        "devices": .array(deviceValues),
        "counts": .object([
            "instances": .number(0),
            "resolved": .number(Double(records.count)),
            "devices": .number(Double(deviceValues.count))
        ]),
        "summary": .string("discovered \(deviceValues.count) Time Capsule device(s).")
    ])
}

func testStringArray(_ value: JSONValue, for key: String) -> [String] {
    guard case .object(let object) = value,
          case .array(let values)? = object[key] else {
        return []
    }
    return values.compactMap { item in
        guard case .string(let string) = item else {
            return nil
        }
        return string
    }
}

func testConfigurePayload(
    host: String = "10.0.0.2",
    configPath: String = "/tmp/profile/.env",
    syap: String = "119",
    model: String = "Time Capsule",
    payloadFamily: String = "netbsd6_samba4"
) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "config_path": .string(configPath),
        "host": .string(host),
        "configure_id": .string("cfg-1"),
        "ssh_authenticated": .bool(true),
        "device_syap": .string(syap),
        "device_model": .string(model),
        "compatibility": .object([
            "os_name": .string("NetBSD"),
            "os_release": .string("6.0"),
            "arch": .string("powerpc"),
            "elf_endianness": .string("big"),
            "payload_family": .string(payloadFamily),
            "device_generation": .string("tc_gen4"),
            "supported": .bool(true),
            "syap_candidates": .array([.string(syap)]),
            "model_candidates": .array([.string(model)])
        ]),
        "device": .object([
            "host": .string(host),
            "syap": .string(syap),
            "model": .string(model)
        ]),
        "summary": .string("configuration saved and SSH authentication verified.")
    ])
}

func testConfiguredDevice(
    host: String = "10.0.0.2",
    configPath: String = "/tmp/profile/.env",
    syap: String = "119",
    model: String = "Time Capsule",
    payloadFamily: String = "netbsd6_samba4"
) throws -> ConfiguredDeviceState {
    ConfiguredDeviceState(payload: try testConfigurePayload(
        host: host,
        configPath: configPath,
        syap: syap,
        model: model,
        payloadFamily: payloadFamily
    ).decode(ConfigurePayload.self))
}

func testDoctorPayload(fatal: Bool = false, checks: [JSONValue]) -> JSONValue {
    let pass = checks.filter { $0.stringValue(for: "status") == "PASS" }.count
    let warn = checks.filter { $0.stringValue(for: "status") == "WARN" }.count
    let fail = checks.filter { $0.stringValue(for: "status") == "FAIL" }.count
    let info = checks.filter { $0.stringValue(for: "status") == "INFO" }.count
    return .object([
        "schema_version": .number(1),
        "fatal": .bool(fatal),
        "results": .array(checks),
        "counts": .object([
            "PASS": .number(Double(pass)),
            "WARN": .number(Double(warn)),
            "FAIL": .number(Double(fail)),
            "INFO": .number(Double(info))
        ]),
        "error": fatal ? .string("doctor failed") : .null,
        "summary": .string(fatal ? "doctor found one or more fatal problems." : "doctor checks passed.")
    ])
}

func testDoctorCheck(status: String, message: String, domain: String) -> JSONValue {
    .object([
        "status": .string(status),
        "message": .string(message),
        "details": .object(["domain": .string(domain)])
    ])
}

func testDeployPlanPayload(
    payloadFamily: String = "netbsd6_samba4",
    netbsd4: Bool? = nil,
    requiresReboot: Bool = true,
    startupMode: DeployStartupMode? = nil
) -> JSONValue {
    let isNetBSD4 = netbsd4 ?? payloadFamily.localizedCaseInsensitiveContains("netbsd4")
    let resolvedStartupMode = startupMode ?? DeployStartupMode.fallback(
        netbsd4: isNetBSD4,
        requiresReboot: requiresReboot
    )
    return .object([
        "schema_version": .number(1),
        "host": .string("root@10.0.0.2"),
        "volume_root": .string("/Volumes/dk2"),
        "payload_dir": .string("/Volumes/dk2/.samba4"),
        "payload_family": .string(payloadFamily),
        "netbsd4": .bool(isNetBSD4),
        "requires_reboot": .bool(requiresReboot),
        "reboot_required": .bool(requiresReboot),
        "startup_mode": .string(resolvedStartupMode.rawValue),
        "uploads": .array([.object(["description": .string("smbd")])]),
        "pre_upload_actions": .array([]),
        "post_upload_actions": .array([]),
        "activation_actions": .array([]),
        "post_deploy_checks": .array([]),
        "summary": .string("deployment dry-run plan generated.")
    ])
}

func testDeployResultPayload(
    payloadFamily: String = "netbsd6_samba4",
    verified: Bool = true,
    netbsd4: Bool = false
) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "payload_dir": .string("/Volumes/dk2/.samba4"),
        "netbsd4": .bool(netbsd4),
        "payload_family": .string(payloadFamily),
        "requires_reboot": .bool(true),
        "rebooted": .bool(true),
        "reboot_requested": .bool(true),
        "waited": .bool(true),
        "verified": .bool(verified),
        "message": .string("Install completed."),
        "summary": .string("deployment completed.")
    ])
}

func testActivationPlanPayload() -> JSONValue {
    .object([
        "schema_version": .number(1),
        "actions": .array([.object(["type": .string("run_script")])]),
        "post_activation_checks": .array([
            .object(["id": .string("runtime_ready"), "description": .string("runtime ready")])
        ]),
        "counts": .object(["actions": .number(1)]),
        "summary": .string("NetBSD4 activation dry-run plan generated.")
    ])
}

func testActivationResultPayload(alreadyActive: Bool) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "already_active": .bool(alreadyActive),
        "summary": .string(alreadyActive ? "NetBSD4 payload was already active." : "NetBSD4 activation completed.")
    ])
}

func testUninstallPlanPayload() -> JSONValue {
    .object([
        "schema_version": .number(1),
        "host": .string("root@10.0.0.2"),
        "volume_roots": .array([.string("/Volumes/dk2")]),
        "payload_dirs": .array([.string("/Volumes/dk2/.samba4")]),
        "remote_actions": .array([.object(["type": .string("remove_path")])]),
        "requires_reboot": .bool(true),
        "reboot_required": .bool(true),
        "post_uninstall_checks": .array([
            .object(["id": .string("managed_files_absent"), "description": .string("managed files absent")])
        ]),
        "counts": .object(["payload_dirs": .number(1)]),
        "summary": .string("uninstall dry-run plan generated.")
    ])
}

func testUninstallResultPayload(waited: Bool, verified: Bool) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "summary": .string(verified ? "uninstall completed." : "uninstall completed without post-reboot verification."),
        "requires_reboot": .bool(true),
        "rebooted": .bool(false),
        "reboot_requested": .bool(true),
        "waited": .bool(waited),
        "verified": .bool(verified)
    ])
}

func testFsckListPayload(targets: [JSONValue]) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "targets": .array(targets),
        "counts": .object(["targets": .number(Double(targets.count))]),
        "summary": .string("found \(targets.count) mounted HFS volume(s).")
    ])
}

func testFsckTargetPayload(
    name: String?,
    device: String = "/dev/dk2",
    mountpoint: String = "/Volumes/dk2"
) -> JSONValue {
    var payload: [String: JSONValue] = [
        "device": .string(device),
        "mountpoint": .string(mountpoint),
        "builtin": .bool(true)
    ]
    if let name {
        payload["name"] = .string(name)
    }
    return .object(payload)
}

func testFsckPlanPayload(
    target: JSONValue? = nil,
    device: String = "/dev/dk2",
    mountpoint: String = "/Volumes/dk2"
) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "target": target ?? testFsckTargetPayload(name: "Data"),
        "device": .string(device),
        "mountpoint": .string(mountpoint),
        "reboot_required": .bool(true),
        "wait_after_reboot": .bool(false),
        "summary": .string("fsck dry-run plan generated.")
    ])
}

func testFsckResultPayload(returncode: Int) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "device": .string("/dev/dk2"),
        "mountpoint": .string("/Volumes/dk2"),
        "returncode": .number(Double(returncode)),
        "reboot_requested": .bool(false),
        "waited": .bool(false),
        "verified": .bool(false),
        "summary": .string("fsck completed.")
    ])
}

func testRepairXattrsPayload(findings: Int, repairable: Int) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "returncode": .number(0),
        "root": .string("/Volumes/Data"),
        "finding_count": .number(Double(findings)),
        "repairable_count": .number(Double(repairable)),
        "counts": .object([
            "findings": .number(Double(findings)),
            "repairable": .number(Double(repairable))
        ]),
        "stats": .object([:]),
        "report": .string("report"),
        "summary": .string("repair-xattrs found \(findings) issue(s), \(repairable) repairable."),
        "summary_text": .string("repair-xattrs found \(findings) issue(s), \(repairable) repairable.")
    ])
}
