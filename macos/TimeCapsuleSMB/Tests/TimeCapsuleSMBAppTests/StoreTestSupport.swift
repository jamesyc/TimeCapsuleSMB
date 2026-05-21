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

func recoveryValue(title: String, actions: [String], suggestedOperation: String = "doctor") -> JSONValue {
    return .object([
        "title": .string(title),
        "message": .string(title),
        "actions": .array(actions.map(JSONValue.string)),
        "retryable": .bool(true),
        "suggested_operation": .string(suggestedOperation)
    ])
}

func testDeviceRecord(
    name: String = "Office Capsule",
    hostname: String = "office-capsule.local.",
    ipv4: [String] = ["10.0.0.2"],
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
        "ipv6": .array([]),
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
    preferredIPv4: String? = "10.0.0.2",
    linkLocalOnly: Bool = false,
    syap: String? = "119",
    model: String? = "Time Capsule",
    fullname: String = "Office Capsule._airport._tcp.local.",
    selectedRecord: JSONValue? = nil
) -> JSONValue {
    let resolvedIPv4 = ipv4 ?? [host]
    let resolvedAddresses = addresses ?? (resolvedIPv4 + ipv6)
    let record = selectedRecord ?? testDeviceRecord(
        name: name,
        hostname: hostname,
        ipv4: resolvedIPv4,
        syap: syap ?? "",
        model: model ?? "",
        fullname: fullname
    )
    return .object([
        "id": .string(id),
        "name": .string(name),
        "host": .string(host),
        "ssh_host": preferredIPv4 == nil ? .null : .string("root@\(host)"),
        "hostname": .string(hostname),
        "addresses": .array(resolvedAddresses.map(JSONValue.string)),
        "ipv4": .array(resolvedIPv4.map(JSONValue.string)),
        "ipv6": .array(ipv6.map(JSONValue.string)),
        "preferred_ipv4": preferredIPv4.map(JSONValue.string) ?? .null,
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
            let host: String
            if case .object(let object) = record,
               case .array(let ipv4Values)? = object["ipv4"],
               let first = ipv4Values.compactMap({ value -> String? in
                   guard case .string(let address) = value else { return nil }
                   return address.hasPrefix("169.254.") ? nil : address
               }).first {
                host = first
            } else {
                host = hostname
            }
            return testDiscoveredDevice(
                id: "bonjour:\(fullname.lowercased())",
                name: name,
                host: host,
                hostname: hostname,
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

func testDeployPlanPayload(payloadFamily: String = "netbsd6_samba4") -> JSONValue {
    .object([
        "schema_version": .number(1),
        "host": .string("root@10.0.0.2"),
        "volume_root": .string("/Volumes/dk2"),
        "payload_dir": .string("/Volumes/dk2/.samba4"),
        "payload_family": .string(payloadFamily),
        "netbsd4": .bool(false),
        "requires_reboot": .bool(true),
        "reboot_required": .bool(true),
        "uploads": .array([.object(["description": .string("smbd")])]),
        "pre_upload_actions": .array([]),
        "post_upload_actions": .array([]),
        "activation_actions": .array([]),
        "post_deploy_checks": .array([]),
        "summary": .string("deployment dry-run plan generated.")
    ])
}

func testDeployResultPayload(payloadFamily: String = "netbsd6_samba4", verified: Bool = true) -> JSONValue {
    .object([
        "schema_version": .number(1),
        "payload_dir": .string("/Volumes/dk2/.samba4"),
        "netbsd4": .bool(false),
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
