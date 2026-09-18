from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.build_wrapper_harness import BuildWrapperHarness


class DiscoveryBuildWrapperTests(unittest.TestCase):
    def test_netbsd7_builds_static_stripped_discoveryd(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, _, gcc_args, strip_args = helper.env_for(root, triple="arm--netbsdelf")
            env["DISCOVERY_STAGE"] = str(root / "discovery-stage")
            env["DISCOVERY_LOG"] = str(root / "discovery.log")

            result = helper.run_wrapper("discovery.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertIn(f"--sysroot={root / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn("-Wl,--gc-sections", args)
            self.assertIn("--strip-unneeded", strip_args.read_text())
            self.assertTrue((root / "discovery-stage" / "discoveryd.stripped").exists())
            self.assertIn("discovery.sources", Path(env["DISCOVERY_LOG"]).read_text())

    def test_netbsd4be_uses_big_endian_lane_without_sysroot(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, _, gcc_args, _ = helper.env_for(root, triple="armeb--netbsdelf")
            env["DISCOVERY_STAGE"] = str(root / "discovery-stage")
            env["DISCOVERY_LOG"] = str(root / "discovery.log")

            result = helper.run_wrapper("discoveryoldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertNotIn(f"--sysroot={root / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn("TRIPLE=armeb--netbsdelf", Path(env["DISCOVERY_LOG"]).read_text())
            self.assertIn("SDK_FAMILY=netbsd4", Path(env["DISCOVERY_LOG"]).read_text())


if __name__ == "__main__":
    unittest.main()
