from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import (
    TERMINAL_INPUT_ATTEMPTS,
    json_text,
    print_json,
    prompt_device_password,
    read_terminal_line,
)
from timecapsulesmb.core.config import AppConfig, ConfigError, DEFAULTS
from timecapsulesmb.core.paths import AppPaths
from timecapsulesmb.services import runtime as service_runtime
from timecapsulesmb.services.runtime import resolve_env_connection
from timecapsulesmb.device.compat import classify_device_compatibility
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.device.probe import ProbeResult, ProbedDeviceState, SshAccessStatus
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import DeployDeviceError, require_supported_payload
from timecapsulesmb.transport.ssh import SshConnection

from tests.cli_support import app_config, valid_env


class RuntimeTests(unittest.TestCase):
    def test_resolve_env_connection_defaults_ssh_opts_when_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = resolve_env_connection(config)

        self.assertEqual(connection.host, "root@10.0.0.2")
        self.assertEqual(connection.password, "pw")
        self.assertEqual(connection.ssh_opts, DEFAULTS["TC_SSH_OPTS"])

    def test_resolve_env_connection_preserves_configured_ssh_opts(self) -> None:
        config = AppConfig.from_values({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ConnectTimeout=9",
        })
        connection = resolve_env_connection(config)

        self.assertEqual(connection.ssh_opts, "-o ConnectTimeout=9")

    def test_resolve_env_connection_uses_password_provider_when_password_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})
        provider = mock.Mock(return_value="prompted-pw")

        connection = resolve_env_connection(config, password_provider=provider)

        provider.assert_called_once_with("Device root password: ")
        self.assertEqual(connection.password, "prompted-pw")

    def test_resolve_env_connection_uses_the_saved_host_as_is(self) -> None:
        for host in ("root@10.0.0.2", "root@fe80::82ea:96ff:fee6:5868%en0"):
            with self.subTest(host=host):
                config = AppConfig.from_values({"TC_HOST": host, "TC_PASSWORD": "pw"})
                self.assertEqual(resolve_env_connection(config).host, host)

    def test_managed_target_accepts_a_scoped_link_local_host(self) -> None:
        config = app_config(valid_env(TC_HOST="root@fe80::1%en0"))
        target = service_runtime.resolve_validated_managed_target(
            config,
            command_name="deploy",
            profile="deploy",
            include_probe=False,
        )

        self.assertEqual(target.connection.host, "root@fe80::1%en0")

    def test_managed_target_still_rejects_unscoped_and_169_254_hosts(self) -> None:
        cases = {
            "root@fe80::1": "is a link-local IPv6 address without its interface",
            "root@169.254.44.9": "must not be a link-local address",
        }
        for host, message in cases.items():
            with self.subTest(host=host):
                with self.assertRaises(ConfigError) as ctx:
                    service_runtime.resolve_validated_managed_target(
                        app_config(valid_env(TC_HOST=host)),
                        command_name="deploy",
                        profile="deploy",
                        include_probe=False,
                    )
                self.assertIn(message, str(ctx.exception))

    def test_resolve_env_connection_does_not_prompt_without_provider(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})

        with self.assertRaises(ConfigError) as ctx:
            resolve_env_connection(config)

        self.assertIn("TC_PASSWORD is required", str(ctx.exception))

    def test_json_text_uses_stable_pretty_format(self) -> None:
        self.assertEqual(json_text({"b": 1, "a": {"c": 2}}), '{\n  "a": {\n    "c": 2\n  },\n  "b": 1\n}')

    def test_json_text_preserves_sensitive_values_for_file_serialization(self) -> None:
        self.assertEqual(json_text({"TC_PASSWORD": "secret"}), '{\n  "TC_PASSWORD": "secret"\n}')

    def test_print_json_redacts_nested_sensitive_values(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            print_json({
                "host": "root@10.0.0.2",
                "TC_PASSWORD": "secret",
                "nested": {
                    "credentials": {"password": "nested-secret"},
                    "key_id": "observed-k30a-78100",
                    "tokens": ["token-secret"],
                    "ok": True,
                },
                "items": [
                    {"session_secret": "session-secret-value"},
                    {"name": "public"},
                ],
                "localization_key": "configure.auth_failed",
            })

        self.assertEqual(
            output.getvalue(),
            (
                '{\n'
                '  "TC_PASSWORD": "<redacted>",\n'
                '  "host": "root@10.0.0.2",\n'
                '  "items": [\n'
                '    {\n'
                '      "session_secret": "<redacted>"\n'
                '    },\n'
                '    {\n'
                '      "name": "public"\n'
                '    }\n'
                '  ],\n'
                '  "localization_key": "configure.auth_failed",\n'
                '  "nested": {\n'
                '    "credentials": "<redacted>",\n'
                '    "key_id": "observed-k30a-78100",\n'
                '    "ok": true,\n'
                '    "tokens": "<redacted>"\n'
                '  }\n'
                '}\n'
            ),
        )
        self.assertNotIn("nested-secret", output.getvalue())
        self.assertNotIn("token-secret", output.getvalue())
        self.assertNotIn("session-secret-value", output.getvalue())

    def test_optional_env_config_uses_missing_config_when_env_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            app_paths = AppPaths(
                distribution_root=Path(tmp),
                config_path=env_path,
                state_dir=Path(tmp),
                package_root=SRC_ROOT / "timecapsulesmb",
            )
            with mock.patch("timecapsulesmb.services.runtime.resolve_app_paths", return_value=app_paths):
                config = service_runtime.load_optional_env_config()

        self.assertFalse(config.exists)
        self.assertEqual(config.path, env_path)
        self.assertEqual(config.values, {})

    def test_optional_env_config_reads_env_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("TC_HOST='root@10.0.0.2'\nTC_CONFIGURE_ID='cfg-1'\n")
            app_paths = AppPaths(
                distribution_root=Path(tmp),
                config_path=env_path,
                state_dir=Path(tmp),
                package_root=SRC_ROOT / "timecapsulesmb",
            )
            with mock.patch("timecapsulesmb.services.runtime.resolve_app_paths", return_value=app_paths):
                config = service_runtime.load_optional_env_config()

        self.assertTrue(config.exists)
        self.assertEqual(config.path, env_path)
        self.assertEqual(config.get("TC_HOST"), "root@10.0.0.2")
        self.assertEqual(config.get("TC_CONFIGURE_ID"), "cfg-1")

    def test_managed_target_resolves_connection_without_probing(self) -> None:
        config = app_config(valid_env())
        with mock.patch("timecapsulesmb.services.runtime.probe_connection_state", side_effect=AssertionError("should not probe")):
            target = service_runtime.resolve_validated_managed_target(
                config,
                command_name="deploy",
                profile="deploy",
                include_probe=False,
            )

        self.assertEqual(target.connection.host, config.require("TC_HOST"))
        self.assertIsNone(target.probe_state)

    def test_managed_target_keeps_a_hostname_without_resolving_it(self) -> None:
        config = app_config(valid_env(TC_HOST="root@capsule.local"))
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo") as getaddrinfo:
            target = service_runtime.resolve_validated_managed_target(
                config,
                command_name="deploy",
                profile="deploy",
                include_probe=False,
            )

        self.assertEqual(target.connection.host, "root@capsule.local")
        getaddrinfo.assert_not_called()

    def test_managed_target_rejects_proxy_ssh_opts_before_any_lookup(self) -> None:
        config = app_config(
            valid_env(
                TC_HOST="root@capsule.local",
                TC_SSH_OPTS="-o ProxyJump=bastion",
            )
        )
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("should not resolve")):
            with self.assertRaises(ConfigError) as ctx:
                service_runtime.resolve_validated_managed_target(
                    config,
                    command_name="deploy",
                    profile="deploy",
                    include_probe=False,
                )

        self.assertIn("TC_SSH_OPTS must not use ProxyJump", str(ctx.exception))

    def _bad_decode(self) -> UnicodeDecodeError:
        # What a UTF-8 stdin raises for a KOI8/CP1251 byte (telemetry, v3.1.1).
        return UnicodeDecodeError("utf-8", b"\xd0a", 0, 1, "invalid continuation byte")

    def test_read_terminal_line_asks_again_after_undecodable_input(self) -> None:
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=[self._bad_decode(), "root@10.0.1.20"]) as input_mock:
            with redirect_stdout(output):
                value = read_terminal_line("SSH target: ")

        self.assertEqual(value, "root@10.0.1.20")
        self.assertEqual(input_mock.call_count, 2)
        self.assertEqual(output.getvalue().count("could not be read as"), 1)
        self.assertIn("set the terminal to UTF-8", output.getvalue())

    def test_read_terminal_line_returns_first_good_answer_without_a_warning(self) -> None:
        output = io.StringIO()
        with mock.patch("getpass.getpass", return_value="pässword") as getpass_mock:
            with redirect_stdout(output):
                value = read_terminal_line("Password: ", secret=True)

        self.assertEqual(value, "pässword")
        getpass_mock.assert_called_once_with("Password: ")
        self.assertEqual(output.getvalue(), "")

    def test_device_password_prompt_gives_up_with_a_config_error_not_a_traceback(self) -> None:
        # CommandContext turns ConfigError into a clean exit and telemetry error.
        output = io.StringIO()
        with mock.patch("getpass.getpass", side_effect=self._bad_decode()) as getpass_mock:
            with redirect_stdout(output):
                with self.assertRaises(ConfigError) as ctx:
                    prompt_device_password("AirPort admin password: ")

        self.assertEqual(getpass_mock.call_count, TERMINAL_INPUT_ATTEMPTS)
        self.assertIn("could not be read as", str(ctx.exception))
        self.assertNotIn("UnicodeDecodeError", str(ctx.exception))

    def test_resolve_env_connection_no_input_fails_instead_of_prompting_for_password(self) -> None:
        config = app_config({"TC_HOST": "root@10.0.0.2"})
        with mock.patch("getpass.getpass", side_effect=AssertionError("non-interactive callers must not prompt")):
            with self.assertRaises(ConfigError) as ctx:
                service_runtime.resolve_env_connection(config, allow_password_prompt=False)

        self.assertIn("TC_PASSWORD is required when --no-input is used.", str(ctx.exception))

    def test_flash_target_resolution_uses_connection_only_config(self) -> None:
        config = app_config({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "not a valid interface value",
            "TC_AIRPORT_SYAP": "not-a-syap",
            "TC_MDNS_DEVICE_MODEL": "not-a-model",
        })
        with mock.patch("timecapsulesmb.services.runtime.probe_connection_state", side_effect=AssertionError("flash target resolution should not probe the device")):
            target = service_runtime.resolve_validated_managed_target(
                config,
                command_name="flash",
                profile="flash",
                include_probe=True,
            )

        self.assertEqual(target.connection.host, "root@10.0.0.2")
        self.assertEqual(target.connection.password, "pw")
        self.assertEqual(target.connection.ssh_opts, "-o foo")
        self.assertIsNone(target.probe_state)


