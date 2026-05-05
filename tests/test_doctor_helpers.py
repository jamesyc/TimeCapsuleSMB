from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.checks.bonjour import BonjourServiceTarget  # noqa: E402
from timecapsulesmb.checks.smb_config import (  # noqa: E402
    parse_active_netbios_name,
    parse_active_share_names,
    parse_xattr_tdb_paths,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers  # noqa: E402
from timecapsulesmb.core.config import AppConfig  # noqa: E402


class DoctorHelperTests(unittest.TestCase):
    def test_parse_xattr_tdb_paths_ignores_comments_and_preserves_multiple_paths(self) -> None:
        smb_conf = """
        [global]
          # xattr_tdb:file = /mnt/Memory/bad.tdb
          ; xattr_tdb:file = /mnt/Memory/also-bad.tdb
          xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb
          XATTR_TDB:FILE=/Volumes/dk3/.samba4/private/xattr.tdb
        """

        self.assertEqual(
            parse_xattr_tdb_paths(smb_conf),
            [
                "/Volumes/dk2/.samba4/private/xattr.tdb",
                "/Volumes/dk3/.samba4/private/xattr.tdb",
            ],
        )

    def test_parse_active_netbios_name_returns_first_non_comment_value_case_insensitively(self) -> None:
        smb_conf = """
        [global]
          ; netbios name = Ignored
          NETBIOS NAME = KitchenCapsule
          netbios name = Other
        """

        self.assertEqual(parse_active_netbios_name(smb_conf), "KitchenCapsule")

    def test_parse_active_share_names_skips_global_and_empty_sections(self) -> None:
        smb_conf = """
        [global]
        [Data]
        [ Time Machine ]
        []
        [GLOBAL]
        """

        self.assertEqual(parse_active_share_names(smb_conf), ["Data", "Time Machine"])

    def test_doctor_smb_servers_appends_local_only_for_single_label_hostname(self) -> None:
        base_values = {"TC_HOST": "root@10.0.1.99"}
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values({**base_values, "TC_MDNS_HOST_LABEL": "timecapsulesamba4"}), None),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values({**base_values, "TC_MDNS_HOST_LABEL": "timecapsulesamba4.local"}), None),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values({**base_values, "TC_MDNS_HOST_LABEL": "10.0.1.99"}), None),
            ["10.0.1.99"],
        )

    def test_doctor_smb_servers_orders_configured_bonjour_then_ssh_host_and_deduplicates(self) -> None:
        values = {
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_HOST": "root@10.0.1.99",
        }
        target = BonjourServiceTarget("Time Capsule Samba 4", "timecapsulesamba4.local", 445)

        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(values), target),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )


if __name__ == "__main__":
    unittest.main()
