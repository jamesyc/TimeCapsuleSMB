from __future__ import annotations

import shlex
import tempfile
import unittest
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
    build_adisk_share_txt,
    build_mdns_device_model_txt,
    DEFAULTS,
    extract_host,
    load_app_config,
    parse_bool,
    parse_env_file,
    parse_env_value,
    require_valid_app_config,
    render_env_text,
    validate_app_config,
    validate_airport_syap,
    validate_bool,
    validate_mdns_device_model_matches_syap,
    validate_mdns_device_model,
    validate_mdns_host_label,
    validate_mdns_instance_name,
    validate_netbios_name,
    validate_net_iface,
    validate_payload_dir_name,
    validate_samba_user,
    validate_ssh_target,
    write_env_file,
)
from timecapsulesmb.core.paths import manifest_artifact_paths, resolve_app_paths, resolve_project_root


class ConfigTests(unittest.TestCase):
    def valid_deploy_file_values(self) -> dict[str, str]:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule8,119"
        return values

    def test_load_app_config_applies_defaults_and_unquotes_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST='root@10.0.0.5'\nTC_NETBIOS_NAME='ArchiveCapsule'\n")
            values = load_app_config(path).values
        self.assertEqual(values["TC_HOST"], "root@10.0.0.5")
        self.assertEqual(values["TC_NETBIOS_NAME"], "ArchiveCapsule")
        self.assertEqual(values["TC_MDNS_HOST_LABEL"], DEFAULTS["TC_MDNS_HOST_LABEL"])

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
        self.assertFalse(config.has_file_value("TC_SHARE_USE_DISK_ROOT"))
        self.assertEqual(config.get("TC_SHARE_USE_DISK_ROOT"), "")

    def test_validate_app_config_reports_missing_env_before_defaulted_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_app_config(Path(tmp) / ".env")
            errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "missing_file")
        self.assertIsNone(errors[0].key)

    def test_validate_app_config_requires_file_value_for_airport_syap(self) -> None:
        values = self.valid_deploy_file_values()
        file_values = dict(values)
        file_values.pop("TC_AIRPORT_SYAP")
        values["TC_AIRPORT_SYAP"] = DEFAULTS["TC_AIRPORT_SYAP"]
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.from_values(
                values,
                path=Path(tmp) / ".env",
                exists=True,
                file_values=file_values,
            )
            errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].kind, "missing_key")
        self.assertEqual(errors[0].key, "TC_AIRPORT_SYAP")

    def test_validate_app_config_ignores_absent_legacy_share_use_disk_root(self) -> None:
        file_values = self.valid_deploy_file_values()
        file_values.pop("TC_SHARE_USE_DISK_ROOT", None)
        values = dict(DEFAULTS)
        values.update(file_values)
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

    def test_resolve_project_root_prefers_start_project_markers(self) -> None:
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
            self.assertEqual(resolve_project_root(nested), root)
            self.assertEqual(resolve_app_paths(nested).env_path, root / ".env")

    def test_render_env_text_contains_config_keys(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_CONFIGURE_ID"] = "12345678-1234-1234-1234-123456789012"
        rendered = render_env_text(values)
        self.assertIn("TC_PASSWORD=secret", rendered)
        self.assertIn("TC_SSH_OPTS='-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'", rendered)
        self.assertIn("TC_MDNS_INSTANCE_NAME='Time Capsule Samba 4'", rendered)
        self.assertIn("TC_MDNS_DEVICE_MODEL=TimeCapsule", rendered)
        self.assertIn("TC_AIRPORT_SYAP=''", rendered)
        self.assertIn("TC_INTERNAL_SHARE_USE_DISK_ROOT=false", rendered)
        self.assertNotIn("TC_SHARE_USE_DISK_ROOT=", rendered)
        self.assertIn("TC_CONFIGURE_ID=12345678-1234-1234-1234-123456789012", rendered)

    def test_env_example_payload_dir_matches_default(self) -> None:
        values = parse_env_file(REPO_ROOT / ".env.example")
        self.assertEqual(values["TC_PAYLOAD_DIR_NAME"], DEFAULTS["TC_PAYLOAD_DIR_NAME"])

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

    def test_write_env_file_round_trips_mdns_device_model(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_MDNS_DEVICE_MODEL"] = "AirPortTimeCapsule"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertEqual(reparsed["TC_MDNS_DEVICE_MODEL"], "AirPortTimeCapsule")

    def test_write_env_file_round_trips_airport_syap(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_AIRPORT_SYAP"] = "119"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_file(path)
        self.assertEqual(reparsed["TC_AIRPORT_SYAP"], "119")

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
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": ".samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        rendered = render_env_text(values)
        self.assertIn(f"TC_SSH_OPTS={shlex.quote(DEFAULTS['TC_SSH_OPTS'])}", rendered)

    def test_extract_host_removes_user_prefix(self) -> None:
        self.assertEqual(extract_host("root@10.0.0.5"), "10.0.0.5")
        self.assertEqual(extract_host("10.0.0.5"), "10.0.0.5")

    def test_build_mdns_device_model_txt(self) -> None:
        self.assertEqual(build_mdns_device_model_txt("TimeCapsule"), "model=TimeCapsule")

    def test_build_adisk_share_txt(self) -> None:
        self.assertEqual(
            build_adisk_share_txt("Data"),
            "dk2=adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

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

    def test_validate_mdns_instance_name_allows_spaces(self) -> None:
        self.assertIsNone(validate_mdns_instance_name("Time Capsule Samba 4", "mDNS SMB instance name"))
        self.assertIsNone(validate_mdns_instance_name("Living Room Backup", "mDNS SMB instance name"))

    def test_validate_mdns_instance_name_rejects_bad_values(self) -> None:
        self.assertEqual(validate_mdns_instance_name("", "mDNS SMB instance name"), "mDNS SMB instance name cannot be blank.")
        self.assertEqual(validate_mdns_instance_name("time.capsule", "mDNS SMB instance name"), "mDNS SMB instance name must not contain dots.")
        self.assertEqual(validate_mdns_instance_name("a" * 64, "mDNS SMB instance name"), "mDNS SMB instance name must be 63 bytes or fewer.")

    def test_validate_mdns_host_label_accepts_dns_safe_values(self) -> None:
        self.assertIsNone(validate_mdns_host_label("timecapsulesamba4", "mDNS host label"))
        self.assertIsNone(validate_mdns_host_label("time-capsule-4", "mDNS host label"))

    def test_validate_mdns_host_label_rejects_spaces_and_bad_values(self) -> None:
        self.assertEqual(
            validate_mdns_host_label("Time Capsule", "mDNS host label"),
            "mDNS host label may contain only letters, numbers, and hyphens.",
        )
        self.assertEqual(validate_mdns_host_label("time.capsule", "mDNS host label"), "mDNS host label must not contain dots.")
        self.assertEqual(
            validate_mdns_host_label("10.0.1.99", "mDNS host label"),
            "mDNS host label must be a single DNS label, not an IP address.",
        )
        self.assertEqual(
            validate_mdns_host_label("fe80::1", "mDNS host label"),
            "mDNS host label must be a single DNS label, not an IP address.",
        )
        self.assertEqual(validate_mdns_host_label("-timecapsule", "mDNS host label"), "mDNS host label must not start or end with a hyphen.")
        self.assertEqual(validate_mdns_host_label("timecapsule-", "mDNS host label"), "mDNS host label must not start or end with a hyphen.")

    def test_validate_netbios_name_rejects_long_values(self) -> None:
        self.assertEqual(
            validate_netbios_name("A" * 16, "Samba NetBIOS name"),
            "Samba NetBIOS name must be 15 bytes or fewer.",
        )

    def test_validate_netbios_name_accepts_safe_values(self) -> None:
        self.assertIsNone(validate_netbios_name("TimeCapsule", "Samba NetBIOS name"))
        self.assertIsNone(validate_netbios_name("TC_4-Backup", "Samba NetBIOS name"))

    def test_validate_netbios_name_rejects_spaces_and_punctuation(self) -> None:
        self.assertEqual(
            validate_netbios_name("Time Capsule", "Samba NetBIOS name"),
            "Samba NetBIOS name may contain only letters, numbers, underscores, and hyphens.",
        )
        self.assertEqual(
            validate_netbios_name("Time.Capsule", "Samba NetBIOS name"),
            "Samba NetBIOS name may contain only letters, numbers, underscores, and hyphens.",
        )

    def test_validate_samba_user_accepts_safe_values(self) -> None:
        self.assertIsNone(validate_samba_user("admin", "Samba username"))
        self.assertIsNone(validate_samba_user("time.machine_user-1", "Samba username"))

    def test_validate_samba_user_rejects_bad_values(self) -> None:
        self.assertEqual(validate_samba_user("", "Samba username"), "Samba username cannot be blank.")
        self.assertEqual(validate_samba_user("time machine", "Samba username"), "Samba username must not contain whitespace.")
        self.assertEqual(
            validate_samba_user("bad:user", "Samba username"),
            "Samba username may contain only letters, numbers, dots, underscores, and hyphens.",
        )
        self.assertEqual(validate_samba_user("a" * 33, "Samba username"), "Samba username must be 32 bytes or fewer.")

    def test_validate_payload_dir_name_accepts_safe_values(self) -> None:
        self.assertIsNone(validate_payload_dir_name(".samba4", "Persistent payload directory name"))
        self.assertIsNone(validate_payload_dir_name("samba4", "Persistent payload directory name"))
        self.assertIsNone(validate_payload_dir_name("tc_samba-4.8", "Persistent payload directory name"))

    def test_validate_payload_dir_name_rejects_bad_values(self) -> None:
        self.assertEqual(validate_payload_dir_name("", "Persistent payload directory name"), "Persistent payload directory name cannot be blank.")
        self.assertEqual(validate_payload_dir_name(".", "Persistent payload directory name"), "Persistent payload directory name must not be . or ...")
        self.assertEqual(validate_payload_dir_name("..", "Persistent payload directory name"), "Persistent payload directory name must not be . or ...")
        self.assertEqual(validate_payload_dir_name("foo/bar", "Persistent payload directory name"), "Persistent payload directory name must be a single directory name, not a path.")
        self.assertEqual(validate_payload_dir_name("-samba4", "Persistent payload directory name"), "Persistent payload directory name must not start with a hyphen.")

    def test_validate_net_iface_accepts_safe_values(self) -> None:
        self.assertIsNone(validate_net_iface("bridge0", "Network interface on the device"))
        self.assertIsNone(validate_net_iface("bge0.100", "Network interface on the device"))

    def test_validate_net_iface_rejects_bad_values(self) -> None:
        self.assertEqual(validate_net_iface("", "Network interface on the device"), "Network interface on the device cannot be blank.")
        self.assertEqual(validate_net_iface("bridge 0", "Network interface on the device"), "Network interface on the device must not contain whitespace.")
        self.assertEqual(
            validate_net_iface("bridge0;reboot", "Network interface on the device"),
            "Network interface on the device may contain only letters, numbers, dots, underscores, colons, and hyphens.",
        )

    def test_validate_ssh_target_accepts_user_at_host_targets(self) -> None:
        self.assertIsNone(validate_ssh_target("root@10.0.0.2", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("root@timecapsule.local", "Device SSH target"))
        self.assertIsNone(validate_ssh_target("admin_user@wan.example.com", "Device SSH target"))

    def test_validate_ssh_target_rejects_bare_or_unsafe_targets(self) -> None:
        self.assertEqual(
            validate_ssh_target("10.0.0.2", "Device SSH target"),
            "Device SSH target must include a username, like root@192.168.1.101.",
        )
        self.assertEqual(validate_ssh_target("@10.0.0.2", "Device SSH target"), "Device SSH target must include a username before @.")
        self.assertEqual(validate_ssh_target("root@", "Device SSH target"), "Device SSH target must include a host after @.")
        self.assertEqual(validate_ssh_target("root user@10.0.0.2", "Device SSH target"), "Device SSH target must not contain whitespace.")
        self.assertEqual(
            validate_ssh_target("root;reboot@10.0.0.2", "Device SSH target"),
            "Device SSH target username may contain only letters, numbers, dots, underscores, and hyphens.",
        )

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
        self.assertEqual(errors[0].key, "TC_MDNS_HOST_LABEL")

    def test_validate_app_config_rejects_generic_device_model_when_syap_is_specific(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        values["TC_MDNS_DEVICE_MODEL"] = "TimeCapsule"
        config = AppConfig.from_values(values, file_values=values)
        errors = validate_app_config(config, profile="deploy")
        self.assertEqual(errors[0].key, "TC_MDNS_DEVICE_MODEL")
        self.assertEqual(errors[0].message,
                         'TC_MDNS_DEVICE_MODEL "TimeCapsule" must match the '
                         'configured syAP expected value "TimeCapsule8,119".')

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

    def test_app_config_require_raises_for_missing_value(self) -> None:
        config = AppConfig.from_values({"TC_HOST": ""})
        with self.assertRaises(ConfigError) as ctx:
            config.require("TC_HOST")
        self.assertNotIsInstance(ctx.exception, SystemExit)


if __name__ == "__main__":
    unittest.main()
