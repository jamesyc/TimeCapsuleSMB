from __future__ import annotations

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
    validate_dns_name,
    validate_mdns_device_model,
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
        rendered = render_env_text(values)
        self.assertIn("TC_PASSWORD=secret", rendered)
        self.assertIn("TC_MDNS_INSTANCE_NAME='Time Capsule Samba 4'", rendered)
        self.assertIn("TC_MDNS_DEVICE_MODEL=TimeCapsule", rendered)

    def test_write_env_file_round_trips_mdns_device_model(self) -> None:
        values = dict(DEFAULTS)
        values["TC_PASSWORD"] = "secret"
        values["TC_MDNS_DEVICE_MODEL"] = "AirPortTimeCapsule"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            write_env_file(path, values)
            reparsed = parse_env_values(path)
        self.assertEqual(reparsed["TC_MDNS_DEVICE_MODEL"], "AirPortTimeCapsule")

    def test_parse_env_value_falls_back_for_unbalanced_quotes(self) -> None:
        self.assertEqual(parse_env_value("'unterminated"), "unterminated")

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

    def test_validate_mdns_device_model_rejects_long_values(self) -> None:
        self.assertEqual(
            validate_mdns_device_model("a" * 250, "mDNS device model hint"),
            "mDNS device model hint must be 249 bytes or fewer.",
        )

    def test_validate_adisk_share_name_rejects_long_values(self) -> None:
        self.assertEqual(
            validate_adisk_share_name("a" * 193, "SMB share name"),
            "SMB share name must be 192 bytes or fewer.",
        )

    def test_app_config_require_raises_for_missing_value(self) -> None:
        config = AppConfig({"TC_HOST": ""})
        with self.assertRaises(SystemExit):
            config.require("TC_HOST")


if __name__ == "__main__":
    unittest.main()
