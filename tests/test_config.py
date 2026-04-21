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
    AppConfig,
    build_adisk_share_txt,
    build_instance_fqdn,
    build_mdns_device_model_txt,
    DEFAULTS,
    extract_host,
    missing_required_keys,
    parse_env_value,
    parse_env_values,
    render_env_text,
    validate_adisk_share_name,
    validate_airport_syap,
    validate_dns_name,
    validate_config_values,
    validate_mdns_device_model,
    validate_mdns_host_label,
    validate_mdns_instance_name,
    validate_netbios_name,
    validate_net_iface,
    validate_payload_dir_name,
    validate_samba_user,
    validate_single_dns_label,
    write_env_file,
)


class ConfigTests(unittest.TestCase):
    def test_parse_env_values_applies_defaults_and_unquotes_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TC_HOST='root@10.0.0.5'\nTC_SHARE_NAME='Archive Share'\n")
            values = parse_env_values(path)
        self.assertEqual(values["TC_HOST"], "root@10.0.0.5")
        self.assertEqual(values["TC_SHARE_NAME"], "Archive Share")
        self.assertEqual(values["TC_MDNS_HOST_LABEL"], DEFAULTS["TC_MDNS_HOST_LABEL"])

    def test_missing_required_keys_detects_blank_values(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = ""
        missing = missing_required_keys(values)
        self.assertIn("TC_PASSWORD", missing)

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
        self.assertIn("TC_CONFIGURE_ID=12345678-1234-1234-1234-123456789012", rendered)

    def test_write_env_file_round_trips_mdns_device_model(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_MDNS_DEVICE_MODEL"] = "AirPortTimeCapsule"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_values(path)
        self.assertEqual(reparsed["TC_MDNS_DEVICE_MODEL"], "AirPortTimeCapsule")

    def test_write_env_file_round_trips_airport_syap(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_AIRPORT_SYAP"] = "119"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_values(path)
        self.assertEqual(reparsed["TC_AIRPORT_SYAP"], "119")

    def test_validate_airport_syap_accepts_known_codes(self) -> None:
        for value in ("106", "109", "113", "116", "119"):
            self.assertIsNone(validate_airport_syap(value, "Airport Utility syAP code"))

    def test_validate_airport_syap_rejects_invalid_values(self) -> None:
        self.assertIsNone(validate_airport_syap("119", "Airport Utility syAP code"))
        self.assertEqual(validate_airport_syap("999", "Airport Utility syAP code"), "The configured syAP is invalid.")
        self.assertEqual(validate_airport_syap("abc", "Airport Utility syAP code"), "Airport Utility syAP code must contain only digits.")
        self.assertEqual(validate_airport_syap("", "Airport Utility syAP code"), "Airport Utility syAP code cannot be blank.")

    def test_write_env_file_round_trips_configure_id(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_CONFIGURE_ID"] = "12345678-1234-1234-1234-123456789012"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_values(path)
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

    def test_parse_env_values_preserves_full_ssh_opts_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            ssh_opts = (
                "-o HostKeyAlgorithms=+ssh-rsa "
                "-o PubkeyAcceptedAlgorithms=+ssh-rsa "
                "-o KexAlgorithms=+diffie-hellman-group14-sha1 "
                "-o ProxyCommand=ssh\\ -4\\ -W\\ %h:%p\\ jump.example.com"
            )
            path.write_text(f"TC_SSH_OPTS='{ssh_opts}'\n")
            values = parse_env_values(path)
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
            reparsed = parse_env_values(path)
        self.assertEqual(reparsed["TC_SSH_OPTS"], values["TC_SSH_OPTS"])

    def test_render_env_text_falls_back_to_default_ssh_opts_when_missing(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.5",
            "TC_PASSWORD": "secret",
            "TC_NET_IFACE": "bridge0",
            "TC_SHARE_NAME": "Data",
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

    def test_validate_single_dns_label_rejects_dots(self) -> None:
        self.assertEqual(
            validate_single_dns_label("time.capsule", "mDNS host label"),
            "mDNS host label must not contain dots.",
        )

    def test_validate_single_dns_label_rejects_long_values(self) -> None:
        self.assertEqual(
            validate_single_dns_label("a" * 64, "mDNS SMB instance name"),
            "mDNS SMB instance name must be 63 bytes or fewer.",
        )

    def test_validate_dns_name_rejects_empty_label(self) -> None:
        self.assertEqual(
            validate_dns_name("_smb..local.", "service type"),
            "service type contains an empty label.",
        )

    def test_validate_dns_name_allows_trailing_dot(self) -> None:
        self.assertIsNone(validate_dns_name("_smb._tcp.local.", "service type"))

    def test_build_instance_fqdn_returns_none_when_too_long(self) -> None:
        self.assertIsNone(build_instance_fqdn("a" * 63, ("b" * 63 + ".") * 4))

    def test_build_mdns_device_model_txt(self) -> None:
        self.assertEqual(build_mdns_device_model_txt("TimeCapsule"), "model=TimeCapsule")

    def test_build_adisk_share_txt(self) -> None:
        self.assertEqual(
            build_adisk_share_txt("Data"),
            "dk2=adVF=0x1093,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
        )

    def test_validate_mdns_device_model_accepts_supported_values(self) -> None:
        for value in ("TimeCapsule", "TimeCapsule6,106", "TimeCapsule6,109", "TimeCapsule6,113", "TimeCapsule6,116", "TimeCapsule8,119"):
            self.assertIsNone(validate_mdns_device_model(value, "mDNS device model hint"))

    def test_validate_mdns_device_model_rejects_unsupported_values(self) -> None:
        self.assertEqual(
            validate_mdns_device_model("AirPortTimeCapsule", "mDNS device model hint"),
            "mDNS device model hint is not a supported Time Capsule model.",
        )
        self.assertEqual(
            validate_mdns_device_model("TimeCapsule7,117", "mDNS device model hint"),
            "mDNS device model hint is not a supported Time Capsule model.",
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
        self.assertIsNone(validate_mdns_host_label("10.0.1.99", "mDNS host label"))

    def test_validate_mdns_host_label_rejects_spaces_and_bad_values(self) -> None:
        self.assertEqual(
            validate_mdns_host_label("Time Capsule", "mDNS host label"),
            "mDNS host label may contain only letters, numbers, and hyphens.",
        )
        self.assertEqual(validate_mdns_host_label("time.capsule", "mDNS host label"), "mDNS host label must not contain dots.")
        self.assertEqual(validate_mdns_host_label("-timecapsule", "mDNS host label"), "mDNS host label must not start or end with a hyphen.")
        self.assertEqual(validate_mdns_host_label("timecapsule-", "mDNS host label"), "mDNS host label must not start or end with a hyphen.")

    def test_validate_adisk_share_name_rejects_long_values(self) -> None:
        self.assertEqual(
            validate_adisk_share_name("a" * 193, "SMB share name"),
            "SMB share name must be 192 bytes or fewer.",
        )

    def test_validate_adisk_share_name_accepts_spaces(self) -> None:
        self.assertIsNone(validate_adisk_share_name("Data", "SMB share name"))
        self.assertIsNone(validate_adisk_share_name("Time Machine Backups", "SMB share name"))

    def test_validate_adisk_share_name_rejects_unsafe_characters(self) -> None:
        for value in ("Bad/Share", "Bad\\Share", "Bad[Share]", "Bad,Share", "Bad=Share"):
            self.assertEqual(
                validate_adisk_share_name(value, "SMB share name"),
                "SMB share name contains a character that is not safe for Samba/adisk.",
            )

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
        self.assertIsNone(validate_net_iface("bridge0", "Network interface on the Time Capsule"))
        self.assertIsNone(validate_net_iface("bge0.100", "Network interface on the Time Capsule"))

    def test_validate_net_iface_rejects_bad_values(self) -> None:
        self.assertEqual(validate_net_iface("", "Network interface on the Time Capsule"), "Network interface on the Time Capsule cannot be blank.")
        self.assertEqual(validate_net_iface("bridge 0", "Network interface on the Time Capsule"), "Network interface on the Time Capsule must not contain whitespace.")
        self.assertEqual(
            validate_net_iface("bridge0;reboot", "Network interface on the Time Capsule"),
            "Network interface on the Time Capsule may contain only letters, numbers, dots, underscores, colons, and hyphens.",
        )

    def test_validate_config_values_uses_profiles(self) -> None:
        values = dict(DEFAULTS)
        values["TC_HOST"] = "root@10.0.0.2"
        values["TC_PASSWORD"] = "pw"
        values["TC_AIRPORT_SYAP"] = "119"
        self.assertEqual(validate_config_values(values, profile="deploy"), [])
        values["TC_MDNS_HOST_LABEL"] = "Time Capsule"
        errors = validate_config_values(values, profile="deploy")
        self.assertEqual(errors[0].key, "TC_MDNS_HOST_LABEL")

    def test_app_config_require_raises_for_missing_value(self) -> None:
        config = AppConfig({"TC_HOST": ""})
        with self.assertRaises(SystemExit):
            config.require("TC_HOST")


if __name__ == "__main__":
    unittest.main()
