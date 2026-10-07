from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.build_wrapper_harness import make_fake_elf_tools
from tests.executables import write_executable


REPO_ROOT = Path(__file__).resolve().parent.parent


class RsyncBuildScriptTests(unittest.TestCase):
    def make_executable(self, path: Path, text: str) -> None:
        write_executable(path, text)

    def make_file(self, path: Path, content: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def prepare_fake_toolchain(self, out: Path, triple: str) -> None:
        tools = out / "tools" / "bin"
        tools.mkdir(parents=True, exist_ok=True)
        self.make_executable(tools / "nbmake", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / "nbfile", "#!/bin/sh\nprintf '%s: fake ELF\\n' \"$1\"\n")
        self.make_executable(tools / f"{triple}-gcc", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / f"{triple}-ar", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / f"{triple}-ranlib", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / f"{triple}-strip", "#!/bin/sh\nexit 0\n")
        make_fake_elf_tools(tools, triple)

    def prepare_fake_rsync_source(self, src_dir: Path) -> None:
        self.make_executable(
            src_dir / "configure",
            textwrap.dedent(
                """\
                #!/bin/sh
                : > "$TEST_CONFIGURE_ARGS"
                : > "$TEST_CONFIGURE_ENV"
                for arg in "$@"; do
                    printf '%s\\n' "$arg" >> "$TEST_CONFIGURE_ARGS"
                done
                {
                    printf 'CC=%s\\n' "$CC"
                    printf 'CFLAGS=%s\\n' "$CFLAGS"
                    printf 'CPPFLAGS=%s\\n' "$CPPFLAGS"
                    printf 'LDFLAGS=%s\\n' "$LDFLAGS"
                    printf 'AR=%s\\n' "$AR"
                    printf 'RANLIB=%s\\n' "$RANLIB"
                } >> "$TEST_CONFIGURE_ENV"
                exit 0
                """
            ),
        )

    def prepare_fake_make(self, path: Path) -> None:
        self.make_executable(
            path,
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$@" > "$TEST_MAKE_ARGS"
                ls > "$TEST_MAKE_ARGS.saw"
                printf 'fake rsync\\n' > rsync
                exit 0
                """
            ),
        )

    def prepare_fake_inputs(self, root: Path, *, lane: str) -> dict[str, Path]:
        out = root / f"out-{lane}"
        src = root / f"rsync-src-{lane}"
        build = root / f"rsync-build-{lane}"
        stage = root / f"rsync-stage-{lane}"
        sysroot = out / "obj" / "destdir.evbarm"

        triple = "armeb--netbsdelf" if lane == "netbsd4be" else "arm--netbsdelf"
        self.prepare_fake_toolchain(out, triple)
        self.prepare_fake_rsync_source(src)
        self.make_file(sysroot / "usr" / "include" / "stdio.h")
        self.make_file(sysroot / "usr" / "lib" / "libc.a")
        self.make_file(sysroot / "lib" / "libc.a")

        return {"out": out, "src": src, "build": build, "stage": stage}

    def env_for_lane(self, root: Path, lane: str, capture: Path, env_capture: Path) -> dict[str, str]:
        paths = self.prepare_fake_inputs(root, lane=lane)
        fake_make = root / "fake-gmake"
        self.prepare_fake_make(fake_make)
        env = os.environ.copy()
        env.update(
            {
                "TC_ENV_FILE": "/dev/null",
                "TEST_CONFIGURE_ARGS": str(capture),
                "TEST_CONFIGURE_ENV": str(env_capture),
                "TEST_MAKE_ARGS": str(root / "make-args.txt"),
                "BUILD_OUT": str(paths["out"]),
                "RSYNC_MAKE": str(fake_make),
                "RSYNC_NETBSD7_SRC_DIR": str(paths["src"]),
                "RSYNC_NETBSD7_WORK": str(root / "work-netbsd7"),
                "RSYNC_NETBSD7_BUILD": str(paths["build"]),
                "RSYNC_NETBSD7_STAGE": str(paths["stage"]),
                "RSYNC_NETBSD7_LOG": str(root / "rsync-netbsd7.log"),
                "RSYNC_NETBSD4LE_SRC_DIR": str(paths["src"]),
                "RSYNC_NETBSD4LE_WORK": str(root / "work-netbsd4le"),
                "RSYNC_NETBSD4LE_BUILD": str(paths["build"]),
                "RSYNC_NETBSD4LE_STAGE": str(paths["stage"]),
                "RSYNC_NETBSD4LE_LOG": str(root / "rsync-netbsd4le.log"),
                "RSYNC_NETBSD4BE_SRC_DIR": str(paths["src"]),
                "RSYNC_NETBSD4BE_WORK": str(root / "work-netbsd4be"),
                "RSYNC_NETBSD4BE_BUILD": str(paths["build"]),
                "RSYNC_NETBSD4BE_STAGE": str(paths["stage"]),
                "RSYNC_NETBSD4BE_LOG": str(root / "rsync-netbsd4be.log"),
            }
        )
        return env

    def run_wrapper(self, wrapper: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(REPO_ROOT / "build" / wrapper)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )

    def test_default_build_configures_static_minimal_rsync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env_capture = root / "configure-env.txt"
            env = self.env_for_lane(root, "netbsd7", capture, env_capture)

            result = self.run_wrapper("rsync.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = capture.read_text().splitlines()
            self.assertIn("--host=armv4-unknown-netbsd7.2", args)
            self.assertIn("--with-included-zlib", args)
            self.assertIn("--with-included-popt", args)
            self.assertIn("--disable-openssl", args)
            self.assertIn("--disable-xattr-support", args)
            self.assertIn("--disable-iconv-open", args)
            self.assertIn("--disable-locale", args)
            configure_env = env_capture.read_text()
            self.assertIn("--sysroot=", configure_env)
            self.assertIn("LDFLAGS=-static", configure_env)
            self.assertTrue((Path(env["RSYNC_NETBSD7_STAGE"]) / "rsync.stripped").exists())

    def test_lane_wrappers_select_expected_hosts_and_abis(self) -> None:
        cases = (
            ("rsync.sh", "netbsd7", "--host=armv4-unknown-netbsd7.2", "NETBSD4_ABI=le"),
            ("rsyncoldle.sh", "netbsd4le", "--host=armv4-unknown-netbsd4.0", "NETBSD4_ABI=le"),
            ("rsyncoldbe.sh", "netbsd4be", "--host=armeb-unknown-netbsd4.0", "NETBSD4_ABI=be"),
        )
        for wrapper, lane, expected_host, expected_abi_log in cases:
            with self.subTest(wrapper=wrapper):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    capture = root / "configure-args.txt"
                    env_capture = root / "configure-env.txt"
                    env = self.env_for_lane(root, lane, capture, env_capture)

                    result = self.run_wrapper(wrapper, env)

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(expected_host, capture.read_text().splitlines())
                    log_path = Path(env["RSYNC_NETBSD7_LOG" if lane == "netbsd7" else "RSYNC_NETBSD4BE_LOG" if lane == "netbsd4be" else "RSYNC_NETBSD4LE_LOG"])
                    self.assertIn(expected_abi_log, log_path.read_text())

    def test_data_faultahead_check_gates_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.env_for_lane(root, "netbsd7", root / "configure-args.txt", root / "configure-env.txt")
            stage = Path(env["RSYNC_NETBSD7_STAGE"])
            log = Path(env["RSYNC_NETBSD7_LOG"])

            result = self.run_wrapper("rsync.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("rsync: tc_disable_data_faultahead turns off fault-ahead", log.read_text())

            # A binary without patch 0003's madvise call is never staged.
            for staged in (stage / "bin" / "rsync", stage / "rsync.stripped"):
                staged.unlink()
            no_call = root / "no-madvise.txt"
            no_call.write_text("")
            env["TEST_OBJDUMP_DISASM"] = str(no_call)

            result = self.run_wrapper("rsync.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tc_disable_data_faultahead does not call madvise", log.read_text())
            self.assertFalse((stage / "bin" / "rsync").exists())
            self.assertFalse((stage / "rsync.stripped").exists())

    def test_fork_repair_goes_only_on_the_netbsd6_final_link(self) -> None:
        # Patch 0070's object and wrappers join make's link on NetBSD 6 only;
        # configure's probes never see them (a probe calling fork would fail).
        for wrapper, lane in (("rsync.sh", "netbsd7"), ("rsyncoldle.sh", "netbsd4le"), ("rsyncoldbe.sh", "netbsd4be")):
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env_capture = root / "configure-env.txt"
                env = self.env_for_lane(root, lane, root / "configure-args.txt", env_capture)

                result = self.run_wrapper(wrapper, env)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("--wrap=", env_capture.read_text())
                make_args = (root / "make-args.txt").read_text()
                link = [arg for arg in make_args.splitlines() if arg.startswith("LDFLAGS=")]
                if lane == "netbsd7":
                    self.assertEqual(len(link), 1, make_args)
                    for name in ("fork", "_fork", "mmap", "_mmap", "munmap", "mremap", "mprotect"):
                        self.assertIn(f"-Wl,--wrap={name}", link[0])
                    self.assertTrue(link[0].endswith("/tc_fork_repair.o"), link[0])
                else:
                    self.assertEqual(link, [])

    def test_every_build_starts_from_an_empty_build_directory(self) -> None:
        # make keeps an object whose source did not change, so objects compiled
        # with other flags were linked into the new rsync, and an old binary
        # was not relinked (the NetBSD 6 lane's fork repair once went missing).
        for wrapper, lane in (("rsync.sh", "netbsd7"), ("rsyncoldle.sh", "netbsd4le")):
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.env_for_lane(root, lane, root / "configure-args.txt", root / "configure-env.txt")
                build = Path(env[f"RSYNC_{lane.upper()}_BUILD"])
                build.mkdir(parents=True)
                (build / "rsync").write_text("stale rsync\n")
                (build / "flist.o").write_text("object compiled with other flags\n")

                result = self.run_wrapper(wrapper, env)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                seen = (root / "make-args.txt.saw").read_text().split()
                self.assertNotIn("rsync", seen)
                self.assertNotIn("flist.o", seen)

    def test_a_build_directory_holding_the_source_or_stage_is_not_emptied(self) -> None:
        for case in ("source", "inside source", "parent of source", "stage"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                capture = root / "configure-args.txt"
                env = self.env_for_lane(root, "netbsd7", capture, root / "configure-env.txt")
                src = Path(env["RSYNC_NETBSD7_SRC_DIR"])
                env["RSYNC_NETBSD7_BUILD"] = str({"source": src, "inside source": src / "build",
                                                  "parent of source": src.parent,
                                                  "stage": Path(env["RSYNC_NETBSD7_STAGE"])}[case])
                Path(env["RSYNC_NETBSD7_STAGE"]).mkdir(parents=True, exist_ok=True)

                result = self.run_wrapper("rsync.sh", env)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to empty RSYNC_BUILD", result.stdout)
                self.assertTrue((src / "configure").is_file())
                self.assertTrue(Path(env["RSYNC_NETBSD7_STAGE"]).is_dir())
                self.assertFalse(capture.exists())

    def test_fork_repair_check_gates_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.env_for_lane(root, "netbsd7", root / "configure-args.txt", root / "configure-env.txt")
            unwrapped = root / "nm.txt"
            unwrapped.write_text("0009248c T _fork\n0009248c W fork\n")
            env["TEST_NM_SYMBOLS"] = str(unwrapped)

            result = self.run_wrapper("rsync.sh", env)

            self.assertNotEqual(result.returncode, 0)
            log = Path(env["RSYNC_NETBSD7_LOG"]).read_text()
            self.assertIn("links libc's _fork without the fork repair's wrapper", log)
            self.assertFalse((Path(env["RSYNC_NETBSD7_STAGE"]) / "rsync.stripped").exists())

    def test_missing_source_fails_before_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env_capture = root / "configure-env.txt"
            env = self.env_for_lane(root, "netbsd7", capture, env_capture)
            Path(env["RSYNC_NETBSD7_SRC_DIR"], "configure").unlink()

            result = self.run_wrapper("rsync.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing rsync source tree", result.stdout + result.stderr)
            self.assertFalse(capture.exists())


if __name__ == "__main__":
    unittest.main()
