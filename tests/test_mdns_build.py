from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class MdnsBuildWrapperTests(unittest.TestCase):
    def make_executable(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def prepare_fake_toolchain(self, out: Path, triple: str) -> None:
        tools = out / "tools" / "bin"
        sysroot = out / "obj" / "destdir.evbarm"
        sysroot.mkdir(parents=True, exist_ok=True)
        self.make_executable(tools / "nbmake", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / "nbfile", "#!/bin/sh\nprintf '%s: fake ELF\\n' \"$1\"\n")
        self.make_executable(
            tools / f"{triple}-gcc",
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$@" > "$TEST_GCC_ARGS"
                out=
                while [ "$#" -gt 0 ]; do
                    if [ "$1" = "-o" ]; then
                        shift
                        out="$1"
                    fi
                    shift || break
                done
                mkdir -p "$(dirname "$out")"
                printf 'fake mdns\\n' > "$out"
                """
            ),
        )
        self.make_executable(
            tools / f"{triple}-strip",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TEST_STRIP_ARGS\"\n",
        )
        self.make_executable(tools / f"{triple}-objdump", "#!/bin/sh\nprintf 'Program Header:\\n'\n")

    def env_for(self, root: Path, *, triple: str) -> tuple[dict[str, str], Path, Path, Path]:
        out = root / "out"
        stage = root / "stage"
        log = root / "mdns.log"
        gcc_args = root / "gcc.args"
        strip_args = root / "strip.args"
        self.prepare_fake_toolchain(out, triple)
        env = os.environ.copy()
        env.update({
            "TC_ENV_FILE": "/dev/null",
            "BUILD_OUT": str(out),
            "BUILD_SRC": str(root / "src"),
            "MDNS_STAGE": str(stage),
            "MDNS_LOG": str(log),
            "TEST_GCC_ARGS": str(gcc_args),
            "TEST_STRIP_ARGS": str(strip_args),
        })
        return env, log, gcc_args, strip_args

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

    def test_netbsd7_mdns_build_uses_sysroot_static_gc_and_strips_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, log, gcc_args, strip_args = self.env_for(Path(tmp), triple="arm--netbsdelf")

            result = self.run_wrapper("mdns.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertIn(f"--sysroot={Path(tmp) / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn("-Wl,--gc-sections", args)
            self.assertIn("--strip-unneeded", strip_args.read_text())
            self.assertTrue((Path(tmp) / "stage" / "mdns-advertiser.stripped").exists())
            self.assertIn("SDK_FAMILY=netbsd7", log.read_text())

    def test_netbsd4be_mdns_wrapper_uses_big_endian_lane_without_sysroot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, log, gcc_args, _strip_args = self.env_for(Path(tmp), triple="armeb--netbsdelf")

            result = self.run_wrapper("mdnsoldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = gcc_args.read_text().splitlines()
            self.assertNotIn(f"--sysroot={Path(tmp) / 'out' / 'obj' / 'destdir.evbarm'}", args)
            self.assertIn(f"-B{Path(tmp) / 'out' / 'obj' / 'destdir.evbarm' / 'usr' / 'lib'}", args)
            self.assertIn("TRIPLE=armeb--netbsdelf", log.read_text())
            self.assertIn("SDK_FAMILY=netbsd4", log.read_text())

    def test_mdns_build_fails_before_mutation_when_toolchain_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env.update({
                "TC_ENV_FILE": "/dev/null",
                "BUILD_OUT": str(root / "missing-out"),
                "BUILD_SRC": str(root / "src"),
                "MDNS_STAGE": str(root / "stage"),
                "MDNS_LOG": str(root / "mdns.log"),
            })

            result = self.run_wrapper("mdns.sh", env)

            self.assertEqual(result.returncode, 1)
            self.assertIn("Unable to find cross compiler", result.stderr)
            self.assertFalse((root / "stage").exists())
            self.assertFalse((root / "mdns.log").exists())


if __name__ == "__main__":
    unittest.main()
