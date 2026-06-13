import XCTest
@testable import TimeCapsuleSMBApp

final class BackendPayloadTests: XCTestCase {
    func testDecodesReadinessPayloads() throws {
        let capabilities = try jsonValue("""
        {
          "schema_version": 1,
          "api_schema_version": 1,
          "helper_version": "1.2.3",
          "helper_version_code": 123,
          "operations": ["discover", "configure"],
          "distribution_root": "/repo",
          "artifact_manifest_sha256": "abc",
          "confirmation_schema_version": 1,
          "summary": "Helper capabilities resolved."
        }
        """).decode(CapabilitiesPayload.self)

        XCTAssertEqual(capabilities.helperVersion, "1.2.3")
        XCTAssertEqual(capabilities.operations, ["discover", "configure"])

        let validation = try jsonValue("""
        {
          "schema_version": 1,
          "ok": false,
          "checks": [{"id": "artifact_hashes", "ok": false, "message": "artifact validation failed", "details": {"failures": ["bad hash"]}}],
          "counts": {"checks": 1, "pass": 0, "fail": 1},
          "summary": "Install validation failed."
        }
        """).decode(InstallValidationPayload.self)

        XCTAssertFalse(validation.ok)
        XCTAssertEqual(validation.checks[0].details, .object(["failures": .array([.string("bad hash")])]))

        let reachability = try jsonValue("""
        {
          "schema_version": 1,
          "status": "partial",
          "ssh_host": "root@10.0.0.2",
          "smb_host": "10.0.0.2",
          "checks": [{"id": "ping", "status": "PASS", "message": "Host responds to ping.", "host": "10.0.0.2"}],
          "counts": {"PASS": 1},
          "summary": "SSH reachable, SMB port closed."
        }
        """).decode(ReachabilityPayload.self)

        XCTAssertEqual(reachability.status, "partial")
        XCTAssertEqual(reachability.checks[0].id, "ping")

        let sshAccess = try jsonValue("""
        {
          "schema_version": 1,
          "host": "10.0.0.2",
          "acp_port_reachable": true,
          "ssh_port_reachable": false,
          "acp_port_error": null,
          "ssh_port_error": "Connection refused",
          "ssh_disabled_likely": true,
          "summary": "AirPort ACP is reachable, but SSH is closed."
        }
        """).decode(SSHAccessPayload.self)

        XCTAssertEqual(sshAccess.host, "10.0.0.2")
        XCTAssertTrue(sshAccess.isSSHDisabledLikely)
    }

    func testDecodesDiscoveryAndConfigurePayloads() throws {
        let discovery = try jsonValue("""
        {
          "schema_version": 1,
          "instances": [{"service_type": "_airport._tcp.local.", "name": "TC", "fullname": "TC._airport._tcp.local."}],
          "resolved": [{
            "name": "TC",
            "hostname": "tc.local.",
            "service_type": "_airport._tcp.local.",
            "port": 5009,
            "ipv4": ["10.0.0.2"],
            "ipv6": [],
            "services": ["_airport._tcp.local."],
            "properties": {"syAP": "119", "model": "Time Capsule"},
            "fullname": "TC._airport._tcp.local."
          }],
          "devices": [{
            "id": "bonjour:tc._airport._tcp.local",
            "name": "TC",
            "host": "10.0.0.2",
            "ssh_host": "root@10.0.0.2",
            "hostname": "tc.local.",
            "addresses": ["10.0.0.2"],
            "ipv4": ["10.0.0.2"],
            "ipv6": [],
            "preferred_ipv4": "10.0.0.2",
            "link_local_only": false,
            "syap": "119",
            "model": "Time Capsule",
            "service_type": "_airport._tcp.local.",
            "fullname": "TC._airport._tcp.local.",
            "selected_record": {
              "name": "TC",
              "hostname": "tc.local.",
              "service_type": "_airport._tcp.local.",
              "port": 5009,
              "ipv4": ["10.0.0.2"],
              "ipv6": [],
              "services": ["_airport._tcp.local."],
              "properties": {"syAP": "119", "model": "Time Capsule"},
              "fullname": "TC._airport._tcp.local."
            }
          }],
          "counts": {"instances": 1, "resolved": 1, "devices": 1},
          "summary": "Discovered 1 device(s)."
        }
        """).decode(DiscoverPayload.self)

        XCTAssertEqual(discovery.resolved[0].name, "TC")
        XCTAssertEqual(discovery.devices[0].host, "10.0.0.2")
        XCTAssertEqual(discovery.devices[0].selectedRecord.stringValue(for: "fullname"), "TC._airport._tcp.local.")
        XCTAssertEqual(discovery.resolved[0].properties["syAP"], "119")
        XCTAssertEqual(discovery.resolved[0].jsonValue.stringValue(for: "name"), "TC")

        let configure = try jsonValue("""
        {
          "schema_version": 1,
          "config_path": "/app/.env",
          "host": "root@10.0.0.2",
          "configure_id": "cfg-1",
          "ssh_authenticated": true,
          "device_syap": "119",
          "device_model": "Time Capsule",
          "compatibility": {
            "os_name": "NetBSD",
            "os_release": "6.0",
            "arch": "evbarm",
            "elf_endianness": "little",
            "payload_family": "netbsd6_samba4",
            "device_generation": "gen5",
            "supported": true,
            "reason_code": "supported_netbsd6",
            "reason_detail": "",
            "syap_candidates": ["119"],
            "model_candidates": ["Time Capsule"]
          },
          "device": {"host": "root@10.0.0.2", "syap": "119", "model": "Time Capsule"},
          "summary": "Configuration saved and SSH authentication verified."
        }
        """).decode(ConfigurePayload.self)

        XCTAssertEqual(configure.host, "root@10.0.0.2")
        XCTAssertEqual(configure.compatibility?.payloadFamily, "netbsd6_samba4")
        XCTAssertEqual(ConfiguredDeviceState(payload: configure).model, "Time Capsule")
    }

