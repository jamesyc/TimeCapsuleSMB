from __future__ import annotations

import io
import ipaddress
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

from timecapsulesmb.app.recovery import cli_steps
from timecapsulesmb.cli.runtime import (
    DEVICE_PASSWORD_NONINTERACTIVE_MESSAGE,
    TERMINAL_INPUT_ATTEMPTS,
    NonInteractivePromptError,
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
from timecapsulesmb.device.probe import AirportAcpReading, ProbeResult, ProbedDeviceState, SshAccessStatus
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import DeployDeviceError, require_supported_payload
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError
from timecapsulesmb.transport.ssh import SshConnection

from tests.cli_support import app_config, valid_env
from timecapsulesmb.checks.network import PORT_CHECK_TIMEOUT_SECONDS
from timecapsulesmb.services.credentials import overlay_request_airport_mac, saved_airport_mac
from timecapsulesmb.services.locate import LocateResult
from tests.reboot_support import DEVICE_AIRPORT_MAC, RecordingCallbacks, acp_password_answer, acp_reading


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

    def test_device_password_prompt_without_input_says_how_to_save_the_password(self) -> None:
        with mock.patch("getpass.getpass", side_effect=EOFError("EOF when reading a line")):
            with self.assertRaises(NonInteractivePromptError) as ctx:
                prompt_device_password("Device root password: ")

        self.assertEqual(str(ctx.exception), DEVICE_PASSWORD_NONINTERACTIVE_MESSAGE)
        self.assertIsInstance(ctx.exception.__cause__, EOFError)

    def test_end_of_input_after_undecodable_input_stops_instead_of_retrying(self) -> None:
        output = io.StringIO()
        with mock.patch("getpass.getpass", side_effect=[self._bad_decode(), EOFError()]) as getpass_mock:
            with redirect_stdout(output):
                with self.assertRaises(NonInteractivePromptError):
                    prompt_device_password("Device root password: ")

        self.assertEqual(getpass_mock.call_count, 2)
        self.assertEqual(output.getvalue().count("could not be read as"), 1)

    def test_resolve_env_connection_without_a_password_provider_fails_instead_of_prompting(self) -> None:
        config = app_config({"TC_HOST": "root@10.0.0.2"})
        with mock.patch("getpass.getpass", side_effect=AssertionError("non-interactive callers must not prompt")):
            with self.assertRaises(ConfigError) as ctx:
                service_runtime.resolve_env_connection(config)

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

    @staticmethod
    def probe_failure_with(args, connect):
        with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", connect):
            return service_runtime.probe_failure_error(*args)

    def test_closed_ssh_with_acp_answering_means_ssh_is_turned_off(self) -> None:
        acp_probe = mock.Mock(return_value=None)

        error = self.probe_failure_with((self.failed_probe(SshAccessStatus.CLOSED), "root@10.0.0.2"), acp_probe)

        acp_probe.assert_called_once_with("10.0.0.2", 5009, PORT_CHECK_TIMEOUT_SECONDS)
        self.assertIsInstance(error, DeviceError)
        self.assertEqual(error.code, "ssh_disabled")
        self.assertIn("SSH is turned off on 10.0.0.2", str(error))

    def test_closed_ssh_with_acp_silent_means_the_device_is_unreachable(self) -> None:
        error = self.probe_failure_with((self.failed_probe(SshAccessStatus.CLOSED),
            "root@10.0.0.2"), mock.Mock(return_value="[Errno 64] Host is down"))

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
                self.probe_failure_with((self.failed_probe(SshAccessStatus.CLOSED), host), acp_probe)
                self.assertEqual(acp_probe.call_args.args[0], expected)

    def test_login_failures_keep_the_probe_message_and_get_their_own_code(self) -> None:
        cases = (
            (SshAccessStatus.AUTH_REJECTED, "auth_failed"),
            (SshAccessStatus.ALGORITHM_NEGOTIATION_FAILED, "ssh_compatibility_failed"),
            (SshAccessStatus.TRANSPORT_FAILED, "ssh_transport_failed"),
            (SshAccessStatus.CLIENT_FAILED, "ssh_client_failed"),
            (SshAccessStatus.LOCAL_NETWORK_FILTERED, "local_network_filtered"),
            (SshAccessStatus.DEVICE_PROBE_FAILED, "device_probe_failed"),
        )
        for status, code in cases:
            with self.subTest(status=status):
                acp_probe = mock.Mock(side_effect=AssertionError("only a closed port checks ACP"))
                error = self.probe_failure_with((self.failed_probe(status, "the probe's own text"), "root@10.0.0.2"), acp_probe)
                self.assertEqual((error.code, str(error)), (code, "the probe's own text"))

    def test_no_probe_failure_is_reported_as_an_unsupported_model(self) -> None:
        codes = {
            self.probe_failure_with((self.failed_probe(status), "root@10.0.0.2"), mock.Mock(return_value=acp_error)).code
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
                "ssh_client_failed",
                "local_network_filtered",
                "device_probe_failed",
            },
        )

    def test_connection_compatibility_raises_the_coded_error_when_ssh_did_not_log_in(self) -> None:
        state = ProbedDeviceState(probe_result=self.failed_probe(SshAccessStatus.CLOSED), compatibility=None)
        # The closed-port recheck has its own tests; this is the state it ends in.
        with mock.patch.object(service_runtime, "probe_managed_connection_state", return_value=state):
            with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=None):
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

        self.assertIsNone(raised.exception.code)