if __name__ == "__main__":
    unittest.main()


class ProbeFailureErrorTests(unittest.TestCase):
    @staticmethod
    def failed_probe(status: SshAccessStatus, error: str = "SSH is not reachable yet.") -> ProbeResult:
        return ProbeResult(ssh_status=status, error=error, os_name="", os_release="", arch="", elf_endianness="unknown")

    def test_closed_ssh_with_acp_answering_means_ssh_is_turned_off(self) -> None:
        acp_probe = mock.Mock(return_value=None)

        error = service_runtime.probe_failure_error(
            self.failed_probe(SshAccessStatus.CLOSED), "root@10.0.0.2", tcp_connect_error_func=acp_probe,
        )

        acp_probe.assert_called_once_with("10.0.0.2", 5009, service_runtime.ACP_PORT_CHECK_TIMEOUT_SECONDS)
        self.assertIsInstance(error, DeviceError)
        self.assertEqual(error.code, "ssh_disabled")
        self.assertIn("SSH is turned off on 10.0.0.2", str(error))

    def test_closed_ssh_with_acp_silent_means_the_device_is_unreachable(self) -> None:
        error = service_runtime.probe_failure_error(
            self.failed_probe(SshAccessStatus.CLOSED),
            "root@10.0.0.2",
            tcp_connect_error_func=mock.Mock(return_value="[Errno 64] Host is down"),
        )

        self.assertEqual(error.code, "device_unreachable")
        self.assertIn("not answering at 10.0.0.2", str(error))
        self.assertIn("[Errno 64] Host is down", str(error))

    def test_acp_check_targets_ipv6_and_hostname_devices_too(self) -> None:
        for host, expected in (
            ("root@fd00::2", "fd00::2"),
            ("root@[2001:db8::2]", "2001:db8::2"),
            ("root@Office-TC.local", "Office-TC.local"),
        ):
            with self.subTest(host=host):
                acp_probe = mock.Mock(return_value=None)
                service_runtime.probe_failure_error(
                    self.failed_probe(SshAccessStatus.CLOSED), host, tcp_connect_error_func=acp_probe,
                )
                self.assertEqual(acp_probe.call_args.args[0], expected)

    def test_login_failures_keep_the_probe_message_and_get_their_own_code(self) -> None:
        cases = (
            (SshAccessStatus.AUTH_REJECTED, "auth_failed"),
            (SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED, "ssh_compatibility_failed"),
            (SshAccessStatus.TRANSPORT_FAILED, "ssh_transport_failed"),
            (SshAccessStatus.LOCAL_NETWORK_FILTERED, "local_network_filtered"),
            (SshAccessStatus.DEVICE_PROBE_FAILED, "device_probe_failed"),
        )
        for status, code in cases:
            with self.subTest(status=status):
                acp_probe = mock.Mock(side_effect=AssertionError("only a closed port checks ACP"))
                error = service_runtime.probe_failure_error(
                    self.failed_probe(status, "the probe's own text"), "root@10.0.0.2", tcp_connect_error_func=acp_probe,
                )
                self.assertEqual((error.code, str(error)), (code, "the probe's own text"))

    def test_no_probe_failure_is_reported_as_an_unsupported_model(self) -> None:
        codes = {
            service_runtime.probe_failure_error(
                self.failed_probe(status), "root@10.0.0.2", tcp_connect_error_func=mock.Mock(return_value=acp_error),
            ).code
            for status in SshAccessStatus
            if status != SshAccessStatus.OPEN_AUTHENTICATED
            for acp_error in (None, "timed out")
        }
        self.assertNotIn("unsupported_device", codes)
        self.assertEqual(
            codes,
            {
                "ssh_disabled",
                "device_unreachable",
                "auth_failed",
                "ssh_compatibility_failed",
                "ssh_transport_failed",
                "local_network_filtered",
                "device_probe_failed",
            },
        )

    def test_connection_compatibility_raises_the_coded_error_when_ssh_did_not_log_in(self) -> None:
        state = ProbedDeviceState(probe_result=self.failed_probe(SshAccessStatus.CLOSED), compatibility=None)
        # The closed-port recheck has its own tests; this is the state it ends in.
        with mock.patch.object(service_runtime, "probe_managed_connection_state", return_value=state):
            with mock.patch.object(service_runtime, "tcp_connect_error", return_value=None):
                with self.assertRaises(service_runtime.DeviceAccessError) as raised:
                    service_runtime.require_connection_compatibility(SshConnection("root@10.0.0.2", "pw", ""))

        self.assertEqual(raised.exception.code, "ssh_disabled")

    def test_connection_compatibility_returns_whatever_a_logged_in_probe_found(self) -> None:
        compatibility = classify_device_compatibility("NetBSD", "4.0_STABLE", "ar7240", "big")
        state = ProbedDeviceState(
            probe_result=ProbeResult(SshAccessStatus.OPEN_AUTHENTICATED, None, "NetBSD", "4.0_STABLE", "ar7240", "big"),
            compatibility=compatibility,
        )
        with mock.patch.object(service_runtime, "probe_connection_state", return_value=state):
            # Callers decide what an unsupported model means for their operation.
            self.assertIs(service_runtime.require_connection_compatibility(SshConnection("root@10.0.0.2", "pw", "")), compatibility)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ProbeManagedConnectionStateTests(unittest.TestCase):
    connection = SshConnection("root@10.0.0.2", "pw", "")

    @staticmethod
    def state(status: SshAccessStatus, error: str | None = None) -> ProbedDeviceState:
        if status == SshAccessStatus.OPEN_AUTHENTICATED:
            result = ProbeResult(status, None, "NetBSD", "6.0", "earmv4", "little")
            return ProbedDeviceState(result, classify_device_compatibility("NetBSD", "6.0", "earmv4", "little"))
        return ProbedDeviceState(ProbeResult(status, error, "", "", "", "unknown"), compatibility=None)

    def run_probe(self, probe: mock.Mock, tcp_open: mock.Mock, clock: FakeClock, connection: SshConnection | None = None):
        return service_runtime.probe_managed_connection_state(
            connection or self.connection,
            probe=probe,
            tcp_open_func=tcp_open,
            sleep_func=clock.sleep,
            monotonic_func=clock.monotonic,
        )

    def test_a_probe_that_reached_ssh_is_not_rechecked(self) -> None:
        for status in (SshAccessStatus.OPEN_AUTHENTICATED, SshAccessStatus.AUTH_REJECTED, SshAccessStatus.TRANSPORT_FAILED):
            with self.subTest(status=status):
                first = self.state(status, "error")
                probe = mock.Mock(return_value=first)
                tcp_open = mock.Mock()
                clock = FakeClock()

                state = self.run_probe(probe, tcp_open, clock)

                self.assertIs(state, first)
                probe.assert_called_once_with(self.connection)
                tcp_open.assert_not_called()
                self.assertEqual(clock.sleeps, [])

    def test_a_port_that_opens_on_a_recheck_is_probed_again(self) -> None:
        logged_in = self.state(SshAccessStatus.OPEN_AUTHENTICATED)
        probe = mock.Mock(side_effect=[self.state(SshAccessStatus.CLOSED, "SSH is not reachable yet."), logged_in])
        tcp_open = mock.Mock(side_effect=[False, True])
        clock = FakeClock()

        state = self.run_probe(probe, tcp_open, clock)

        self.assertIs(state, logged_in)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(tcp_open.call_args_list, [mock.call("10.0.0.2", 22)] * 2)
        self.assertEqual(clock.sleeps, [service_runtime.CLOSED_SSH_RECHECK_INTERVAL_SECONDS] * 2)

    def test_a_port_that_stays_closed_keeps_the_first_answer_after_the_window(self) -> None:
        closed = self.state(SshAccessStatus.CLOSED, "SSH is not reachable yet.")
        probe = mock.Mock(return_value=closed)
        tcp_open = mock.Mock(return_value=False)
        clock = FakeClock()

        state = self.run_probe(probe, tcp_open, clock)

        self.assertIs(state, closed)
        probe.assert_called_once()
        self.assertEqual(sum(clock.sleeps), service_runtime.CLOSED_SSH_RECHECK_WINDOW_SECONDS)
        expected_checks = service_runtime.CLOSED_SSH_RECHECK_WINDOW_SECONDS / service_runtime.CLOSED_SSH_RECHECK_INTERVAL_SECONDS
        self.assertEqual(tcp_open.call_count, expected_checks)

    def test_slow_connect_timeouts_do_not_stretch_the_window(self) -> None:
        clock = FakeClock()
        start = clock.now

        def timing_out(_host: str, _port: int) -> bool:
            clock.now += 2.0
            return False

        tcp_open = mock.Mock(side_effect=timing_out)
        state = self.run_probe(mock.Mock(return_value=self.state(SshAccessStatus.CLOSED)), tcp_open, clock)

        self.assertEqual(state.probe_result.ssh_status, SshAccessStatus.CLOSED)
        # The last check may start just before the deadline and take one timeout.
        self.assertLessEqual(clock.now - start, service_runtime.CLOSED_SSH_RECHECK_WINDOW_SECONDS + 2.0)
        self.assertEqual(tcp_open.call_count, 2)

    def test_recheck_targets_the_ipv6_endpoint(self) -> None:
        tcp_open = mock.Mock(return_value=True)
        probe = mock.Mock(side_effect=[self.state(SshAccessStatus.CLOSED), self.state(SshAccessStatus.OPEN_AUTHENTICATED)])

        self.run_probe(probe, tcp_open, FakeClock(), SshConnection("root@[2001:db8::2]", "pw", ""))

        tcp_open.assert_called_once_with("2001:db8::2", 22)

    def test_deploy_target_and_compatibility_checks_recheck_a_closed_port(self) -> None:
        config = app_config(valid_env())
        for name in ("resolve_validated_managed_target", "require_connection_compatibility"):
            with self.subTest(caller=name):
                probe = mock.Mock(side_effect=[
                    self.state(SshAccessStatus.CLOSED, "SSH is not reachable yet."),
                    self.state(SshAccessStatus.OPEN_AUTHENTICATED),
                ])
                with mock.patch("timecapsulesmb.services.runtime.probe_connection_state", probe), \
                        mock.patch("timecapsulesmb.services.runtime.tcp_open", return_value=True), \
                        mock.patch("timecapsulesmb.services.runtime.CLOSED_SSH_RECHECK_INTERVAL_SECONDS", 0.0):
                    if name == "resolve_validated_managed_target":
                        target = service_runtime.resolve_validated_managed_target(
                            config, command_name="deploy", profile="deploy", include_probe=True,
                        )
                        compatibility = target.probe_state.compatibility
                    else:
                        compatibility = service_runtime.require_connection_compatibility(self.connection)

                self.assertEqual(probe.call_count, 2)
                self.assertTrue(compatibility.supported)


