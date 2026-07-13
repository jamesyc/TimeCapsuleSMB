from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.config import (
    AIRPORT_SYAP_TO_MODEL,
    AppConfig,
    ConfigError,
    ConfigValidationError,
    build_mdns_device_model_txt,
    DEFAULT_ENV_FILE_MODE,
    DEFAULTS,
    env_file_target_mode,
    load_app_config,
    parse_bool,
    parse_env_file,
    parse_env_value,
    preserved_env_file_values,
    require_valid_app_config,
    render_env_text,
    validate_app_config,
    validate_airport_syap,
    validate_bool,
    validate_mdns_device_model_matches_syap,
    validate_mdns_device_model,
    validate_ssh_target,
    write_env_file,
)
from timecapsulesmb.core.net import endpoint_host
from timecapsulesmb.core.paths import manifest_artifact_paths, resolve_app_paths, resolve_distribution_root


class ConfigTests(unittest.TestCase):
    def valid_deploy_file_values(self) -> dict[str, str]:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        return values

    def test_load_app_config_applies_defaults_and_unquotes_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST='root@10.0.0.5'\nTC_NETBIOS_NAME='ArchiveCapsule'\n")
            values = load_app_config(path).values
        self.assertEqual(values["TC_HOST"], "root@10.0.0.5")
        self.assertEqual(values["TC_NETBIOS_NAME"], "ArchiveCapsule")
        self.assertNotIn("TC_MDNS_HOST_LABEL", DEFAULTS)

    def test_parse_env_file_does_not_apply_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST='root@10.0.0.5'\n")
            values = parse_env_file(path)
        self.assertEqual(values, {"TC_HOST": "root@10.0.0.5"})

    def test_load_app_config_tracks_missing_env_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            config = load_app_config(path)
        self.assertFalse(config.exists)
        self.assertEqual(config.path, path)
        self.assertEqual(config.file_values, {})
        self.assertEqual(config.get("TC_HOST"), DEFAULTS["TC_HOST"])

    def test_load_app_config_tracks_file_values_and_merged_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST=root@10.0.0.5\n")
            config = load_app_config(path)
        self.assertTrue(config.exists)
        self.assertEqual(config.file_values, {"TC_HOST": "root@10.0.0.5"})
        self.assertTrue(config.has_file_value("TC_HOST"))

    def test_validate_app_config_reports_missing_env_before_defaulted_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_app_config(Path(tmp) / ".env")
            errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "missing_file")
        self.assertIsNone(errors[0].key)

    def test_validate_app_config_does_not_require_saved_airport_identity(self) -> None:
        values = self.valid_deploy_file_values()
        file_values = dict(values)
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.from_values(
                values,
                path=Path(tmp) / ".env",
                exists=True,
                file_values=file_values,
            )
            errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors, [])

    def test_require_valid_app_config_formats_actual_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            config = AppConfig.from_values({}, path=path, exists=True, file_values={})
            with self.assertRaises(ConfigValidationError) as ctx:
                require_valid_app_config(config, profile="deploy", command_name="deploy")
        self.assertNotIsInstance(ctx.exception, SystemExit)
        self.assertIn(f"Missing required setting in {path}: TC_HOST", str(ctx.exception))
        self.assertIn("before running `deploy`", str(ctx.exception))

    def test_resolve_distribution_root_prefers_start_project_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            nested = root / "nested"
            nested.mkdir()
            (root / "tcapsule").write_text("#!/usr/bin/env python3\n")
            (root / "src" / "timecapsulesmb").mkdir(parents=True)
            for relative_path in manifest_artifact_paths():
                artifact_path = root / relative_path
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(b"payload")
            self.assertEqual(resolve_distribution_root(nested), root)
            self.assertEqual(resolve_app_paths(nested).config_path, root / ".env")

    def test_render_env_text_contains_config_keys(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_CONFIGURE_ID"] = "12345678-1234-1234-1234-123456789012"
        rendered = render_env_text(values)
        self.assertIn("TC_PASSWORD=secret", rendered)
        self.assertIn(f"TC_SSH_OPTS={shlex.quote(DEFAULTS['TC_SSH_OPTS'])}", rendered)
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("TC_MDNS_HOST_LABEL", rendered)
        self.assertNotIn("TC_NETBIOS_NAME", rendered)
        self.assertNotIn("NET_IPV4_HINT", rendered)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", rendered)
        self.assertNotIn("TC_AIRPORT_SYAP", rendered)
        self.assertNotIn("TC_SAMBA_USER", rendered)
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", rendered)
        self.assertIn("TC_INTERNAL_SHARE_USE_DISK_ROOT=false", rendered)
        self.assertIn("TC_SMB_BIND_LAN_ONLY=false", rendered)
        self.assertIn("TC_SMB_BROWSE_COMPATIBILITY=false", rendered)
        self.assertIn("TC_MDNS_ADVERTISE_AFP=false", rendered)
        self.assertIn("TC_ANY_PROTOCOL=false", rendered)
        self.assertIn("TC_REQUIRE_SMB_ENCRYPTION=false", rendered)
        self.assertIn("TC_FRUIT_METADATA_NETATALK=true", rendered)
        self.assertIn("TC_DEBUG_LOGGING=false", rendered)
        self.assertIn("TC_ATA_IDLE_SECONDS=300", rendered)
        self.assertIn("TC_ATA_STANDBY=''", rendered)
        self.assertIn("TC_CONFIGURE_ID=12345678-1234-1234-1234-123456789012", rendered)

    def test_render_env_text_preserves_custom_settings_but_omits_deprecated_keys(self) -> None:
        values = dict(DEFAULTS)
        values.update({
            "TC_PASSWORD": "secret",
            "TC_CUSTOM_SETTING": "kept value",
            "CUSTOM_FLAG": "",
            "TC_SAMBA_USER": "admin",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "old-name",
        })

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(render_env_text(values))
            reparsed = parse_env_file(path)

        self.assertEqual(reparsed["TC_CUSTOM_SETTING"], "kept value")
        self.assertEqual(reparsed["CUSTOM_FLAG"], "")
        self.assertNotIn("TC_SAMBA_USER", reparsed)
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", reparsed)
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", reparsed)

    def test_preserved_env_file_values_filters_deprecated_runtime_keys(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_CUSTOM_SETTING": "kept",
            "TC_AIRPORT_SYAP": "119",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "NET_IPV4_HINT": "10.0.0.2",
        }

        preserved = preserved_env_file_values(values)

        self.assertEqual(preserved, {"TC_HOST": "root@10.0.0.2", "TC_CUSTOM_SETTING": "kept"})

    def test_env_example_does_not_include_runtime_derived_settings(self) -> None:
        values = parse_env_file(REPO_ROOT / ".env.example")
        self.assertNotIn("TC_PAYLOAD_DIR_NAME", values)
        self.assertNotIn("TC_SAMBA_USER", values)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", values)
        self.assertNotIn("TC_AIRPORT_SYAP", values)

    def test_parse_bool_accepts_true_case_insensitively(self) -> None:
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("TRUE"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool(""))

    def test_validate_bool_accepts_true_false_and_missing_default(self) -> None:
        self.assertIsNone(validate_bool("true", "Flag"))
        self.assertIsNone(validate_bool("false", "Flag"))
        self.assertIsNone(validate_bool("", "Flag"))
        self.assertEqual(validate_bool("yes", "Flag"), "Flag must be true or false.")

    def test_env_file_target_mode_defaults_and_overrides(self) -> None:
        self.assertEqual(env_file_target_mode({}), DEFAULT_ENV_FILE_MODE)
        self.assertEqual(env_file_target_mode({}), 0o600)
        self.assertEqual(env_file_target_mode({"TCAPSULE_ENV_FILE_MODE": "640"}), 0o640)
        self.assertEqual(env_file_target_mode({"TCAPSULE_ENV_FILE_MODE": "0640"}), 0o640)
        # Invalid or out-of-range values fall back to the safe default.
        self.assertEqual(env_file_target_mode({"TCAPSULE_ENV_FILE_MODE": "not-octal"}), 0o600)
        self.assertEqual(env_file_target_mode({"TCAPSULE_ENV_FILE_MODE": "99999"}), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_write_env_file_defaults_to_0600(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TCAPSULE_ENV_FILE_MODE", None)
                write_env_file(path, values)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_write_env_file_honors_mode_override(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with mock.patch.dict(os.environ, {"TCAPSULE_ENV_FILE_MODE": "640"}):
                write_env_file(path, values)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_write_env_file_omits_mdns_device_model(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_MDNS_DEVICE_MODEL"] = "AirPortTimeCapsule"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertNotIn("TC_MDNS_DEVICE_MODEL", reparsed)

    def test_write_env_file_omits_airport_syap(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_AIRPORT_SYAP"] = "119"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertNotIn("TC_AIRPORT_SYAP", reparsed)

    def test_validate_airport_syap_accepts_known_codes(self) -> None:
        for value in ("104", "105", "106", "108", "109", "113", "114", "116", "117", "119", "120"):
            self.assertIsNone(validate_airport_syap(value, "Airport Utility syAP code"))

    def test_validate_airport_syap_rejects_invalid_values(self) -> None:
        self.assertIsNone(validate_airport_syap("119", "Airport Utility syAP code"))
        self.assertEqual(validate_airport_syap("999", "Airport Utility syAP code"), "The configured syAP is invalid.")
        self.assertEqual(validate_airport_syap("abc", "Airport Utility syAP code"), "Airport Utility syAP code must contain only digits.")
        self.assertEqual(validate_airport_syap("", "Airport Utility syAP code"), "Airport Utility syAP code cannot be blank.")

    def test_airport_syap_to_model_mapping_matches_supported_models(self) -> None:
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["104"], "AirPort5,104")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["105"], "AirPort5,105")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["106"], "TimeCapsule6,106")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["108"], "AirPort5,108")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["109"], "TimeCapsule6,109")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["113"], "TimeCapsule6,113")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["114"], "AirPort5,114")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["116"], "TimeCapsule6,116")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["117"], "AirPort5,117")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["119"], "TimeCapsule8,119")
        self.assertEqual(AIRPORT_SYAP_TO_MODEL["120"], "AirPort7,120")

    def test_validate_mdns_device_model_matches_syap_requires_exact_match(self) -> None:
        self.assertIsNone(validate_mdns_device_model_matches_syap("119", "TimeCapsule8,119"))
        self.assertIsNone(validate_mdns_device_model_matches_syap("120", "AirPort7,120"))
        self.assertEqual(
            validate_mdns_device_model_matches_syap("119", "TimeCapsule"),
            'TC_MDNS_DEVICE_MODEL "TimeCapsule" must match the configured '
            'syAP expected value "TimeCapsule8,119".'
        )
        self.assertEqual(
            validate_mdns_device_model_matches_syap("119", "TimeCapsule6,113"),
            'TC_MDNS_DEVICE_MODEL "TimeCapsule6,113" must match the configured '
            'syAP expected value "TimeCapsule8,119".'
        )
        self.assertEqual(
            validate_mdns_device_model_matches_syap("120", "TimeCapsule8,119"),
            'TC_MDNS_DEVICE_MODEL "TimeCapsule8,119" must match the configured '
            'syAP expected value "AirPort7,120".'
        )
        self.assertIsNone(validate_mdns_device_model_matches_syap("", "TimeCapsule"))

    def test_write_env_file_round_trips_configure_id(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_CONFIGURE_ID"] = "12345678-1234-1234-1234-123456789012"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertEqual(reparsed["TC_CONFIGURE_ID"], "12345678-1234-1234-1234-123456789012")

    def test_write_env_file_is_atomic_when_replace_fails(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.5"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST='root@10.0.0.2'\n")
            with mock.patch("timecapsulesmb.core.config.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_env_file(path, values)

            self.assertEqual(parse_env_file(path)["TC_HOST"], "root@10.0.0.2")
            self.assertEqual(list(Path(tmp).glob(".env.*.tmp")), [])

    def test_parse_env_value_falls_back_for_unbalanced_quotes(self) -> None:
        self.assertEqual(parse_env_value("'unterminated"), "unterminated")

    def test_parse_env_value_preserves_unquoted_multi_token_string(self) -> None:
        value = "-o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1"
        self.assertEqual(parse_env_value(value), value)

    def test_parse_env_value_unquotes_multi_token_quoted_string(self) -> None:
        value = "'-o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1'"
        self.assertEqual(
            parse_env_value(value),
            "-o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1",
        )

    def test_parse_env_file_preserves_full_ssh_opts_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            ssh_opts = (
                "-o HostKeyAlgorithms=+ssh-rsa "
                "-o PubkeyAcceptedAlgorithms=+ssh-rsa "
                "-o KexAlgorithms=+diffie-hellman-group14-sha1 "
                "-o ProxyCommand=ssh\\ -4\\ -W\\ %h:%p\\ jump.example.com"
            )
            path.write_text(f"TC_SSH_OPTS='{ssh_opts}'\n")
            values = parse_env_file(path)
        self.assertEqual(values["TC_SSH_OPTS"], ssh_opts)

    def test_write_env_file_round_trips_full_ssh_opts(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_SSH_OPTS"] = (
            "-J user@jump.example.com:22123 "
            "-o HostKeyAlgorithms=+ssh-rsa "
            "-o KexAlgorithms=+diffie-hellman-group14-sha1"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertEqual(reparsed["TC_SSH_OPTS"], values["TC_SSH_OPTS"])

    def test_render_env_text_falls_back_to_default_ssh_opts_when_missing(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.5",
            "TC_PASSWORD": "secret",
            "TC_SAMBA_USER": "admin",
            "TC_PAYLOAD_DIR_NAME": ".samba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        rendered = render_env_text(values)
        self.assertIn(f"TC_SSH_OPTS={shlex.quote(DEFAULTS['TC_SSH_OPTS'])}", rendered)

    def test_endpoint_host_removes_user_prefix(self) -> None:
        self.assertEqual(endpoint_host("root@10.0.0.5"), "10.0.0.5")
        self.assertEqual(endpoint_host("10.0.0.5"), "10.0.0.5")

    def test_build_mdns_device_model_txt(self) -> None:
        self.assertEqual(build_mdns_device_model_txt("TimeCapsule"), "model=TimeCapsule")

    def test_validate_mdns_device_model_accepts_supported_values(self) -> None:
        for value in (
            "TimeCapsule",
            "AirPort",
            "AirPort5,104",
            "AirPort5,105",
            "TimeCapsule6,106",
            "AirPort5,108",
            "TimeCapsule6,109",
            "TimeCapsule6,113",
            "AirPort5,114",
            "TimeCapsule6,116",
            "AirPort5,117",
            "TimeCapsule8,119",
            "AirPort7,120",
        ):
            self.assertIsNone(validate_mdns_device_model(value, "mDNS device model hint"))

    def test_validate_mdns_device_model_rejects_unsupported_values(self) -> None:
        self.assertEqual(
            validate_mdns_device_model("AirPortTimeCapsule", "mDNS device model hint"),
            "mDNS device model hint is not a supported AirPort storage device model.",
        )
        self.assertEqual(
            validate_mdns_device_model("TimeCapsule7,117", "mDNS device model hint"),
            "mDNS device model hint is not a supported AirPort storage device model.",
        )
        self.assertEqual(validate_mdns_device_model("", "mDNS device model hint"), "mDNS device model hint cannot be blank.")

    def test_validate_ssh_target_accepts_user_at_host_targets(self) -> None:
        self.assertIsNone(validate_ssh_target("root@10.0.0.2", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@10.0.0.2:22", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@127.0.0.1", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@localhost", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@timecapsule.local", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@timecapsule.local:22", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("admin_user@wan.example.com", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@[fd00::2]:22", "Device SSH target"))

    def test_validate_ssh_target_rejects_bare_or_unsafe_targets(self) -> None:
        self.assertEqual(
            validate_ssh_target("10.0.0.2", "Device SSH target"),
            "Device SSH target must include a username, like root@192.168.x.x",
        )
        self.assertEqual(validate_ssh_target("@10.0.0.2", "Device SSH target"), "Device SSH target must include a username before @.")
        self.assertEqual(validate_ssh_target("root@", "Device SSH target"), "Device SSH target must include a host after @.")
        self.assertEqual(validate_ssh_target("root user@10.0.0.2", "Device SSH target"), "Device SSH target must not contain whitespace.")
        self.assertEqual(
            validate_ssh_target("root;reboot@10.0.0.2", "Device SSH target"),
            "Device SSH target username may contain only letters, numbers, dots, underscores, and hyphens.",
        )
        self.assertEqual(
            validate_ssh_target("root@10.0.0.2:2222", "Device SSH target"),
            "Device SSH target only supports the default SSH port 22. Set custom SSH ports in TC_SSH_OPTS.",
        )
        self.assertEqual(
            validate_ssh_target("root@timecapsule.local:ssh", "Device SSH target"),
            "Device SSH target port must be numeric.",
        )
        self.assertEqual(
            validate_ssh_target("root@169.254.44.9", "Device SSH target"),
            "Device SSH target host must not be a link-local address. "
            "Use the device's LAN IP or a hostname that resolves to its LAN IP; "
            "link-local addresses are only suitable for temporary SSH recovery.",
        )
        self.assertEqual(
            validate_ssh_target("root@fe80::1%en0", "Device SSH target"),
            "Device SSH target host must not be a link-local address. "
            "Use the device's LAN IP or a hostname that resolves to its LAN IP; "
            "link-local addresses are only suitable for temporary SSH recovery.",
        )

    def test_validate_ssh_target_rejects_placeholder_default_ip(self) -> None:
        self.assertEqual(
            validate_ssh_target("root@192.168.x.x", "Device SSH target"),
            "Device SSH target IP address is invalid. "
            "Replace 192.168.x.x with the device's actual IP address.",
        )
        self.assertIsNone(validate_ssh_target("root@192.168.1.101", "Device SSH target"))

    def test_validate_app_config_uses_profiles(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule8,119"
        config = AppConfig.from_values(values, file_values=values)
        self.assertEqual(validate_app_config(config, profile="deploy"), [])
        values["TC_MDNS_HOST_LABEL"] = "Time Capsule"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors, [])
        values["TC_ANY_PROTOCOL"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_ANY_PROTOCOL")
        values["TC_ANY_PROTOCOL"] = "false"
        values["TC_REQUIRE_SMB_ENCRYPTION"] = "true"
        values["TC_ANY_PROTOCOL"] = "true"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "inconsistent_values")
        self.assertEqual(errors[0].key, "TC_REQUIRE_SMB_ENCRYPTION")
        values["TC_ANY_PROTOCOL"] = "false"
        values["TC_REQUIRE_SMB_ENCRYPTION"] = "false"
        values["TC_SMB_BIND_LAN_ONLY"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_SMB_BIND_LAN_ONLY")
        values["TC_SMB_BIND_LAN_ONLY"] = "true"
        values["TC_SMB_BROWSE_COMPATIBILITY"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_SMB_BROWSE_COMPATIBILITY")
        values["TC_SMB_BROWSE_COMPATIBILITY"] = "false"
        values["TC_MDNS_ADVERTISE_AFP"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_MDNS_ADVERTISE_AFP")
        values["TC_MDNS_ADVERTISE_AFP"] = "true"
        values["TC_FRUIT_METADATA_NETATALK"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_FRUIT_METADATA_NETATALK")
        values["TC_FRUIT_METADATA_NETATALK"] = "false"
        values["TC_DEBUG_LOGGING"] = "not-bool"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_DEBUG_LOGGING")
        values["TC_DEBUG_LOGGING"] = "false"
        values["TC_ATA_IDLE_SECONDS"] = "-1"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_ATA_IDLE_SECONDS")
        values["TC_ATA_IDLE_SECONDS"] = ""
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "invalid_value")
        self.assertEqual(errors[0].key, "TC_ATA_IDLE_SECONDS")

    def test_flash_profile_ignores_deploy_only_settings(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_NET_IFACE"] = "not a valid interface"
        values["TC_AIRPORT_SYAP"] = "not-a-syap"
        values["TC_MDNS_DEVICE_MODEL"] = "not-a-model"
        values["TC_SAMBA_USER"] = "bad user"
        values["TC_PAYLOAD_DIR_NAME"] = "/bad"
        values["TC_INTERNAL_SHARE_USE_DISK_ROOT"] = "not-bool"
        values["TC_SMB_BIND_LAN_ONLY"] = "not-bool"
        values["TC_SMB_BROWSE_COMPATIBILITY"] = "not-bool"
        values["TC_MDNS_ADVERTISE_AFP"] = "not-bool"
        values["TC_ANY_PROTOCOL"] = "not-bool"
        values["TC_FRUIT_METADATA_NETATALK"] = "not-bool"
        values["TC_DEBUG_LOGGING"] = "not-bool"
        values["TC_ATA_IDLE_SECONDS"] = "bad"
        values["TC_ATA_STANDBY"] = "bad"
        config = AppConfig.from_values(values, file_values=values)

        self.assertEqual(validate_app_config(config, profile="flash"), [])

    def test_flash_profile_accepts_request_scoped_password(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        file_values = dict(values)
        file_values.pop("TC_PASSWORD", None)
        config = AppConfig.from_values(values, file_values=file_values)

        self.assertEqual(validate_app_config(config, profile="flash"), [])

    def test_flash_profile_still_requires_effective_password(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        file_values = dict(values)
        file_values.pop("TC_PASSWORD", None)
        config = AppConfig.from_values(values, file_values=file_values)

        errors = validate_app_config(config, profile="flash")

        self.assertEqual(errors[0].kind, "missing_key")
        self.assertEqual(errors[0].key, "TC_PASSWORD")

    def test_doctor_profile_accepts_request_scoped_password(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        file_values = dict(values)
        file_values.pop("TC_PASSWORD", None)
        config = AppConfig.from_values(values, file_values=file_values)

        self.assertEqual(validate_app_config(config, profile="doctor"), [])

    def test_doctor_profile_still_requires_effective_password(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        file_values = dict(values)
        file_values.pop("TC_PASSWORD", None)
        config = AppConfig.from_values(values, file_values=file_values)

        errors = validate_app_config(config, profile="doctor")

        self.assertEqual(errors[0].kind, "missing_key")
        self.assertEqual(errors[0].key, "TC_PASSWORD")

    def test_validate_app_config_ignores_stale_device_model_syap_pair(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors, [])

    def test_validate_app_config_rejects_bare_deploy_host(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule8,119"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].key, "TC_HOST")
        self.assertIn("must include a username", errors[0].message)

    def test_validate_app_config_rejects_link_local_deploy_host(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@169.254.44.9"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule8,119"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].key, "TC_HOST")
        self.assertIn("must not be a link-local address", errors[0].message)

    def test_validate_app_config_rejects_ipv6_link_local_deploy_host(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@fe80::82ea:96ff:fee6:c7e5"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule8,119"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].key, "TC_HOST")
        self.assertIn("must not be a link-local address", errors[0].message)

    def test_app_config_require_raises_for_missing_value(self) -> None:
        config = AppConfig.from_values({"TC_HOST": ""})
        with self.assertRaises(ConfigError) as ctx:
            config.require("TC_HOST")
        self.assertNotIsInstance(ctx.exception, SystemExit)


if __name__ == "__main__":
    unittest.main()
