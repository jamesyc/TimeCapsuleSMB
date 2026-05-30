from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app import service
from timecapsulesmb.core.config import AppConfig, DEFAULTS
from timecapsulesmb.services import reachability


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


class ReachabilityTests(unittest.TestCase):
    def test_reachability_passes_when_ssh_and_smb_work(self) -> None:
        config = AppConfig.from_values({
            "TC_HOST": "root@tc.local",
            "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"],
        })

        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=("10.0.0.2",)):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.subprocess.run",
                    return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
                ):
                    with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                        with mock.patch(
                            "timecapsulesmb.services.reachability.run_ssh",
                            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=reachability.REACHABILITY_OK_TOKEN, stderr=""),
                        ):
                            result = reachability.run_reachability(
                                config,
                                {"smb_hosts": ["tc.local"]},
                                password="pw",
                            )

        self.assertEqual(result.status, "reachable")
        self.assertEqual(result.summary, "SSH reachable; SMB port reachable.")
        self.assertEqual({check.id: check.status for check in result.checks}, {
            "dns": "PASS",
            "ping": "PASS",
            "ssh_port": "PASS",
            "ssh_auth": "PASS",
            "smb_port": "PASS",
        })

    def test_missing_password_skips_auth_but_checks_ports(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                    with mock.patch("timecapsulesmb.services.reachability.run_ssh") as ssh:
                        result = reachability.run_reachability(config, {}, password="")

        ssh.assert_not_called()
        self.assertEqual(result.status, "reachable")
        self.assertEqual(result.summary, "SSH reachable; SMB port reachable.")
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "SKIP")

    def test_partial_when_ssh_port_works_but_smb_port_is_closed(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        def tcp(host: str, port: int, *, timeout: float) -> str | None:
            return None if port == 22 else "connection refused"

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", side_effect=tcp):
                    result = reachability.run_reachability(config, {}, password="")

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.summary, "SSH reachable, SMB port closed.")

    def test_ssh_proxy_skips_direct_port_check_but_auth_can_pass(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": "-J jump"})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value="connection refused") as tcp:
                    with mock.patch(
                        "timecapsulesmb.services.reachability.run_ssh",
                        return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=reachability.REACHABILITY_OK_TOKEN, stderr=""),
                    ) as ssh:
                        result = reachability.run_reachability(config, {}, password="pw")

        self.assertEqual(tcp.call_count, 1)
        ssh.assert_called_once()
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_port"], "SKIP")
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "PASS")
        self.assertEqual(result.status, "partial")

    def test_ping_is_secondary_when_tcp_services_fail(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value="connection refused"):
                    result = reachability.run_reachability(config, {}, password="")

        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.summary, "Could not reach SSH or SMB.")

    def test_all_failed_checks_return_unreachable_without_raising(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@tc.local", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=()):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.subprocess.run",
                    return_value=subprocess.CompletedProcess(["ping"], 1, stderr=b"timeout"),
                ):
                    with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value="connection timed out"):
                        result = reachability.run_reachability(config, {}, password="")

        self.assertEqual(result.status, "unreachable")
        self.assertEqual({check.id: check.status for check in result.checks}["dns"], "FAIL")
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "SKIP")

    def test_invalid_timeout_params_fall_back_to_defaults(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ) as ping:
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None) as tcp:
                    result = reachability.run_reachability(
                        config,
                        {"tcp_timeout": "not-a-number", "ssh_timeout": "not-a-number"},
                        password="",
                    )

        self.assertEqual(result.status, "reachable")
        self.assertEqual(ping.call_args.kwargs["timeout"], 3.0)
        self.assertEqual(tcp.call_args.kwargs["timeout"], 2.0)

    def test_ipv6_candidates_use_ping6_when_available(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@[fd00::2]", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        def which(command: str) -> str | None:
            return f"/sbin/{command}" if command == "ping6" else None

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", side_effect=which):
            with mock.patch(
                "timecapsulesmb.services.reachability.subprocess.run",
                return_value=subprocess.CompletedProcess(["ping6"], 0, stderr=b""),
            ) as ping:
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                    reachability.run_reachability(config, {}, password="")

        self.assertEqual(ping.call_args.args[0][0], "/sbin/ping6")
        self.assertIn("fd00::2", ping.call_args.args[0])

    def test_app_operation_emits_stages_checks_and_payload(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.app.ops.common.load_optional_env_config", return_value=config):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.subprocess.run",
                    return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
                ):
                    with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                        with mock.patch(
                            "timecapsulesmb.services.reachability.run_ssh",
                            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=reachability.REACHABILITY_OK_TOKEN, stderr=""),
                        ):
                            rc = service.run_api_request(
                                {"operation": "reachability", "params": {"credentials": {"password": "pw"}}},
                                collector.sink,
                            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            [event["stage"] for event in collector.events_of_type("stage")],
            ["load_config", "build_candidates", "check_dns", "check_ping", "check_ssh_port", "check_ssh_auth", "check_smb_port"],
        )
        self.assertEqual(len(collector.events_of_type("check")), 5)
        result = collector.events_of_type("result")[0]
        self.assertEqual(result["payload"]["status"], "reachable")
        self.assertEqual(result["payload"]["counts"]["PASS"], 5)

    def test_reachability_does_not_import_zeroconf(self) -> None:
        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", side_effect=AssertionError("no dns needed")):
            result = reachability.run_reachability(
                AppConfig.from_values({"TC_HOST": "", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]}),
                {},
            )

        self.assertEqual(result.status, "skipped")


if __name__ == "__main__":
    unittest.main()
