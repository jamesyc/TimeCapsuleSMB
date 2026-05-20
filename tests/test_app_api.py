from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.app import helper, service
from timecapsulesmb.cli import main as cli_main
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState
from timecapsulesmb.discovery.bonjour import BonjourDiscoverySnapshot, BonjourResolvedService, BonjourServiceInstance
from timecapsulesmb.transport.ssh import SshConnection


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


def supported_compatibility(payload_family: str = "netbsd6_samba4") -> DeviceCompatibility:
    return DeviceCompatibility(
        os_name="NetBSD",
        os_release="6.0",
        arch="earmv4",
        elf_endianness="little",
        payload_family=payload_family,
        device_generation="gen5",
        supported=True,
        reason_code="supported_netbsd6",
        syap_candidates=("119",),
        model_candidates=("TimeCapsule8,119",),
    )


def probed_state() -> ProbedDeviceState:
    return ProbedDeviceState(
        probe_result=ProbeResult(
            ssh_port_reachable=True,
            ssh_authenticated=True,
            error=None,
            os_name="NetBSD",
            os_release="6.0",
            arch="earmv4",
            elf_endianness="little",
            airport_model="TimeCapsule8,119",
            airport_syap="119",
        ),
        compatibility=supported_compatibility(),
    )


class AppApiTests(unittest.TestCase):
    def test_event_redacts_password_fields(self) -> None:
        event = AppEvent("result", "configure", {
            "ok": True,
            "payload": {
                "password": "secret",
                "nested": {"TC_PASSWORD": "secret"},
            },
        })

        data = event.to_jsonable()

        self.assertEqual(data["payload"]["password"], "<redacted>")
        self.assertEqual(data["payload"]["nested"]["TC_PASSWORD"], "<redacted>")

    def test_result_event_preserves_falsey_payloads(self) -> None:
        collector = CollectingSink()

        collector.sink.result("paths", ok=True, payload=[])

        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"], [])

    def test_unknown_operation_emits_error_without_result(self) -> None:
        collector = CollectingSink()

        rc = service.run_api_request({"operation": "nope", "params": {}}, collector.sink)

        self.assertEqual(rc, 1)
        self.assertEqual(len(collector.events_of_type("error")), 1)
        self.assertEqual(collector.events_of_type("result"), [])

    def test_discover_operation_returns_snapshot_payload(self) -> None:
        collector = CollectingSink()
        snapshot = BonjourDiscoverySnapshot(
            instances=[BonjourServiceInstance("_airport._tcp.local.", "TC", "TC._airport._tcp.local.")],
            resolved=[
                BonjourResolvedService(
                    name="TC",
                    hostname="tc.local.",
                    service_type="_airport._tcp.local.",
                    port=5009,
                    ipv4=("10.0.0.2",),
                    properties={"syAP": "119"},
                )
            ],
        )

        with mock.patch("timecapsulesmb.app.service.discover_snapshot", return_value=snapshot):
            rc = service.run_api_request({"operation": "discover", "params": {"timeout": 0.1}}, collector.sink)

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["resolved"][0]["name"], "TC")
        self.assertEqual(result["payload"]["resolved"][0]["ipv4"], ["10.0.0.2"])

    def test_configure_writes_env_without_leaking_password_to_events(self) -> None:
        collector = CollectingSink()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / ".env"
            with mock.patch("timecapsulesmb.app.service.probe_connection_state", return_value=probed_state()):
                rc = service.run_api_request(
                    {
                        "operation": "configure",
                        "params": {
                            "config": str(config_path),
                            "host": "root@10.0.0.2",
                            "password": "goodpw",
                        },
                    },
                    collector.sink,
                )

            self.assertEqual(rc, 0)
            self.assertIn("TC_HOST=root@10.0.0.2", config_path.read_text())
            self.assertIn("TC_PASSWORD=goodpw", config_path.read_text())
            serialized_events = json.dumps(collector.events)
            self.assertNotIn("goodpw", serialized_events)

    def test_doctor_streams_check_events(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445}))
            return [CheckResult("PASS", "smbd is bound to TCP 445", {"port": 445})], False

        with mock.patch("timecapsulesmb.app.service.load_env_config", return_value=config):
            with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                with mock.patch("timecapsulesmb.app.service.resolve_env_connection", return_value=SshConnection("root@10.0.0.2", "pw", "-o foo")):
                    with mock.patch("timecapsulesmb.app.service.run_doctor_checks", side_effect=fake_run_doctor_checks):
                        rc = service.run_api_request({"operation": "doctor", "params": {}}, collector.sink)

        self.assertEqual(rc, 0)
        checks = collector.events_of_type("check")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[0]["details"], {"port": 445})

    def test_deploy_dry_run_returns_structured_plan_without_remote_actions(self) -> None:
        collector = CollectingSink()
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        target = SimpleNamespace(connection=connection, probe_state=probed_state())
        artifacts = {
            "smbd": SimpleNamespace(absolute_path=REPO_ROOT / "bin/samba4/smbd"),
            "mdns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/mdns/mdns-advertiser"),
            "nbns-advertiser": SimpleNamespace(absolute_path=REPO_ROOT / "bin/nbns/nbns-advertiser"),
        }

        with mock.patch("timecapsulesmb.app.service.load_env_config", return_value=AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})):
            with mock.patch("timecapsulesmb.app.service.resolve_validated_managed_target", return_value=target):
                with mock.patch("timecapsulesmb.app.service.resolve_app_paths", return_value=SimpleNamespace(distribution_root=REPO_ROOT)):
                    with mock.patch("timecapsulesmb.app.service.validate_artifacts", return_value=[("smbd", True, "ok")]):
                        with mock.patch("timecapsulesmb.app.service.resolve_payload_artifacts", return_value=artifacts):
                            with mock.patch("timecapsulesmb.app.service.run_remote_actions", side_effect=AssertionError("dry run should not run remote actions")):
                                rc = service.run_api_request(
                                    {"operation": "deploy", "params": {"dry_run": True, "yes": True}},
                                    collector.sink,
                                )

        self.assertEqual(rc, 0)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["host"], "root@10.0.0.2")
        self.assertEqual(result["payload"]["reboot_required"], True)

    def test_helper_reads_request_and_writes_ndjson(self) -> None:
        output = io.StringIO()
        fake_stdin = io.StringIO('{"operation":"paths","params":{}}')
        with mock.patch.object(sys, "stdin", fake_stdin):
            with mock.patch("timecapsulesmb.app.helper.run_api_request") as run_mock:
                run_mock.side_effect = lambda request, sink: (sink.result(request["operation"], ok=True, payload={"ok": True}) or 0)
                with redirect_stdout(output):
                    rc = helper.main([])

        self.assertEqual(rc, 0)
        line = json.loads(output.getvalue())
        self.assertEqual(line["type"], "result")
        self.assertEqual(line["operation"], "paths")

    def test_api_command_is_registered(self) -> None:
        self.assertIs(cli_main.COMMANDS["api"], helper.main)


if __name__ == "__main__":
    unittest.main()
