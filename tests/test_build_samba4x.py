from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class Samba4XBuildScriptTests(unittest.TestCase):
    def make_executable(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def make_file(self, path: Path, content: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def prepare_fake_toolchain(self, out: Path, triple: str) -> None:
        tools = out / "tools" / "bin"
        tools.mkdir(parents=True, exist_ok=True)
        self.make_executable(tools / "nbmake", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / "nbfile", "#!/bin/sh\nprintf '%s: fake ELF\\n' \"$1\"\n")
        self.make_executable(
            tools / f"{triple}-gcc",
            textwrap.dedent(
                """\
                #!/bin/sh
                out=
                while [ "$#" -gt 0 ]; do
                    if [ "$1" = "-o" ]; then
                        shift
                        out="$1"
                    fi
                    shift || break
                done
                if [ -n "$out" ]; then
                    mkdir -p "$(dirname "$out")"
                    printf 'fake object\\n' >"$out"
                fi
                exit 0
                """
            ),
        )
        self.make_executable(tools / f"{triple}-g++", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / f"{triple}-cpp", "#!/bin/sh\nexit 0\n")
        self.make_executable(
            tools / f"{triple}-ld",
            textwrap.dedent(
                """\
                #!/bin/sh
                if [ "${1:-}" = "--verbose" ]; then
                    printf '====\\nSECTIONS {\\n  SIZEOF_HEADERS;\\n}\\n====\\n'
                fi
                exit 0
                """
            ),
        )
        self.make_executable(
            tools / f"{triple}-objdump",
            textwrap.dedent(
                """\
                #!/bin/sh
                case "${1:-}" in
                    -h)
                        printf '  1 .note.netbsd.ident 00000000\\n'
                        printf '  2 .note.netbsd.pax 00000000\\n'
                        ;;
                    -p)
                        printf 'Program Header:\\n'
                        ;;
                esac
                exit 0
                """
            ),
        )
        for name in ("ar", "ranlib", "readelf", "strip"):
            self.make_executable(tools / f"{triple}-{name}", "#!/bin/sh\nexit 0\n")

    def prepare_fake_samba_source(self, src_dir: Path) -> None:
        self.make_executable(
            src_dir / "configure",
            textwrap.dedent(
                """\
                #!/bin/sh
                : > "$TEST_CONFIGURE_ARGS"
                for arg in "$@"; do
                    printf '%s\\n' "$arg" >> "$TEST_CONFIGURE_ARGS"
                done
                mkdir -p bin/c4che bin/default/include bin/default/source3/include bin/default/source4/include
                cat > bin/c4che/default.py <<'EOF'
                ENABLE_PIE = True
                LDFLAGS = []
                LINKFLAGS = []
                EOF
                for header in bin/default/include/config.h bin/default/source3/include/config.h bin/default/source4/include/config.h; do
                    cat > "$header" <<'EOF'
                /* #undef HAVE_IFACE_IFCONF */
                EOF
                done
                exit 0
                """
            ),
        )
        self.make_executable(
            src_dir / "buildtools" / "bin" / "waf",
            textwrap.dedent(
                """\
                import pathlib
                import sys

                if "build" in sys.argv:
                    smbd = pathlib.Path("bin/default/source3/smbd/smbd")
                    smbd.parent.mkdir(parents=True, exist_ok=True)
                    smbd.write_text("fake smbd\\n")
                sys.exit(0)
                """
            ),
        )

    def prepare_fake_netbsd_inputs(self, root: Path, *, lane: str) -> dict[str, Path]:
        out = root / f"out-{lane}"
        build_src = root / f"netbsd-src-{lane}"
        samba_src = root / f"samba-src-{lane}"
        samba_build = root / f"samba-build-{lane}"
        samba_stage = root / f"samba-stage-{lane}"
        obj = out / "obj"
        sysroot = obj / "destdir.evbarm"

        if lane == "netbsd4be":
            triple = "armeb--netbsdelf"
            gmp_arch = "armeb"
        elif lane == "netbsd4le":
            triple = "arm--netbsdelf"
            gmp_arch = "arm"
        else:
            triple = "arm--netbsdelf"
            gmp_arch = "earm"

        self.prepare_fake_toolchain(out, triple)
        self.prepare_fake_samba_source(samba_src)
        self.make_file(sysroot / "usr" / "include" / "zlib.h")
        self.make_file(sysroot / "usr" / "lib" / "libz.a")
        self.make_file(obj / "external" / "lgpl3" / "gmp" / "lib" / "libgmp" / "libgmp.a")
        self.make_file(build_src / "external" / "lgpl3" / "gmp" / "lib" / "libgmp" / "arch" / gmp_arch / "gmp.h")

        deps = samba_build / "deps"
        self.make_file(deps / ".stamp-nettle-3.10.1-system-gmp")
        self.make_file(deps / "lib" / "libnettle.a")
        self.make_file(deps / "lib" / "libhogweed.a")
        self.make_file(deps / ".stamp-libtasn1-4.20.0")
        self.make_file(deps / "lib" / "libtasn1.a")
        self.make_file(deps / ".stamp-gnutls-3.8.5-system-nettle-oaep-no-thread-local")
        self.make_file(deps / "lib" / "libgnutls.a")
        self.make_file(deps / "lib" / "pkgconfig" / "gnutls.pc", "Libs: -L${libdir} -lgnutls\n")

        return {
            "out": out,
            "build_src": build_src,
            "samba_src": samba_src,
            "samba_build": samba_build,
            "samba_stage": samba_stage,
        }

    def env_for_lane(self, root: Path, lane: str, capture: Path) -> dict[str, str]:
        paths = self.prepare_fake_netbsd_inputs(root, lane=lane)
        env = os.environ.copy()
        env.update(
            {
                "TC_ENV_FILE": "/dev/null",
                "PYTHON3": sys.executable,
                "TEST_CONFIGURE_ARGS": str(capture),
                "BUILD_SRC": str(paths["build_src"]),
                "BUILD_OUT": str(paths["out"]),
                "SAMBA4X_NETBSD7_SRC_DIR": str(paths["samba_src"]),
                "SAMBA4X_NETBSD7_WORK": str(root / "work-netbsd7"),
                "SAMBA4X_NETBSD7_BUILD": str(paths["samba_build"]),
                "SAMBA4X_NETBSD7_STAGE": str(paths["samba_stage"]),
                "SAMBA4X_NETBSD7_LOG": str(root / "samba4x-netbsd7.log"),
                "SAMBA4X_NETBSD4LE_SRC_DIR": str(paths["samba_src"]),
                "SAMBA4X_NETBSD4LE_WORK": str(root / "work-netbsd4le"),
                "SAMBA4X_NETBSD4LE_BUILD": str(paths["samba_build"]),
                "SAMBA4X_NETBSD4LE_STAGE": str(paths["samba_stage"]),
                "SAMBA4X_NETBSD4LE_LOG": str(root / "samba4x-netbsd4le.log"),
                "SAMBA4X_NETBSD4BE_SRC_DIR": str(paths["samba_src"]),
                "SAMBA4X_NETBSD4BE_WORK": str(root / "work-netbsd4be"),
                "SAMBA4X_NETBSD4BE_BUILD": str(paths["samba_build"]),
                "SAMBA4X_NETBSD4BE_STAGE": str(paths["samba_stage"]),
                "SAMBA4X_NETBSD4BE_LOG": str(root / "samba4x-netbsd4be.log"),
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

    def configure_args(self, capture: Path) -> list[str]:
        return capture.read_text().splitlines()

    def cross_answer_arg(self, args: list[str]) -> str:
        matches = [arg for arg in args if arg.startswith("--cross-answers=")]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_default_build_uses_cross_answers_without_cross_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env = self.env_for_lane(root, "netbsd7", capture)

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.configure_args(capture)
            self.assertIn("--cross-compile", args)
            self.assertNotIn("--cross-execute=", "\n".join(args))
            cross_answers = self.cross_answer_arg(args)
            self.assertTrue(cross_answers.endswith("/samba4x-4.24.1-netbsd7.answers"))

    def test_refresh_mode_keeps_cross_answers_and_adds_cross_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env = self.env_for_lane(root, "netbsd7", capture)
            env["SAMBA4X_REFRESH_CROSS_ANSWERS"] = "1"

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.configure_args(capture)
            self.cross_answer_arg(args)
            self.assertTrue(any(arg.startswith("--cross-execute=") for arg in args))

    def test_lane_wrappers_select_their_default_cross_answer_files(self) -> None:
        cases = (
            ("samba4x.sh", "netbsd7", "samba4x-4.24.1-netbsd7.answers"),
            ("samba4xoldle.sh", "netbsd4le", "samba4x-4.24.1-netbsd4le.answers"),
            ("samba4xoldbe.sh", "netbsd4be", "samba4x-4.24.1-netbsd4be.answers"),
        )
        for wrapper, lane, expected in cases:
            with self.subTest(wrapper=wrapper):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    capture = root / "configure-args.txt"
                    env = self.env_for_lane(root, lane, capture)

                    result = self.run_wrapper(wrapper, env)

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    cross_answers = self.cross_answer_arg(self.configure_args(capture))
                    self.assertTrue(cross_answers.endswith(f"/{expected}"))

    def test_missing_cross_answers_fail_before_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env = self.env_for_lane(root, "netbsd7", capture)
            env["SAMBA4X_CROSS_ANSWERS"] = str(root / "missing.answers")

            result = self.run_wrapper("samba4x.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing Samba 4.x cross-answers file", Path(env["SAMBA4X_NETBSD7_LOG"]).read_text())
            self.assertFalse(capture.exists())

    def test_unknown_cross_answers_fail_before_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            answers = root / "unknown.answers"
            answers.write_text("Checking target behavior: UNKNOWN\n")
            env = self.env_for_lane(root, "netbsd7", capture)
            env["SAMBA4X_CROSS_ANSWERS"] = str(answers)

            result = self.run_wrapper("samba4x.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("contains UNKNOWN entries", Path(env["SAMBA4X_NETBSD7_LOG"]).read_text())
            self.assertFalse(capture.exists())


if __name__ == "__main__":
    unittest.main()
