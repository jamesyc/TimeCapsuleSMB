from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timecapsulesmb.core.summaries import Summary
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app import service
from timecapsulesmb.core.config import AppConfig, DEFAULTS
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.app import contracts
from timecapsulesmb.device import probe
from timecapsulesmb.device.probe import DeployedVersionProbeResult
from timecapsulesmb.services import reachability
from timecapsulesmb.transport.errors import SshAuthenticationError, SshNetworkError


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


class ReachabilityTests(unittest.TestCase):
    def ssh_auth_succeeds(self):
        return mock.patch(
            "timecapsulesmb.services.reachability.run_ssh",
            return_value=subprocess.CompletedProcess(
                ["ssh"], 0, stdout=reachability.REACHABILITY_OK_TOKEN, stderr=""
            ),
        )

    def test_smb_port_without_ssh_is_partial_and_names_the_smb_host(self) -> None:
        def tcp(host: str, port: int, *_args, **_kwargs) -> str | None:
            return None if port == 445 else "connection refused"

        config = AppConfig.from_values({"TC_HOST": "root@tc.local", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=("10.0.0.2",)):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value=None):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", side_effect=tcp):
                    with mock.patch("timecapsulesmb.services.reachability.run_ssh") as run_ssh:
                        result = reachability.run_reachability(config, {"smb_hosts": ["tc.local"]}, password="pw")

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.summary, Summary("reachability.smb_only", "SMB port reachable, SSH closed."))
        self.assertEqual(result.smb_host, "tc.local")
        run_ssh.assert_not_called()

    def test_no_saved_hosts_skips_every_probe(self) -> None:
        config = AppConfig.from_values({})
        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips") as resolve:
            with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error") as tcp:
                with mock.patch("timecapsulesmb.services.reachability.run_ssh") as run_ssh:
                    result = reachability.run_reachability(config, {})

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.summary, Summary("reachability.no_candidates", "No saved host candidates were available."))
        self.assertEqual([(check.id, check.status) for check in result.checks], [("candidates", "SKIP")])
        self.assertIsNone(result.ssh_host)
        resolve.assert_not_called()
        tcp.assert_not_called()
        run_ssh.assert_not_called()

    def test_reachability_passes_when_ssh_and_smb_work(self) -> None:
        config = AppConfig.from_values({
            "TC_HOST": "root@tc.local",
            "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"],
        })

        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=("10.0.0.2",)):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.run_process",
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
        self.assertEqual(result.summary, Summary("reachability.all_reachable", "SSH reachable; SMB port reachable."))
        self.assertEqual({check.id: check.status for check in result.checks}, {
            "dns": "PASS",
            "ping": "PASS",
            "ssh_port": "PASS",
            "ssh_auth": "PASS",
            "smb_port": "PASS",
        })

    def test_missing_password_attempts_key_auth_and_checks_ports(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                    with self.ssh_auth_succeeds() as ssh:
                        result = reachability.run_reachability(config, {}, password="")

        ssh.assert_called_once()
        self.assertEqual(ssh.call_args.args[0].password, "")
        self.assertEqual(result.status, "reachable")
        self.assertEqual(result.summary, Summary("reachability.all_reachable", "SSH reachable; SMB port reachable."))
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "PASS")

    def test_reachability_strips_ports_from_host_candidates(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2:22", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        tcp_calls: list[tuple[str, int]] = []

        def tcp(host: str, port: int, *, timeout: float) -> str | None:
            tcp_calls.append((host, port))
            return None

        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=("10.0.0.2",)) as resolve:
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.run_process",
                    return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
                ):
                    with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", side_effect=tcp):
                        with self.ssh_auth_succeeds():
                            result = reachability.run_reachability(
                                config,
                                {"smb_hosts": ["capsule.local:445"]},
                                password="",
                            )

        self.assertEqual(result.status, "reachable")
        resolve.assert_called_once_with("capsule.local")
        self.assertIn(("10.0.0.2", 22), tcp_calls)
        self.assertIn(("capsule.local", 445), tcp_calls)

    def test_partial_when_ssh_port_works_but_smb_port_is_closed(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        def tcp(host: str, port: int, *, timeout: float) -> str | None:
            return None if port == 22 else "connection refused"

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", side_effect=tcp):
                    with self.ssh_auth_succeeds():
                        result = reachability.run_reachability(config, {}, password="")

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.summary, Summary("reachability.ssh_only", "SSH reachable, SMB port closed."))

    def test_ping_is_secondary_when_tcp_services_fail(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value="connection refused"):
                    result = reachability.run_reachability(config, {}, password="")

        self.assertEqual(result.status, "unreachable")
        self.assertEqual(result.summary, Summary("reachability.unreachable", "Could not reach SSH or SMB."))

    def test_all_failed_checks_return_unreachable_without_raising(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@tc.local", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.resolve_host_ips", return_value=()):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.run_process",
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
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ) as ping:
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None) as tcp:
                    with self.ssh_auth_succeeds():
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
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping6"], 0, stderr=b""),
            ) as ping:
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                    with self.ssh_auth_succeeds():
                        reachability.run_reachability(config, {}, password="")

        self.assertEqual(ping.call_args.args[0][0], "/sbin/ping6")
        self.assertIn("fd00::2", ping.call_args.args[0])

    def test_ssh_and_smb_ports_are_checked_on_the_saved_host(self) -> None:
        for saved, host in (("root@10.0.0.2", "10.0.0.2"), ("root@fe80::1%en0", "fe80::1%en0")):
            with self.subTest(saved=saved):
                config = AppConfig.from_values({"TC_HOST": saved, "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
                # The host's own ping binaries do not matter here.
                with mock.patch("timecapsulesmb.services.reachability.shutil.which", side_effect=lambda name: f"/sbin/{name}"):
                    with mock.patch(
                        "timecapsulesmb.services.reachability.run_process",
                        return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
                    ) as ping:
                        with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None) as tcp:
                            with self.ssh_auth_succeeds():
                                reachability.run_reachability(config, {}, password="")

                command = "/sbin/ping6" if ":" in host else "/sbin/ping"
                self.assertEqual(ping.call_args.args[0], [command, "-c", "1", host])
                self.assertIn(mock.call(host, 22, timeout=mock.ANY), tcp.call_args_list)
                self.assertIn(mock.call(host, 445, timeout=mock.ANY), tcp.call_args_list)

    def test_endpoint_host_normalizes_urls_ports_ipv6_and_trailing_dots(self) -> None:
        cases = {
            "smb://Capsule.local.:445/Data": "capsule.local",
            "root@Capsule.local.:22": "Capsule.local",
            "root@[fd00::2]:22": "fd00::2",
            "[fe80::1%bridge0]:445": "fe80::1%bridge0",
            "10.000.000.002:445": "10.0.0.2",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(endpoint_host(raw), expected)

    def test_ssh_target_from_params_canonicalizes_url_and_port_inputs(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@fallback.local", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        self.assertEqual(
            reachability.ssh_target_from_params(config, {"ssh_host": "ssh://admin@Capsule.local.:22"}),
            "admin@capsule.local",
        )
        self.assertEqual(
            reachability.ssh_target_from_params(config, {"host": "Capsule.local:2222"}),
            "root@Capsule.local",
        )
        self.assertEqual(
            reachability.ssh_target_from_params(config, {"host": "root@[fd00::2]:22"}),
            "root@fd00::2",
        )

    def test_smb_hosts_from_params_dedupes_case_and_ignores_empty_items(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@Capsule.local.", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        hosts = reachability.smb_hosts_from_params(
            config,
            {"smb_hosts": ["capsule.local", "", None], "smb_host": "smb://CAPSULE.local:445/Data"},
            ssh_host="Capsule.local",
        )

        self.assertEqual(hosts, ["capsule.local"])

    def test_negative_timeout_params_fall_back_to_defaults(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ) as ping:
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None) as tcp:
                    with self.ssh_auth_succeeds():
                        reachability.run_reachability(
                            config,
                            {"tcp_timeout": -1, "ssh_timeout": -1},
                            password="",
                        )

        self.assertEqual(ping.call_args.kwargs["timeout"], 3.0)
        self.assertEqual(tcp.call_args.kwargs["timeout"], 2.0)

    def test_ssh_auth_requires_ok_token_even_when_rc_is_zero(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        port_check = reachability.ReachabilityCheck("ssh_port", "PASS", "ok", host="10.0.0.2")

        with mock.patch(
            "timecapsulesmb.services.reachability.run_ssh",
            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="login banner only", stderr=""),
        ):
            result, installed = reachability.check_ssh_auth(
                "root@10.0.0.2",
                config,
                password="pw",
                port_check=port_check,
                timeout=8,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.detail, "login banner only")
        self.assertIsNone(installed)

    def test_ssh_auth_accepts_ok_token_after_login_noise(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        port_check = reachability.ReachabilityCheck("ssh_port", "PASS", "ok", host="10.0.0.2")

        with mock.patch(
            "timecapsulesmb.services.reachability.run_ssh",
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                0,
                stdout=f"Last login: today\n{reachability.REACHABILITY_OK_TOKEN}\n",
                stderr="",
            ),
        ):
            result, installed = reachability.check_ssh_auth(
                "root@10.0.0.2",
                config,
                password="pw",
                port_check=port_check,
                timeout=8,
            )

        self.assertEqual(result.status, "PASS")
        # A login whose output has no version lines read no version.
        self.assertEqual((installed.release_tag, installed.cli_version_code), (None, None))

    def test_ssh_auth_rejection_downgrades_reachability_to_partial(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
            with mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ):
                with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                    with mock.patch(
                        "timecapsulesmb.services.reachability.run_ssh",
                        side_effect=SshAuthenticationError("Permission denied"),
                    ):
                        result = reachability.run_reachability(config, {}, password="bad")

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.summary, Summary("reachability.auth_failed", "SSH authentication failed."))
        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "FAIL")

    def test_ssh_network_failure_makes_auth_check_unavailable(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        port_check = reachability.ReachabilityCheck("ssh_port", "PASS", "ok", host="10.0.0.2")

        with mock.patch(
            "timecapsulesmb.services.reachability.run_ssh",
            side_effect=SshNetworkError("Connection reset by peer"),
        ):
            result, installed = reachability.check_ssh_auth(
                "root@10.0.0.2",
                config,
                password="pw",
                port_check=port_check,
                timeout=8,
            )

        self.assertEqual(result.status, "SKIP")
        self.assertEqual(result.message, "SSH authentication could not be checked.")
        self.assertEqual(result.detail, "Connection reset by peer")
        self.assertIsNone(installed)

    def reachable_device(self, ssh):
        """Every probe but the login passes; `ssh` stands in for run_ssh."""
        return (
            mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"),
            mock.patch(
                "timecapsulesmb.services.reachability.run_process",
                return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
            ),
            mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None),
            mock.patch("timecapsulesmb.services.reachability.run_ssh", side_effect=ssh),
        )

    def run_reachable(self, ssh, config: AppConfig | None = None) -> reachability.ReachabilityResult:
        config = config or AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        which, ping, tcp, run_ssh = self.reachable_device(ssh)
        with which, ping, tcp, run_ssh:
            return reachability.run_reachability(config, {}, password="pw")

    def test_the_login_reads_the_installed_version_through_the_device_shell(self) -> None:
        def device_shell(_connection, command, **_kwargs):
            # The command the device's sh would run, here under /bin/sh.
            proc = subprocess.run(command, shell=True, capture_output=True, text=True)
            return subprocess.CompletedProcess(["ssh"], proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "tcapsulesmb.conf"
            config.write_text("TC_DEPLOY_RELEASE_TAG=v4.0.0\nTC_DEPLOY_CLI_VERSION_CODE=40000\n")
            with mock.patch.object(probe, "FLASH_RUNTIME_CONFIG", str(config)):
                with mock.patch.object(reachability, "CLI_VERSION_CODE", 50000), mock.patch.object(reachability, "RELEASE_TAG", "v5.0.0"):
                    result = self.run_reachable(device_shell)

        self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], "PASS")
        self.assertEqual(result.installed, reachability.InstalledVersion("v4.0.0", 40000, "outdated", "v5.0.0"))

    def test_the_installed_version_is_compared_with_this_release(self) -> None:
        with mock.patch.object(reachability, "CLI_VERSION_CODE", 50000), mock.patch.object(reachability, "RELEASE_TAG", "v5.0.0"):
            for code, state in ((40000, "outdated"), (50000, "current"), (60000, "newer"), (None, "unknown")):
                with self.subTest(code=code):
                    installed = reachability.installed_version(DeployedVersionProbeResult("v4.0.0", code, ""))
                    self.assertEqual((installed.version_code, installed.state, installed.app_release_tag), (code, state, "v5.0.0"))

    def test_the_payload_carries_the_installed_version(self) -> None:
        stdout = f"release_tag=v6.0.0\ncli_version_code=60000\n{reachability.REACHABILITY_OK_TOKEN}"
        with mock.patch.object(reachability, "CLI_VERSION_CODE", 50000), mock.patch.object(reachability, "RELEASE_TAG", "v5.0.0"):
            result = self.run_reachable(lambda *_a, **_k: subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr=""))

        payload = contracts.reachability_payload(result)
        self.assertEqual(
            {key: payload[key] for key in ("installed_release_tag", "installed_version_code", "install_state", "app_release_tag")},
            {"installed_release_tag": "v6.0.0", "installed_version_code": 60000, "install_state": "newer", "app_release_tag": "v5.0.0"},
        )

    def test_a_config_without_version_keys_is_unknown(self) -> None:
        stdout = f"release_tag=\ncli_version_code=\n{reachability.REACHABILITY_OK_TOKEN}"
        result = self.run_reachable(lambda *_a, **_k: subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr=""))

        self.assertEqual(result.installed.state, "unknown")
        self.assertEqual(contracts.reachability_payload(result)["install_state"], "unknown")

    def test_no_login_means_the_version_was_not_checked(self) -> None:
        def port(host: str, port: int, *_args, **_kwargs) -> str | None:
            return "connection refused" if port == 22 else None

        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        which, ping, _tcp, _ssh = self.reachable_device(None)
        with which, ping, mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", side_effect=port):
            with mock.patch("timecapsulesmb.services.reachability.run_ssh") as run_ssh:
                closed = reachability.run_reachability(config, {}, password="pw")
        run_ssh.assert_not_called()
        rejected = self.run_reachable(SshAuthenticationError("Permission denied"))

        for result, auth in ((closed, "SKIP"), (rejected, "FAIL")):
            with self.subTest(auth=auth):
                self.assertEqual({check.id: check.status for check in result.checks}["ssh_auth"], auth)
                self.assertIsNone(result.installed)
                payload = contracts.reachability_payload(result)
                self.assertEqual(payload["install_state"], "not_checked")
                self.assertEqual(
                    [payload[key] for key in ("installed_release_tag", "installed_version_code", "app_release_tag")],
                    [None, None, None],
                )

    def test_version_lines_without_the_token_last_still_fail_the_login(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})
        port_check = reachability.ReachabilityCheck("ssh_port", "PASS", "ok", host="10.0.0.2")
        stdout = f"{reachability.REACHABILITY_OK_TOKEN}\nrelease_tag=v4.0.0\ncli_version_code=40000\n"

        with mock.patch(
            "timecapsulesmb.services.reachability.run_ssh",
            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr=""),
        ):
            result, installed = reachability.check_ssh_auth(
                "root@10.0.0.2", config, password="pw", port_check=port_check, timeout=8,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertIsNone(installed)

    def test_app_operation_uses_effective_config_password(self) -> None:
        base = AppConfig.from_values(
            {
                "TC_HOST": "root@10.0.0.2",
                "TC_PASSWORD": "saved-password",
                "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"],
            },
            file_values={"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "saved-password"},
        )

        for params, expected in (
            ({}, "saved-password"),
            ({"credentials": {"password": "keychain-password"}}, "keychain-password"),
        ):
            with self.subTest(expected=expected):
                collector = CollectingSink()
                with mock.patch("timecapsulesmb.app.ops.common.load_optional_env_config", return_value=base):
                    with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                        with mock.patch(
                            "timecapsulesmb.services.reachability.run_process",
                            return_value=subprocess.CompletedProcess(["ping"], 0, stderr=b""),
                        ):
                            with mock.patch("timecapsulesmb.services.reachability.tcp_connect_error", return_value=None):
                                with self.ssh_auth_succeeds() as ssh:
                                    rc = service.run_api_request(
                                        {"operation": "reachability", "params": params},
                                        collector.sink,
                                    )

                self.assertEqual(rc, 0)
                self.assertEqual(ssh.call_args.args[0].password, expected)
                self.assertNotIn(expected, str(collector.events))

    def test_app_operation_emits_stages_checks_and_payload(self) -> None:
        collector = CollectingSink()
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_SSH_OPTS": DEFAULTS["TC_SSH_OPTS"]})

        with mock.patch("timecapsulesmb.app.ops.common.load_optional_env_config", return_value=config):
            with mock.patch("timecapsulesmb.services.reachability.shutil.which", return_value="/sbin/ping"):
                with mock.patch(
                    "timecapsulesmb.services.reachability.run_process",
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