class RequireAcpdReadyPasswordTests(unittest.TestCase):
    def check(self, matches: bool | None) -> tuple[list[dict[str, object]], Exception | None]:
        debug: list[dict[str, object]] = []
        callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.append(fields))
        answer = acp_password_answer(matches)
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", answer):
            try:
                service_runtime.require_acpd_ready(SshConnection("root@10.0.0.2", "pw", ""), callbacks)
            except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
                return debug, exc
        return debug, None

    def test_a_mismatch_is_refused_as_auth_failed(self) -> None:
        debug, error = self.check(False)
        self.assertIsInstance(error, service_runtime.DeviceAccessError)
        self.assertEqual(error.code, "auth_failed")
        self.assertEqual(str(error), service_runtime.AIRPORT_ADMIN_PASSWORD_REJECTED_MESSAGE)
        self.assertEqual(debug, [{"sypw_check": "mismatch"}])

    def test_a_match_or_an_unknown_answer_lets_the_command_go_on(self) -> None:
        for matches, result in ((True, "match"), (None, "unknown")):
            with self.subTest(result=result):
                debug, error = self.check(matches)
                self.assertIsNone(error)
                self.assertEqual(debug, [{"sypw_check": result}])


class RequireAcpdReadyReadingTests(unittest.TestCase):
    """A command that already read the device does not read it again."""

    def test_an_earlier_read_is_used_instead_of_a_new_one(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        for reading, refused in ((acp_reading(True), False), (acp_reading(False), True), (acp_reading(None), False)):
            with self.subTest(matches=reading.password_matches):
                read = acp_password_answer(True)
                debug: list[dict[str, object]] = []
                callbacks = OperationCallbacks(add_debug_fields=lambda **fields: debug.append(fields))
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", read):
                    try:
                        service_runtime.require_acpd_ready(connection, callbacks, reading=reading)
                        outcome = None
                    except service_runtime.DeviceAccessError as exc:
                        outcome = exc
                read.assert_not_called()
                self.assertEqual(outcome is not None, refused)
                self.assertEqual(debug, [{"sypw_check": {True: "match", False: "mismatch", None: "unknown"}[reading.password_matches]}])


class RequireAcpdReadyAcpdTests(unittest.TestCase):
    """Apple's ACPd must answer the device's own acp, and this computer, have
    run the last restart requested, and carry no stuck fork, before a reboot
    or flash changes anything."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
    STUCK_LISTING = "\n".join((
        "  119 1 2 Sl 0 kqueue 0:00.00 ACPd",
        "  352 119 2 S 1 nanoslp 0:00.00 ACPd",
    ))

    def gate(
        self,
        *,
        answers: bool = True,
        listing: str = "",
        never_happened: bool = False,
        reading=None,
        off_network=None,
        network_answers: bool = True,
    ):
        debug: dict[str, object] = {}
        callbacks = OperationCallbacks(add_debug_fields=debug.update)
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as answer, \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=listing) as processes, \
                mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=never_happened), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=network_answers) as network, \
                mock.patch("timecapsulesmb.services.runtime.client_networks_if_off_network", return_value=off_network):
            try:
                service_runtime.require_acpd_ready(self.CONNECTION, callbacks, reading=reading or acp_reading(True))
                error = None
            except service_runtime.DeviceAccessError as exc:
                error = exc
        self.network = network
        return error, debug, answer, processes

    def test_a_healthy_acpd_lets_the_command_go_on(self) -> None:
        error, debug, answer, processes = self.gate()
        self.assertIsNone(error)
        answer.assert_called_once_with(self.CONNECTION)
        processes.assert_called_once_with(self.CONNECTION)
        self.assertNotIn("acpd_problems", debug)

    def test_an_acpd_that_does_not_answer_syut_is_broken(self) -> None:
        error, debug, _answer, _processes = self.gate(answers=False)
        self.assertEqual(error.code, "acpd_broken")
        # Plain words for the user; the debug field names the problem.
        self.assertTrue(str(error).startswith("Apple ACPd is broken: the AirPort service on this device is stuck"))
        self.assertNotIn("syUT", str(error))
        self.assertIn("Nothing was changed", str(error))
        self.assertEqual(debug["acpd_problems"], ["syut_silent"])

    def test_a_stuck_fork_of_acpd_is_broken(self) -> None:
        error, debug, _answer, _processes = self.gate(listing=self.STUCK_LISTING)
        self.assertEqual(error.code, "acpd_broken")
        self.assertIn("pid 352", str(error))
        self.assertEqual(debug["acpd_problems"], ["stuck_child"])
        self.assertEqual(debug["acpd_stuck_children"], [352])

    def test_a_restart_that_never_happened_is_refused_until_the_device_is_unplugged(self) -> None:
        # Another restart request would fail the same way.
        error, debug, _answer, _processes = self.gate(never_happened=True)
        self.assertEqual(error.code, "acpd_broken")
        self.assertIn("did not restart", str(error))
        self.assertIn("Nothing was changed", str(error))
        # acpd_broken's next steps say to unplug it.
        self.assertIn("Unplug the device, wait 10 seconds and plug it back in.", cli_steps("deploy", error.code))
        self.assertEqual(debug["acpd_problems"], ["restart_never_happened"])

    def test_the_first_problem_is_named_and_every_one_is_reported(self) -> None:
        error, debug, _answer, _processes = self.gate(answers=False, listing=self.STUCK_LISTING)
        self.assertIn("the AirPort service on this device is stuck", str(error))
        self.assertEqual(debug["acpd_problems"], ["syut_silent", "stuck_child"])
        self.assertEqual(debug["acpd_stuck_children"], [352])

    def test_a_restart_that_never_happened_is_the_only_problem_named(self) -> None:
        error, debug, answer, processes = self.gate(never_happened=True, answers=False, listing=self.STUCK_LISTING)
        self.assertIn("did not restart", str(error))
        self.assertEqual(debug["acpd_problems"], ["restart_never_happened"])
        answer.assert_not_called()
        processes.assert_not_called()

    def test_a_rejected_password_is_refused_before_ssh_is_asked(self) -> None:
        error, _debug, answer, processes = self.gate(reading=acp_reading(False))
        self.assertEqual(error.code, "auth_failed")
        answer.assert_not_called()
        processes.assert_not_called()

    def test_an_unanswered_network_read_blames_acpd_only_when_its_own_acp_fails(self) -> None:
        unanswered = AirportAcpReading(password_matches=None, error="ACP receive failed: timed out")
        error, debug, _answer, _processes = self.gate(reading=unanswered, answers=False)
        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(debug["sypw_check"], "unknown")
        self.assertEqual(debug["acp_read_error"], "ACP receive failed: timed out")

    def test_an_unanswered_network_read_with_a_working_acpd_is_the_path(self) -> None:
        # ACPd answers its own acp and, through SSH, the network.
        unanswered = AirportAcpReading(password_matches=None, error="ACP receive failed: timed out")
        error, _debug, _answer, _processes = self.gate(reading=unanswered)
        self.assertEqual(error.code, "acp_unreachable")
        self.assertIn("10.0.0.2", str(error))
        self.assertIn("nothing was changed", str(error))
        self.network.assert_called_once_with(self.CONNECTION)

    def test_an_unanswered_network_read_that_acpd_leaves_unanswered_through_ssh_is_acpd(self) -> None:
        # Its own acp answers over its Unix socket; its TCP listener does not.
        unanswered = AirportAcpReading(password_matches=None, error="Could not connect to ACP on 10.0.0.2:5009: timed out")
        evidence = {"remote_kern_limits": "kern.maxfiles = 1024", "remote_kernel_table_full": ["file: table is full"]}
        with mock.patch("timecapsulesmb.device.acpd.network_silent_details", return_value=evidence):
            error, debug, _answer, _processes = self.gate(reading=unanswered, network_answers=False)
        # The evidence goes with the problem to telemetry.
        self.assertEqual(debug["remote_kern_limits"], "kern.maxfiles = 1024")
        self.assertEqual(debug["remote_kernel_table_full"], ["file: table is full"])
        self.assertEqual(error.code, "acpd_broken")
        self.assertTrue(str(error).startswith("Apple ACPd is broken: the AirPort service stopped answering the network"))
        self.assertIn("Nothing was changed", str(error))
        # acpd_broken's next steps say to unplug it.
        self.assertIn("Unplug the device, wait 10 seconds and plug it back in.", cli_steps("deploy", error.code))
        self.assertNotIn("VPN", str(error))
        self.assertEqual(debug["acpd_problems"], ["network_silent"])
        self.assertEqual(debug["acp_read_error"], "Could not connect to ACP on 10.0.0.2:5009: timed out")

    def test_a_read_of_its_own_that_goes_unanswered_is_checked_through_ssh(self) -> None:
        # A profile without a saved MAC brings no earlier read: the gate reads
        # the device itself and checks ACPd's listener with that read.
        unanswered = AirportAcpReading(password_matches=None, error="ACP receive failed: timed out")
        debug: dict[str, object] = {}
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=unanswered) as read, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=False) as network:
            with self.assertRaises(service_runtime.DeviceAccessError) as raised:
                service_runtime.require_acpd_ready(self.CONNECTION, OperationCallbacks(add_debug_fields=debug.update))
        read.assert_called_once()
        network.assert_called_once_with(self.CONNECTION)
        self.assertEqual(raised.exception.code, "acpd_broken")
        self.assertEqual(debug["acpd_problems"], ["network_silent"])

    def test_a_silent_listener_is_named_off_this_computers_network_too(self) -> None:
        # Off the network or not, ACPd does not answer even through SSH.
        unanswered = AirportAcpReading(password_matches=None, error="No route to host")
        networks = [ipaddress.ip_network("192.168.5.0/24")]
        error, debug, _answer, _processes = self.gate(reading=unanswered, off_network=networks, network_answers=False)
        self.assertEqual(error.code, "acpd_broken")
        self.assertEqual(debug["acpd_problems"], ["network_silent"])

    def test_an_unanswered_network_read_off_this_computers_network_says_so(self) -> None:
        unanswered = AirportAcpReading(password_matches=None, error="No route to host")
        networks = [ipaddress.ip_network("192.168.5.0/24")]
        with mock.patch("timecapsulesmb.services.runtime.device_off_network_message", return_value="off network") as message:
            error, _debug, _answer, _processes = self.gate(reading=unanswered, off_network=networks)
        self.assertEqual((error.code, str(error)), ("device_off_network", "off network"))
        message.assert_called_once_with("10.0.0.2", networks)

    def test_an_ssh_failure_asking_acpd_is_raised_not_blamed_on_acpd(self) -> None:
        # A slow device or a dropped session is not Apple's fault: the SSH
        # error goes up, where the caller reports a slow or unreachable device.
        unanswered = AirportAcpReading(password_matches=None, error="ACP receive failed: timed out")
        for read in ("acpd_answers", "restart_never_happened", "acpd_answers_network"):
            for failure in (SshCommandTimeout("Timed out waiting for ssh command to finish"), SshError("Connection reset")):
                with self.subTest(read=read, failure=type(failure).__name__):
                    callbacks = OperationCallbacks()
                    reads = {
                        "acpd_answers": mock.Mock(return_value=True),
                        "restart_never_happened": mock.Mock(return_value=False),
                        "read_processes": mock.Mock(return_value=""),
                        read: mock.Mock(side_effect=failure),
                    }
                    with mock.patch.multiple("timecapsulesmb.device.acpd", **reads):
                        with self.assertRaises(type(failure)) as raised:
                            service_runtime.require_acpd_ready(self.CONNECTION, callbacks, reading=unanswered)
                    self.assertIs(raised.exception, failure)

    def test_no_password_to_check_is_not_an_unanswered_read(self) -> None:
        error, _debug, _answer, _children = self.gate(reading=AirportAcpReading(password_matches=None))
        self.assertIsNone(error)


class ConfirmDeviceAddressTests(unittest.TestCase):
    """The saved address checked against the AirPort MAC the app saved."""

    MOVED = LocateResult("found", host="root@10.0.0.9", address="10.0.0.9")

    def confirm(self, reading, located: LocateResult | None = None):
        recorder = RecordingCallbacks()
        locate = mock.Mock(return_value=located or LocateResult("not_found"))
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=reading), \
                mock.patch("timecapsulesmb.services.runtime.locate_airport", locate):
            try:
                outcome = service_runtime.confirm_device_address("root@10.0.0.2", "pw", DEVICE_AIRPORT_MAC, recorder.callbacks())[0]
            except service_runtime.DeviceAccessError as exc:
                outcome = exc
        return outcome, locate, recorder

    def test_the_device_at_its_saved_address_is_used_there(self) -> None:
        for reading in (acp_reading(True), AirportAcpReading(password_matches=True, airport_mac=None)):
            with self.subTest(reading=reading):
                outcome, locate, recorder = self.confirm(reading)

                self.assertEqual(outcome, "root@10.0.0.2")
                locate.assert_not_called()
                self.assertNotIn("current_host", recorder.fields)

    def test_a_device_that_moved_is_used_at_its_new_address(self) -> None:
        readings = {
            "unreachable": acp_reading(None),
            # Another AirPort took the address, with another password or the same one.
            "password_rejected": acp_reading(False),
            "identity_mismatch": AirportAcpReading(password_matches=True, airport_mac="02:00:00:00:00:02"),
        }
        for trigger, reading in readings.items():
            with self.subTest(trigger=trigger):
                outcome, locate, recorder = self.confirm(reading, self.MOVED)

                self.assertEqual(outcome, "root@10.0.0.9")
                self.assertEqual(locate.call_args.kwargs["trigger"], trigger)
                self.assertEqual(locate.call_args.kwargs["current_host"], "root@10.0.0.2")
                self.assertEqual(recorder.fields["current_host"], "root@10.0.0.9")
                self.assertIn("The device now answers at 10.0.0.9; the saved address is out of date.", recorder.messages)

    def test_a_device_found_nowhere_else_stays_at_its_saved_address(self) -> None:
        # The command's own probe or password check then says what is wrong.
        for reading in (acp_reading(None), acp_reading(False)):
            with self.subTest(reading=reading):
                outcome, _locate, recorder = self.confirm(reading)

                self.assertEqual(outcome, "root@10.0.0.2")
                self.assertNotIn("current_host", recorder.fields)

    def test_another_airport_at_the_address_with_the_device_nowhere_fails(self) -> None:
        outcome, _locate, _recorder = self.confirm(AirportAcpReading(password_matches=True, airport_mac="02:00:00:00:00:02"))

        self.assertIsInstance(outcome, service_runtime.DeviceAccessError)
        self.assertEqual(outcome.code, "device_identity_mismatch")
        self.assertIn("A different AirPort answers at 10.0.0.2", str(outcome))

    def test_a_device_back_with_another_password_is_reported_and_stays_at_its_saved_address(self) -> None:
        # Only the record's MAC says it is this device, and the record may be a
        # stale cache entry for an address another AirPort took.
        located = LocateResult("password_rejected", address="10.0.1.1")
        outcome, _locate, recorder = self.confirm(acp_reading(None), located)

        self.assertEqual(outcome, "root@10.0.0.2")
        self.assertIn(located.rejected_note, recorder.messages)

    def test_the_read_that_placed_the_device_is_returned_for_the_password_check(self) -> None:
        cases = {
            "there": (acp_reading(True), None, ("root@10.0.0.2", acp_reading(True))),
            "moved": (acp_reading(None), self.MOVED, ("root@10.0.0.9", acp_reading(True))),
            "unanswered": (acp_reading(None), None, ("root@10.0.0.2", acp_reading(None))),
        }
        for case, (reading, located, expected) in cases.items():
            with self.subTest(case=case):
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=reading), \
                        mock.patch("timecapsulesmb.services.runtime.locate_airport", return_value=located or LocateResult("not_found")):
                    result = service_runtime.confirm_device_address("root@10.0.0.2", "pw", DEVICE_AIRPORT_MAC, RecordingCallbacks().callbacks())
                self.assertEqual(result, expected)


class AirportIdentityTests(unittest.TestCase):
    """The device identity commands report, for the app to save with its profile."""

    def test_the_fields_carry_the_mac_and_the_names_apple_advertises(self) -> None:
        cases = {
            "name": (("Gregory's AirPort Time Capsule", None), {
                "airport_instance_name": "Gregory's AirPort Time Capsule",
                # ACPd drops the apostrophe and keeps case; the app compares lowercased.
                "airport_host_label": "gregorys-airport-time-capsule", "airport_host_label_source": "syNm",
            }),
            # ACPd takes the host label from syDN before syNm; such a label stays when syNm changes.
            "dns name": (("Office", "Den Capsule"), {"airport_instance_name": "Office", "airport_host_label": "den-capsule",
                                                     "airport_host_label_source": "syDN"}),
            # A syDN with nothing ACPd keeps (all non-ASCII) leaves the label to syNm.
            "unusable dns name": (("Office", "日本"), {"airport_instance_name": "Office", "airport_host_label": "office",
                                                     "airport_host_label_source": "syNm"}),
            # Over 63 bytes mDNSResponder advertises a name of its own.
            "long name": (("x" * 64, None), {"airport_host_label": "x" * 63, "airport_host_label_source": "syNm"}),
            # No name of its own: Base-Station-... names no device.
            "no name": ((None, None), {}),
            "non-ASCII name": (("日本語", None), {"airport_instance_name": "日本語"}),
        }
        for case, ((name, dns_name), expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(service_runtime.airport_identity_fields(DEVICE_AIRPORT_MAC, name, dns_name),
                                 {"airport_mac": DEVICE_AIRPORT_MAC, **expected})
        self.assertEqual(service_runtime.airport_identity_fields(None, "Office"), {})

    def test_only_a_read_that_took_the_password_is_reported(self) -> None:
        named = AirportAcpReading(password_matches=True, airport_mac=DEVICE_AIRPORT_MAC, airport_name="Office")
        cases = {
            "match": (named, {"airport_mac": DEVICE_AIRPORT_MAC, "airport_instance_name": "Office", "airport_host_label": "office",
                              "airport_host_label_source": "syNm"}),
            "rejected": (AirportAcpReading(password_matches=False), {}),
            "unanswered": (acp_reading(None), {}),
            "no mac": (AirportAcpReading(password_matches=True, airport_name="Office"), {}),
        }
        for case, (reading, expected) in cases.items():
            with self.subTest(case=case):
                recorder = RecordingCallbacks()
                service_runtime.report_airport_identity(recorder.callbacks(), reading)
                self.assertEqual(recorder.fields, expected)

    def test_the_password_check_before_a_reboot_reports_the_device(self) -> None:
        recorder = RecordingCallbacks()
        reading = AirportAcpReading(password_matches=True, airport_mac=DEVICE_AIRPORT_MAC, airport_name="Office")
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=True):
            service_runtime.require_acpd_ready(SshConnection("root@10.0.0.2", "pw", ""), recorder.callbacks(), reading=reading)
        self.assertEqual(recorder.fields["airport_mac"], DEVICE_AIRPORT_MAC)
        self.assertEqual(recorder.fields["airport_instance_name"], "Office")

    def test_a_rejected_password_check_reports_nothing(self) -> None:
        recorder = RecordingCallbacks()
        with self.assertRaises(service_runtime.DeviceAccessError):
            service_runtime.require_acpd_ready(SshConnection("root@10.0.0.2", "pw", ""), recorder.callbacks(), reading=acp_reading(False))
        self.assertEqual(recorder.fields, {})

    def test_the_saved_device_at_its_address_is_reported_another_airport_is_not(self) -> None:
        named = AirportAcpReading(password_matches=True, airport_mac=DEVICE_AIRPORT_MAC, airport_name="Office")
        other = AirportAcpReading(password_matches=True, airport_mac="02:00:00:00:00:02", airport_name="Den")
        for reading, reported in ((named, True), (other, False)):
            with self.subTest(reading=reading):
                recorder = RecordingCallbacks()
                with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=reading) as read, \
                        mock.patch("timecapsulesmb.services.runtime.locate_airport", return_value=LocateResult("not_found")):
                    try:
                        service_runtime.confirm_device_address("root@10.0.0.2", "pw", DEVICE_AIRPORT_MAC, recorder.callbacks())
                    except service_runtime.DeviceAccessError:
                        pass
                # Commands read twice before looking elsewhere; configure once.
                read.assert_called_once_with("root@10.0.0.2", "pw", attempts=2)
                self.assertEqual("airport_mac" in recorder.fields, reported)
                self.assertNotIn("Den", recorder.fields.values())

    def test_learning_reads_once(self) -> None:
        recorder = RecordingCallbacks()
        reading = AirportAcpReading(password_matches=True, airport_mac=DEVICE_AIRPORT_MAC, airport_name="Office")
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=reading) as read:
            service_runtime.learn_airport_identity("root@10.0.0.2", "pw", recorder.callbacks())
        read.assert_called_once_with("root@10.0.0.2", "pw", attempts=1)
        self.assertEqual(recorder.fields["airport_mac"], DEVICE_AIRPORT_MAC)


class FollowSavedDeviceTests(unittest.TestCase):
    """The saved device reached where it answers now, for the app and the CLI."""

    def follow(self, values: dict[str, str], airport_mac, *, host="root@10.0.0.2", reading=None):
        config = app_config(values)
        result = (host, reading or acp_reading(True))
        with mock.patch("timecapsulesmb.services.runtime.confirm_device_address", return_value=result) as confirm:
            followed, device_reading = service_runtime.follow_saved_device(config, airport_mac, RecordingCallbacks().callbacks())
        return config, followed, device_reading, confirm

    def test_without_a_mac_or_a_password_nothing_is_read(self) -> None:
        for values, mac in ((valid_env(), None), ({**valid_env(), "TC_PASSWORD": ""}, DEVICE_AIRPORT_MAC)):
            with self.subTest(mac=mac):
                config, followed, device_reading, confirm = self.follow(values, mac)
                self.assertIs(followed, config)
                self.assertIsNone(device_reading)
                confirm.assert_not_called()

    def test_a_device_at_its_saved_address_keeps_the_config(self) -> None:
        values = valid_env()
        config, followed, device_reading, _confirm = self.follow(values, DEVICE_AIRPORT_MAC, host=values["TC_HOST"])
        self.assertIs(followed, config)
        self.assertEqual(device_reading, (values["TC_HOST"], acp_reading(True)))

    def test_a_moved_device_changes_only_this_commands_copy(self) -> None:
        config, followed, device_reading, _confirm = self.follow(valid_env(), DEVICE_AIRPORT_MAC, host="root@10.0.0.9")
        self.assertEqual(followed.get("TC_HOST"), "root@10.0.0.9")
        self.assertNotEqual(config.get("TC_HOST"), "root@10.0.0.9")
        self.assertEqual(followed.path, config.path)
        self.assertEqual(device_reading, ("root@10.0.0.9", acp_reading(True)))


class SavedHostnameTests(unittest.TestCase):
    def test_a_profile_saved_by_hostname_is_not_looked_for_elsewhere(self) -> None:
        # A hostname resolves to wherever the device is now.
        recorder = RecordingCallbacks()
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", return_value=acp_reading(None)), \
                mock.patch("timecapsulesmb.discovery.bonjour.BonjourQuery.browse") as browse:
            host, reading = service_runtime.confirm_device_address(
                "root@Office-Capsule.local", "pw", DEVICE_AIRPORT_MAC, recorder.callbacks(),
            )

        self.assertEqual((host, reading), ("root@Office-Capsule.local", acp_reading(None)))
        browse.assert_not_called()
        self.assertTrue(recorder.measurement("host_follow")["saved_hostname"])


class SavedAirportMacTests(unittest.TestCase):
    def test_the_saved_mac_is_the_configs(self) -> None:
        saved = app_config({**valid_env(), "TC_AIRPORT_MAC": "02-00-00-00-00-0B"})
        self.assertEqual(saved_airport_mac(saved), "02:00:00:00:00:0b")
        self.assertIsNone(saved_airport_mac(app_config({**valid_env(), "TC_AIRPORT_MAC": ""})))
        self.assertIsNone(saved_airport_mac(app_config(valid_env())))
        with self.assertRaises(ValueError):
            saved_airport_mac(app_config({**valid_env(), "TC_AIRPORT_MAC": "not-a-mac"}))

    def test_the_app_owns_its_profiles_mac(self) -> None:
        # The app's registry, not the .env configure wrote, says which device a profile is.
        saved = app_config({**valid_env(), "TC_AIRPORT_MAC": "02:00:00:00:00:0b"})
        overlaid = overlay_request_airport_mac(saved, {"airport_mac": "02-00-00-00-00-0A"})
        self.assertEqual(saved_airport_mac(overlaid), "02:00:00:00:00:0a")
        self.assertIsNone(saved_airport_mac(overlay_request_airport_mac(saved, {})))
        self.assertEqual(overlaid.path, saved.path)
        with self.assertRaises(ValueError):
            overlay_request_airport_mac(saved, {"airport_mac": "not-a-mac"})
