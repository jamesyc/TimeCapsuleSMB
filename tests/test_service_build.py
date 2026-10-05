from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.build_wrapper_harness import BuildWrapperHarness


class ServiceBuildWrapperTests(unittest.TestCase):
    def test_netbsd7_builds_static_stripped_unified_service(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, log, gcc_args, strip_args = helper.env_for(root, triple="arm--netbsdelf")

            result = helper.run_wrapper("service.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertIn(f"--sysroot={root / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn("-Wl,--gc-sections", args)
            self.assertIn("--strip-unneeded", strip_args.read_text())
            self.assertTrue((root / "stage" / "service.stripped").exists())
            self.assertIn("service.sources", log.read_text())

    # One test per lane wrapper, so the parallel runner spreads the builds.
    def test_data_faultahead_check_gates_stripping_netbsd6(self) -> None:
        self.check_data_faultahead_check_gates_stripping("service.sh")

    def test_data_faultahead_check_gates_stripping_netbsd4le(self) -> None:
        self.check_data_faultahead_check_gates_stripping("serviceoldle.sh")

    def test_data_faultahead_check_gates_stripping_netbsd4be(self) -> None:
        self.check_data_faultahead_check_gates_stripping("serviceoldbe.sh")

    def check_data_faultahead_check_gates_stripping(self, *wrappers: str) -> None:
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper = BuildWrapperHarness()
                env, log, _, _ = helper.env_for(root, triple="armeb--netbsdelf" if "be" in wrapper else "arm--netbsdelf")
                objdump_args = root / "objdump.args"
                env["TEST_OBJDUMP_ARGS"] = str(objdump_args)

                result = helper.run_wrapper(wrapper, env)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn("service: disable_data_faultahead turns off fault-ahead", log.read_text())
                # The unstripped image is checked, before strip drops the symbols.
                self.assertEqual(objdump_args.read_text().splitlines()[-1], str(root / "stage" / "service"))

                # An image without entry.c's madvise call is never stripped for packaging.
                (root / "stage" / "service.stripped").unlink()
                no_call = root / "no-madvise.txt"
                no_call.write_text("")
                env["TEST_OBJDUMP_DISASM"] = str(no_call)

                result = helper.run_wrapper(wrapper, env)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("disable_data_faultahead does not call madvise", log.read_text())
                self.assertFalse((root / "stage" / "service.stripped").exists())

    def test_fork_repair_links_only_into_the_netbsd6_service(self) -> None:
        # Patch 0070's overlay file is in service.sources for every lane; only
        # NetBSD 6 defines TC_FORK_REPAIR and wraps libc's fork and mmap family.
        for wrapper, triple, netbsd6 in (("service.sh", "arm--netbsdelf", True),
                                         ("serviceoldle.sh", "arm--netbsdelf", False),
                                         ("serviceoldbe.sh", "armeb--netbsdelf", False)):
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                helper = BuildWrapperHarness()
                env, log, gcc_args, _ = helper.env_for(root, triple=triple)

                result = helper.run_wrapper(wrapper, env)

                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                args = gcc_args.read_text().splitlines()
                self.assertTrue(any(arg.endswith("overlay/lib/replace/tc_fork_repair.c") for arg in args))
                self.assertEqual("-DTC_FORK_REPAIR=1" in args, netbsd6)
                for name in ("fork", "_fork", "mmap", "_mmap", "munmap", "mremap", "mprotect"):
                    self.assertEqual(f"-Wl,--wrap={name}" in args, netbsd6, name)
                self.assertEqual("go through the fork repair" in log.read_text(), netbsd6)

    def test_fork_repair_check_gates_stripping_netbsd6(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, log, _, _ = helper.env_for(root, triple="arm--netbsdelf")
            unwrapped = root / "nm.txt"
            unwrapped.write_text("0009248c T _fork\n0009248c W fork\n")
            env["TEST_NM_SYMBOLS"] = str(unwrapped)

            result = helper.run_wrapper("service.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("links libc's _fork without the fork repair's wrapper", log.read_text())
            self.assertFalse((root / "stage" / "service.stripped").exists())

    def test_netbsd4le_uses_little_endian_lane_without_sysroot(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, log, gcc_args, _ = helper.env_for(root, triple="arm--netbsdelf")

            result = helper.run_wrapper("serviceoldle.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertNotIn(f"--sysroot={root / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn('-DTC_TELEMETRY_LANE="4le"', args)
            self.assertIn("SDK_FAMILY=netbsd4", log.read_text())

    def test_netbsd4be_uses_big_endian_lane_without_sysroot(self) -> None:
        helper = BuildWrapperHarness()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env, log, gcc_args, _ = helper.env_for(root, triple="armeb--netbsdelf")

            result = helper.run_wrapper("serviceoldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertNotIn(f"--sysroot={root / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn('-DTC_TELEMETRY_LANE="4be"', args)
            self.assertIn("TRIPLE=armeb--netbsdelf", log.read_text())


if __name__ == "__main__":
    unittest.main()