    func testDecodesDeployDoctorAndMaintenancePayloads() throws {
        let deployPlan = try jsonValue("""
        {
          "schema_version": 1,
          "host": "root@10.0.0.2",
          "volume_root": "/Volumes/dk2",
          "payload_dir": "/Volumes/dk2/.samba4",
          "payload_family": "netbsd6_samba4",
          "netbsd4": false,
          "requires_reboot": true,
          "reboot_required": true,
          "startup_mode": "reboot_then_verify",
          "uploads": [{"description": "smbd"}],
          "pre_upload_actions": [{"type": "stop_process"}],
          "post_upload_actions": [],
          "activation_actions": [],
          "post_deploy_checks": [{"id": "ssh_returns_after_reboot", "description": "SSH returns after reboot"}],
          "summary": "Deployment dry-run plan generated."
        }
        """).decode(DeployPlanPayload.self)

        XCTAssertEqual(deployPlan.payloadFamily, "netbsd6_samba4")
        XCTAssertTrue(deployPlan.requiresReboot)
        XCTAssertEqual(deployPlan.startupMode, .rebootThenVerify)
        XCTAssertEqual(deployPlan.uploads.count, 1)

        let deployResult = try jsonValue("""
        {
          "schema_version": 1,
          "payload_dir": "/Volumes/dk2/.samba4",
          "netbsd4": false,
          "payload_family": "netbsd6_samba4",
          "requires_reboot": true,
          "rebooted": true,
          "reboot_requested": true,
          "waited": true,
          "verified": true,
          "summary": "Deployment completed."
        }
        """).decode(DeployResultPayload.self)

        XCTAssertEqual(deployResult.rebootRequested, true)
        XCTAssertEqual(deployResult.verified, true)

        let doctor = try jsonValue("""
        {
          "schema_version": 1,
          "fatal": true,
          "results": [{"status": "FAIL", "message": "smbd is not running", "details": {"domain": "runtime"}}],
          "counts": {"FAIL": 1},
          "error": "smbd is not running",
          "summary": "Doctor found one or more fatal problems."
        }
        """).decode(DoctorPayload.self)

        XCTAssertTrue(doctor.fatal)
        XCTAssertEqual(doctor.results[0].details, .object(["domain": .string("runtime")]))

        let fsckTargets = try jsonValue("""
        {
          "schema_version": 1,
          "targets": [{"device": "/dev/dk2", "mountpoint": "/Volumes/dk2", "name": "Data", "builtin": true}],
          "counts": {"targets": 1},
          "summary": "Found 1 mounted HFS volume(s)."
        }
        """).decode(FsckVolumeListPayload.self)

        XCTAssertEqual(fsckTargets.targets[0].device, "/dev/dk2")

        let maintenance = try jsonValue("""
        {
          "schema_version": 1,
          "summary": "Uninstall completed.",
          "requires_reboot": true,
          "rebooted": true,
          "reboot_requested": true,
          "waited": true,
          "verified": true,
          "counts": {"payload_dirs": 1}
        }
        """).decode(MaintenanceResultPayload.self)

        XCTAssertEqual(maintenance.rebooted, true)
        XCTAssertEqual(maintenance.counts?["payload_dirs"], 1)
    }

    func testDecodesRecoveryAndReportsContractFailures() throws {
        let event = BackendEvent(
            type: "error",
            operation: "deploy",
            code: "remote_error",
            message: "failed",
            recovery: try jsonValue("""
            {
              "title": "No HFS volumes found",
              "message": "The device did not report a deployable HFS disk.",
              "actions": ["Wake the disk.", "Retry deploy."],
              "retryable": true,
              "suggested_operation": "deploy",
              "docs_anchor": "deploy"
            }
            """)
        )

        let error = BackendErrorViewModel(event: event)

        XCTAssertEqual(error.recovery?.title, "No HFS volumes found")
        XCTAssertEqual(error.recovery?.actions, ["Wake the disk.", "Retry deploy."])
        XCTAssertEqual(error.recovery?.suggestedOperation, "deploy")

        XCTAssertThrowsError(try BackendEvent(type: "result", operation: "capabilities", ok: true).decodePayload(CapabilitiesPayload.self)) { thrown in
            XCTAssertEqual(thrown as? BackendContractError, .missingPayload(operation: "capabilities"))
        }

        XCTAssertThrowsError(
            try BackendEvent(
                type: "result",
                operation: "capabilities",
                ok: true,
                payload: .object(["schema_version": .string("wrong")])
            ).decodePayload(CapabilitiesPayload.self)
        ) { thrown in
            guard case BackendContractError.payloadDecodeFailed(let operation, let payloadType, _)? = thrown as? BackendContractError else {
                return XCTFail("Expected payloadDecodeFailed, got \(thrown)")
            }
            XCTAssertEqual(operation, "capabilities")
            XCTAssertEqual(payloadType, "CapabilitiesPayload")
        }
    }

    private func jsonValue(_ text: String) throws -> JSONValue {
        let data = Data(text.utf8)
        return try JSONDecoder().decode(JSONValue.self, from: data)
    }
}