class RequireSupportedPayloadTests(unittest.TestCase):
    @staticmethod
    def target(probe_state: ProbedDeviceState | None) -> service_runtime.ManagedTargetState:
        return service_runtime.ManagedTargetState(connection=SshConnection("root@10.0.0.2", "pw", ""), probe_state=probe_state)

    @staticmethod
    def logged_in(os_release: str, arch: str, endianness: str) -> ProbedDeviceState:
        result = ProbeResult(SshAccessStatus.OPEN_AUTHENTICATED, None, "NetBSD", os_release, arch, endianness)
        return ProbedDeviceState(probe_result=result, compatibility=classify_device_compatibility("NetBSD", os_release, arch, endianness))

    def test_ssh_failures_raise_their_access_code(self) -> None:
        state = ProbedDeviceState(
            probe_result=ProbeResult(SshAccessStatus.AUTH_REJECTED, "denied", "", "", "", "unknown"), compatibility=None,
        )

        with self.assertRaises(service_runtime.DeviceAccessError) as raised:
            require_supported_payload(self.target(state), allow_unsupported=False)

        self.assertEqual(raised.exception.code, "auth_failed")

    def test_an_airport_express_is_an_unsupported_device(self) -> None:
        with self.assertRaises(DeployDeviceError) as raised:
            require_supported_payload(self.target(self.logged_in("4.0_STABLE", "ar7240", "big")), allow_unsupported=False)

        self.assertEqual(raised.exception.code, "unsupported_device")
        self.assertIn("AirPort Express", str(raised.exception))

    def test_allow_unsupported_still_needs_a_payload_for_the_device(self) -> None:
        with self.assertRaises(DeployDeviceError) as raised:
            require_supported_payload(self.target(self.logged_in("4.0_STABLE", "ar7240", "big")), allow_unsupported=True)

        self.assertEqual(raised.exception.code, "unsupported_device")
        self.assertIn("No deployable payload", str(raised.exception))

    def test_a_supported_device_returns_its_compatibility(self) -> None:
        state = self.logged_in("6.0", "earmv4", "little")

        self.assertIs(require_supported_payload(self.target(state), allow_unsupported=False), state.compatibility)

    def test_a_missing_probe_is_an_uncoded_device_error(self) -> None:
        with self.assertRaises(DeviceError) as raised:
            require_supported_payload(self.target(None), allow_unsupported=False)

        self.assertFalse(hasattr(raised.exception, "code"))


class RequireDevicePasswordTests(unittest.TestCase):
    def check(self, returncode: int) -> tuple[list[dict[str, object]], Exception | None]:
        debug: list[dict[str, object]] = []
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.append(fields))
        answer = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], returncode, b"", b""))
        with mock.patch("timecapsulesmb.device.probe.run_ssh_input", answer):
            try:
                service_runtime.require_device_password(SshConnection("root@10.0.0.2", "pw", ""), callbacks)
            except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
                return debug, exc
        return debug, None

    def test_a_mismatch_is_refused_as_auth_failed(self) -> None:
        debug, error = self.check(1)
        self.assertIsInstance(error, service_runtime.DeviceAccessError)
        self.assertEqual(error.code, "auth_failed")
        self.assertEqual(str(error), service_runtime.AIRPORT_PASSWORD_MISMATCH_MESSAGE)
        self.assertEqual(debug, [{"sypw_check": "mismatch"}])

    def test_a_match_or_an_unknown_answer_lets_the_command_go_on(self) -> None:
        for returncode, result in ((0, "match"), (2, "unknown")):
            with self.subTest(result=result):
                debug, error = self.check(returncode)
                self.assertIsNone(error)
                self.assertEqual(debug, [{"sypw_check": result}])
