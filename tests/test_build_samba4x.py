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

    def make_fake_cross_execute(self, path: Path) -> None:
        self.make_executable(
            path,
            textwrap.dedent(
                """\
                #!/bin/sh
                if [ -n "${TEST_CROSS_EXEC_ARGS:-}" ]; then
                    printf '%s\\n' "$@" >> "$TEST_CROSS_EXEC_ARGS"
                fi
                exit "${TEST_CROSS_EXEC_RC:-0}"
                """
            ),
        )

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
                cross_answers=
                for arg in "$@"; do
                    printf '%s\\n' "$arg" >> "$TEST_CONFIGURE_ARGS"
                    case "$arg" in
                        --cross-answers=*)
                            cross_answers="${arg#--cross-answers=}"
                            ;;
                    esac
                done
                if [ -n "${TEST_SEED_CAPTURE:-}" ] && [ -n "$cross_answers" ]; then
                    cp "$cross_answers" "$TEST_SEED_CAPTURE"
                fi
                if [ "${TEST_CONFIGURE_WRITES_ANSWERS:-0}" = "1" ] && [ -n "$cross_answers" ]; then
                    printf '%s: %s\\n' \
                        'Checking whether the realpath function allows a NULL argument' \
                        "${TEST_REALPATH_ANSWER:-OK}" >> "$cross_answers"
                    if [ -n "${TEST_DUPLICATE_REALPATH_ANSWER:-}" ]; then
                        printf '%s: %s\\n' \
                            'Checking whether the realpath function allows a NULL argument' \
                            "$TEST_DUPLICATE_REALPATH_ANSWER" >> "$cross_answers"
                    fi
                    if [ -n "${TEST_EXTRA_GENERATED_ANSWER:-}" ]; then
                        printf '%s\\n' "$TEST_EXTRA_GENERATED_ANSWER" >> "$cross_answers"
                    fi
                fi
                mkdir -p bin/c4che
                cat > bin/c4che/default.py <<'EOF'
                ENABLE_PIE = True
                LDFLAGS = []
                LINKFLAGS = []
                EOF
                if [ "${TEST_CONFIGURE_NO_HEADERS:-0}" != "1" ]; then
                    mkdir -p bin/default/include bin/default/source3/include bin/default/source4/include
                    for header in bin/default/include/config.h bin/default/source3/include/config.h bin/default/source4/include/config.h; do
                        cat > "$header" <<EOF
                /* #undef HAVE_IFACE_IFCONF */
                ${TEST_CONFIGURE_DEFINE:-}
                EOF
                    done
                fi
                exit 0
                """
            ),
        )
        self.make_executable(
            src_dir / "buildtools" / "bin" / "waf",
            textwrap.dedent(
                """\
                import os
                import pathlib
                import sys

                if "build" in sys.argv:
                    targets = next(
                        (arg.split("=", 1)[1] for arg in sys.argv
                         if arg.startswith("--targets=")),
                        "",
                    ).split(",")
                    capture = os.environ.get("TEST_WAF_TARGETS")
                    if "pthreadpool_tevent_sync_test" in targets:
                        test_binary = pathlib.Path(
                            "bin/default/lib/pthreadpool/"
                            "pthreadpool_tevent_sync_test"
                        )
                        test_binary.parent.mkdir(parents=True, exist_ok=True)
                        test_binary.write_text("fake pthreadpool test\\n")
                        test_binary.chmod(0o755)
                        if capture:
                            with pathlib.Path(capture).open("a") as stream:
                                stream.write("pthreadpool_tevent_sync_test\\n")
                    if "smbd/smbd" in targets:
                        smbd = pathlib.Path("bin/default/source3/smbd/smbd")
                        smbd.parent.mkdir(parents=True, exist_ok=True)
                        smbd.write_text("fake smbd\\n")
                        if capture:
                            with pathlib.Path(capture).open("a") as stream:
                                stream.write("smbd/smbd\\n")
                        if os.environ.get("TEST_SKIP_MAP", "0") != "1":
                            map_path = pathlib.Path(os.environ["MAP_FILE"])
                            map_path.parent.mkdir(parents=True, exist_ok=True)
                            map_path.write_text(
                                os.environ.get(
                                    "TEST_MAP_CONTENT",
                                    "OUTPUT(bin/default/source3/smbd/smbd elf32-littlearm)\\n",
                                )
                            )
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

    def cross_execute_args(self, args: list[str]) -> list[str]:
        return [arg for arg in args if arg.startswith("--cross-execute=")]

    def test_default_build_uses_cross_answers_without_cross_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            cross_exec_capture = root / "cross-exec-args.txt"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd7", capture)
            env["SAMBA4X_CROSS_EXECUTE"] = str(cross_exec)
            env["TEST_CROSS_EXEC_ARGS"] = str(cross_exec_capture)

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.configure_args(capture)
            self.assertIn("--cross-compile", args)
            self.assertIn("--disable-pthread", args)
            self.assertIn("--disable-pthreadpool", args)
            self.assertIn("--disable-tdb-mutex-locking", args)
            self.assertIn(
                "--with-static-modules="
                "vfs_catia,vfs_fruit,vfs_streams_xattr,vfs_xattr_tdb,vfs_acl_xattr,vfs_aio_fork",
                args,
            )
            self.assertEqual(self.cross_execute_args(args), [])
            cross_answers = self.cross_answer_arg(args)
            self.assertTrue(cross_answers.endswith("/samba4x-4.24.3-netbsd7.answers"))
            self.assertFalse(cross_exec_capture.exists())

    def test_generation_helper_starts_from_fresh_seed_and_ignores_stale_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            seed_capture = root / "seed-before-configure.answers"
            output_dir = root / "generated"
            cross_exec_capture = root / "cross-exec-args.txt"
            cross_exec = root / "cross-exec.sh"
            stale_answers = root / "stale.answers"
            stale_answers.write_text(
                "Checking stale tracked answer: CARRIED-FORWARD\n"
                "Checking whether the realpath function allows a NULL argument: OK\n"
            )
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd4be", capture)
            env.update(
                {
                    "SAMBA4X_CROSS_ANSWERS": str(stale_answers),
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "SAMBA4X_GENERATED_CROSS_ANSWERS_DIR": str(output_dir),
                    "TEST_CONFIGURE_WRITES_ANSWERS": "1",
                    "TEST_REALPATH_ANSWER": "NO",
                    "TEST_SEED_CAPTURE": str(seed_capture),
                    "TEST_CROSS_EXEC_ARGS": str(cross_exec_capture),
                    "TEST_CROSS_EXEC_RC": "1",
                }
            )

            result = self.run_wrapper("generate-samba4x-cross-answers-oldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.configure_args(capture)
            self.assertIn("--disable-pthread", args)
            self.assertIn("--disable-pthreadpool", args)
            self.assertIn("--disable-tdb-mutex-locking", args)
            cross_answers = self.cross_answer_arg(args)
            self.assertTrue(cross_answers.endswith("/generated-samba4x-4.24.3-netbsd4be.answers"))
            self.assertEqual(len(self.cross_execute_args(args)), 1)
            seed = seed_capture.read_text()
            self.assertIn('Checking uname sysname type: "NetBSD"', seed)
            self.assertNotIn("CARRIED-FORWARD", seed)
            generated = output_dir / "samba4x-4.24.3-netbsd4be.answers"
            generated_text = generated.read_text()
            self.assertIn("Checking whether the realpath function allows a NULL argument: NO", generated_text)
            self.assertNotIn("CARRIED-FORWARD", generated_text)
            self.assertTrue(cross_exec_capture.exists())

    def test_refresh_mode_is_generation_alias_with_cross_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            output_dir = root / "generated"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd7", capture)
            env.update(
                {
                    "SAMBA4X_REFRESH_CROSS_ANSWERS": "1",
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "SAMBA4X_GENERATED_CROSS_ANSWERS_DIR": str(output_dir),
                    "TEST_CONFIGURE_WRITES_ANSWERS": "1",
                    "TEST_REALPATH_ANSWER": "OK",
                    "TEST_CROSS_EXEC_RC": "0",
                }
            )

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = self.configure_args(capture)
            self.cross_answer_arg(args)
            self.assertEqual(len(self.cross_execute_args(args)), 1)
            self.assertTrue((output_dir / "samba4x-4.24.3-netbsd7.answers").exists())

    def test_lane_wrappers_select_their_default_cross_answer_files(self) -> None:
        cases = (
            ("samba4x.sh", "netbsd7", "samba4x-4.24.3-netbsd7.answers"),
            ("samba4xoldle.sh", "netbsd4le", "samba4x-4.24.3-netbsd4le.answers"),
            ("samba4xoldbe.sh", "netbsd4be", "samba4x-4.24.3-netbsd4be.answers"),
        )
        for wrapper, lane, expected in cases:
            with self.subTest(wrapper=wrapper):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    capture = root / "configure-args.txt"
                    env = self.env_for_lane(root, lane, capture)

                    result = self.run_wrapper(wrapper, env)

                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    args = self.configure_args(capture)
                    self.assertIn("--disable-pthread", args)
                    self.assertIn("--disable-pthreadpool", args)
                    self.assertIn("--disable-tdb-mutex-locking", args)
                    cross_answers = self.cross_answer_arg(args)
                    self.assertTrue(cross_answers.endswith(f"/{expected}"))

    def test_forbidden_pthread_config_defines_fail_before_build(self) -> None:
        symbols = (
            "HAVE_PTHREAD",
            "HAVE_PTHREAD_CREATE",
            "HAVE_PTHREAD_ATTR_INIT",
            "HAVE_LIBPTHREAD",
            "WITH_PTHREADPOOL",
            "HAVE_ROBUST_MUTEXES",
            "HAVE_PTHREAD_MUTEXATTR_SETROBUST",
            "HAVE_PTHREAD_MUTEXATTR_SETROBUST_NP",
            "HAVE_DECL_PTHREAD_MUTEX_ROBUST",
            "HAVE_DECL_PTHREAD_MUTEX_ROBUST_NP",
            "HAVE_PTHREAD_MUTEX_CONSISTENT",
            "HAVE_PTHREAD_MUTEX_CONSISTENT_NP",
            "USE_TDB_MUTEX_LOCKING",
        )
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    capture = root / "configure-args.txt"
                    targets = root / "waf-targets.txt"
                    env = self.env_for_lane(root, "netbsd7", capture)
                    env["TEST_CONFIGURE_DEFINE"] = f"#define {symbol} 1"
                    env["TEST_WAF_TARGETS"] = str(targets)

                    result = self.run_wrapper("samba4x.sh", env)

                    self.assertNotEqual(result.returncode, 0)
                    log = Path(env["SAMBA4X_NETBSD7_LOG"]).read_text()
                    self.assertIn(
                        f"unexpectedly defined {symbol}",
                        log,
                    )
                    self.assertFalse(targets.exists())

    def test_missing_generated_config_headers_fail_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            targets = root / "waf-targets.txt"
            env = self.env_for_lane(root, "netbsd7", capture)
            env["TEST_CONFIGURE_NO_HEADERS"] = "1"
            env["TEST_WAF_TARGETS"] = str(targets)

            result = self.run_wrapper("samba4x.sh", env)

            self.assertNotEqual(result.returncode, 0)
            log = Path(env["SAMBA4X_NETBSD7_LOG"]).read_text()
            self.assertIn("generated no config.h files", log)
            self.assertFalse(targets.exists())

    def test_smbd_map_must_be_present_identify_smbd_and_omit_pthread(self) -> None:
        cases = (
            ("missing", None, "1", "missing or empty"),
            ("empty", "", "0", "missing or empty"),
            (
                "wrong-output",
                "OUTPUT(bin/default/testprog elf32-littlearm)\n",
                "0",
                "does not identify the smbd output",
            ),
            (
                "pthread",
                "OUTPUT(bin/default/source3/smbd/smbd elf32-littlearm)\n"
                "/sysroot/usr/lib/libpthread.a(pthread.o)\n",
                "0",
                "contains libpthread.a",
            ),
        )
        for name, map_content, skip_map, expected in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    capture = root / "configure-args.txt"
                    env = self.env_for_lane(root, "netbsd7", capture)
                    env["TEST_SKIP_MAP"] = skip_map
                    if map_content is not None:
                        env["TEST_MAP_CONTENT"] = map_content
                    if name == "missing":
                        stale_map = (
                            Path(env["SAMBA4X_NETBSD7_BUILD"])
                            / "smbd-link.map"
                        )
                        self.make_file(
                            stale_map,
                            "OUTPUT(bin/default/source3/smbd/smbd elf32-littlearm)\n",
                        )

                    result = self.run_wrapper("samba4x.sh", env)

                    self.assertNotEqual(result.returncode, 0)
                    log = Path(env["SAMBA4X_NETBSD7_LOG"]).read_text()
                    self.assertIn(expected, log)

    def test_opt_in_pthreadpool_lifecycle_target_is_offline_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            targets = root / "waf-targets.txt"
            cross_exec_capture = root / "cross-exec-args.txt"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd7", capture)
            env.update(
                {
                    "TEST_WAF_TARGETS": str(targets),
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "TEST_CROSS_EXEC_ARGS": str(cross_exec_capture),
                }
            )

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(targets.read_text().splitlines(), ["smbd/smbd"])
            self.assertIn(
                "--nonshared-binary=smbd/smbd",
                self.configure_args(capture),
            )
            self.assertFalse(cross_exec_capture.exists())

    def test_opt_in_pthreadpool_lifecycle_build_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            targets = root / "waf-targets.txt"
            cross_exec_capture = root / "cross-exec-args.txt"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd7", capture)
            env.update(
                {
                    "TEST_WAF_TARGETS": str(targets),
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "TEST_CROSS_EXEC_ARGS": str(cross_exec_capture),
                    "SAMBA4X_RUN_PTHREADPOOL_SYNC_TEST": "1",
                }
            )

            result = self.run_wrapper("samba4x.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                targets.read_text().splitlines(),
                ["pthreadpool_tevent_sync_test", "smbd/smbd"],
            )
            self.assertIn(
                "--nonshared-binary=smbd/smbd,pthreadpool_tevent_sync_test",
                self.configure_args(capture),
            )
            self.assertTrue(cross_exec_capture.exists())
            log = Path(env["SAMBA4X_NETBSD7_LOG"]).read_text()
            self.assertIn(
                "SAMBA4X_BUILD_PTHREADPOOL_SYNC_TEST=1",
                log,
            )
            self.assertNotIn(
                "-Map=",
                next(
                    line
                    for line in log.splitlines()
                    if line.startswith(
                        "TC_PTHREADPOOL_TEST_STATIC_LINKFLAGS="
                    )
                ),
            )

    def test_netbsd4_without_gc_sections_still_generates_smbd_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            env = self.env_for_lane(root, "netbsd4be", capture)
            env["SAMBA4X_NETBSD4_GC_SECTIONS"] = "0"

            result = self.run_wrapper("samba4xoldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            map_path = Path(env["SAMBA4X_NETBSD4BE_BUILD"]) / "smbd-link.map"
            self.assertIn("source3/smbd/smbd", map_path.read_text())

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

    def test_conflicting_duplicate_cross_answers_fail_before_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            answers = root / "conflicting.answers"
            answers.write_text(
                "Checking duplicate behavior: OK\n"
                "Checking duplicate behavior: NO\n"
            )
            env = self.env_for_lane(root, "netbsd7", capture)
            env["SAMBA4X_CROSS_ANSWERS"] = str(answers)

            result = self.run_wrapper("samba4x.sh", env)

            self.assertNotEqual(result.returncode, 0)
            log = Path(env["SAMBA4X_NETBSD7_LOG"]).read_text()
            self.assertIn("contains conflicting duplicate answers", log)
            self.assertFalse(capture.exists())

    def test_netbsd4_realpath_ok_cross_answers_fail_before_configure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            answers = root / "netbsd4-bad-realpath.answers"
            answers.write_text(
                'Checking uname sysname type: "NetBSD"\n'
                "Checking whether the realpath function allows a NULL argument: OK\n"
            )
            env = self.env_for_lane(root, "netbsd4be", capture)
            env["SAMBA4X_CROSS_ANSWERS"] = str(answers)

            result = self.run_wrapper("samba4xoldbe.sh", env)

            self.assertNotEqual(result.returncode, 0)
            log = Path(env["SAMBA4X_NETBSD4BE_LOG"]).read_text()
            self.assertIn("incorrectly allows realpath(path, NULL)", log)
            self.assertFalse(capture.exists())

    def test_generation_normalizes_duplicate_answers_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            output_dir = root / "generated"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd4be", capture)
            env.update(
                {
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "SAMBA4X_GENERATED_CROSS_ANSWERS_DIR": str(output_dir),
                    "TEST_CONFIGURE_WRITES_ANSWERS": "1",
                    "TEST_REALPATH_ANSWER": "OK",
                    "TEST_DUPLICATE_REALPATH_ANSWER": "NO",
                    "TEST_CROSS_EXEC_RC": "1",
                }
            )

            result = self.run_wrapper("generate-samba4x-cross-answers-oldbe.sh", env)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            generated = output_dir / "samba4x-4.24.3-netbsd4be.answers"
            realpath_lines = [
                line
                for line in generated.read_text().splitlines()
                if line.startswith("Checking whether the realpath function allows a NULL argument:")
            ]
            self.assertEqual(
                realpath_lines,
                ["Checking whether the realpath function allows a NULL argument: NO"],
            )

    def test_generation_fails_when_independent_probe_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "configure-args.txt"
            output_dir = root / "generated"
            cross_exec = root / "cross-exec.sh"
            self.make_fake_cross_execute(cross_exec)
            env = self.env_for_lane(root, "netbsd4be", capture)
            env.update(
                {
                    "SAMBA4X_CROSS_EXECUTE": str(cross_exec),
                    "SAMBA4X_GENERATED_CROSS_ANSWERS_DIR": str(output_dir),
                    "TEST_CONFIGURE_WRITES_ANSWERS": "1",
                    "TEST_REALPATH_ANSWER": "OK",
                    "TEST_CROSS_EXEC_RC": "1",
                }
            )

            result = self.run_wrapper("generate-samba4x-cross-answers-oldbe.sh", env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "disagrees with independent realpath(path, NULL) probe",
                Path(env["SAMBA4X_NETBSD4BE_LOG"]).read_text(),
            )
            self.assertFalse((output_dir / "samba4x-4.24.3-netbsd4be.answers").exists())


if __name__ == "__main__":
    unittest.main()
