from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.repair_xattrs import (  # noqa: E402
    MountedSmbShare,
    default_share_path_from_config,
    parse_mounted_smb_shares,
    should_skip_path,
)
from timecapsulesmb.core.config import AppConfig  # noqa: E402


class RepairXattrsDomainTests(unittest.TestCase):
    def test_parse_mounted_smb_shares_decodes_server_share_and_mountpoint(self) -> None:
        shares = parse_mounted_smb_shares(
            "//admin@timecapsule%20home.local/Data%20Disk on /Volumes/Data Disk (smbfs, nodev, nosuid)\n"
        )

        self.assertEqual(shares, [MountedSmbShare("timecapsule home.local", "Data Disk", Path("/Volumes/Data Disk"))])

    def test_default_share_path_prefers_exact_host_and_ignores_missing_mounts(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"})
        shares = [
            MountedSmbShare("192.168.1.111", "Data", Path("/Volumes/Missing")),
            MountedSmbShare("192.168.1.217", "Data", Path("/Volumes/Data")),
        ]

        self.assertEqual(
            default_share_path_from_config(
                config,
                shares=shares,
                path_exists_func=lambda path: path == Path("/Volumes/Data"),
            ),
            Path("/Volumes/Data"),
        )

    def test_default_share_path_raises_when_same_share_has_multiple_existing_nonmatching_mounts(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@192.168.1.217", "TC_SHARE_NAME": "Data"})
        shares = [
            MountedSmbShare("timecapsule-a.local", "Data", Path("/Volumes/Data")),
            MountedSmbShare("timecapsule-b.local", "Data", Path("/Volumes/Data-1")),
        ]

        with self.assertRaises(RuntimeError) as cm:
            default_share_path_from_config(config, shares=shares, path_exists_func=lambda _path: True)
        self.assertIn("multiple mounted SMB shares", str(cm.exception))

    def test_should_skip_path_always_skips_managed_payload_directory(self) -> None:
        root = Path("/Volumes/Data")

        self.assertTrue(
            should_skip_path(
                root / ".samba4" / "private" / "xattr.tdb",
                root,
                include_hidden=True,
                include_time_machine=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
