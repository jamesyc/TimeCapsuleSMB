from __future__ import annotations

import io
import socket
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
from timecapsulesmb.services.runtime import resolve_env_connection, ssh_target_link_local_resolution_error

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
            "TC_SSH_OPTS": "-o ProxyJump=bastion",
        })
        connection = resolve_env_connection(config)

        self.assertEqual(connection.ssh_opts, "-o ProxyJump=bastion")

    def test_resolve_env_connection_uses_password_provider_when_password_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})
        provider = mock.Mock(return_value="prompted-pw")

        connection = resolve_env_connection(config, password_provider=provider)

        provider.assert_called_once_with("Device root password: ")
        self.assertEqual(connection.password, "prompted-pw")

    def test_resolve_env_connection_does_not_prompt_without_provider(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2"})

        with self.assertRaises(ConfigError) as ctx:
            resolve_env_connection(config)

        self.assertIn("TC_PASSWORD is required", str(ctx.exception))

    def test_ssh_target_link_local_resolution_error_rejects_resolved_hostname(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("capsule.local resolves to link-local address 169.254.44.9", error)

    def test_ssh_target_link_local_resolution_error_rejects_resolved_ipv6_hostname(self) -> None:
        addrinfo = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::1%en0", 0, 0, 4))]

        with (
            mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", return_value=4),
            mock.patch("timecapsulesmb.core.net.socket.if_indextoname", return_value="en0"),
        ):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("capsule.local resolves to link-local address fe80::1", error)

    def test_ssh_target_link_local_resolution_error_allows_loopback_hostname(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            error = ssh_target_link_local_resolution_error(
                "root@localhost",
                DEFAULTS["TC_SSH_OPTS"],
            )

        self.assertIsNone(error)

    def test_ssh_target_link_local_resolution_error_skips_proxied_ssh(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("should not resolve")):
            error = ssh_target_link_local_resolution_error(
                "root@capsule.local",
                "-o ProxyJump=bastion",
            )

        self.assertIsNone(error)

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

    def test_managed_target_rejects_hostname_that_resolves_link_local(self) -> None:
        config = app_config(valid_env(TC_HOST="root@capsule.local"))
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.44.9", 0))]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            with self.assertRaises(ConfigError) as ctx:
                service_runtime.resolve_validated_managed_target(
                    config,
                    command_name="deploy",
                    profile="deploy",
                    include_probe=False,
                )

        self.assertIn("TC_HOST host capsule.local resolves to link-local address 169.254.44.9", str(ctx.exception))

    def test_managed_target_allows_proxied_hostname_that_resolves_link_local(self) -> None:
        config = app_config(
            valid_env(
                TC_HOST="root@capsule.local",
                TC_SSH_OPTS="-o ProxyJump=bastion",
            )
        )
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=AssertionError("should not resolve")):
            target = service_runtime.resolve_validated_managed_target(
                config,
                command_name="deploy",
                profile="deploy",
                include_probe=False,
            )

        self.assertEqual(target.connection.host, "root@capsule.local")

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
