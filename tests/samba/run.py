"""Build and execute the downstream Samba regression targets.

Host validation owns a disposable checkout. NetBSD builds stage the same C
fixtures into their already-patched source and use their existing toolchain.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGETS = ("pthreadpool_tevent_sync_test", "tc_aio_fork_test", "tc_durable_reconnect_test", "tc_streams_xattr_test")
AIO_CASES = (
    "read", "short", "empty", "zero", "oversized", "read_error", "pwrite", "append", "fsync",
    "pwrite_error", "append_error", "fsync_error",
    "sync_read", "sync_read_error", "sync_pwrite", "sync_pwrite_error",
    "sync_append", "sync_append_error", "sync_fsync", "sync_fsync_error",
    "queue", "cancel_queued", "cancel_active", "queued_fork_failure",
    "dispatch_failure", "allocation_failure", "response_failure",
    "limits", "unlimited", "cleanup",
)
DURABLE_CASES = (
    "transition", "exhausted", "already_disconnected", "client_mismatch",
    "create_mismatch", "owner_mismatch", "not_durable", "database_failure", "v1_reconnect",
)

STREAM_CASES = ("charset_types", "root_delete", "nested_delete", "extent_delete", "missing_primary",
                "missing_path", "invalid_stream", "primary_error", "extent_error", "roundtrip_shrink",
                "shrink_missing", "short_read", "read_error")


def stage(source: Path) -> None:
    modules = source / "source3/modules"
    script = modules / "wscript_build"
    marker = "\n# TC_SAMBA_REGRESSION_TARGETS\n"
    original = script.read_text().split(marker)[0]
    for name in TARGETS[1:]:
        shutil.copy2(HERE / (name + ".c"), modules / (name + ".c"))
    script.write_text(original + marker + (HERE / "targets.py").read_text())


def host_flags(source: Path) -> None:
    # Patch 0001 isolates build-time generators from target flags. Supply the
    # Annex K define those real Samba headers require, also for native builds.
    for cache in (source / "bin/c4che").glob("*_cache.py"):
        with cache.open("a") as stream:
            stream.write("HOST_CFLAGS = ['-D__STDC_WANT_LIB_EXT1__=1']\n")
    (source / "bin/c4che/sambadeps").unlink(missing_ok=True)


def cases():
    yield TARGETS[0], ()
    for case in AIO_CASES:
        yield TARGETS[1], (case,)
    for case in DURABLE_CASES:
        yield TARGETS[2], (case,)
    for case in STREAM_CASES:
        yield TARGETS[3], (case,)


def run_tests(source: Path, cross_exec: str | None = None) -> None:
    """Timeouts kill the whole local test group, including forked AIO workers."""
    for target, arguments in cases():
        folder = "lib/pthreadpool" if target == TARGETS[0] else "source3/modules"
        binary = source / "bin/default" / folder / target
        if cross_exec:
            binary = binary.with_suffix(".stripped")
        if not binary.is_file():
            raise FileNotFoundError(binary)
        command = ([cross_exec] if cross_exec else []) + [str(binary), *arguments]
        print("RUN", target, *arguments, flush=True)
        process = subprocess.Popen(command, start_new_session=True)
        try:
            result = process.wait(timeout=60 if cross_exec else 25)
            if result:
                raise subprocess.CalledProcessError(result, command)
        finally:
            # Also collect descendants after an assertion failure in a driver.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def host(work: Path, jobs: int, sanitizers: bool) -> None:
    # --work must be a new directory. This command never resets a user's tree.
    work.mkdir(parents=True, exist_ok=False)
    source = work / "source"
    config = str(ROOT / "build/env.sh")
    # Use the build's source pin, including deliberate environment overrides.
    # Loading only defaults keeps host tests independent of device credentials.
    url, ref = subprocess.check_output(
        ["sh", "-c", '. "$1"; printf "%s\\n%s\\n" "$SAMBA4X_GIT_URL" "$SAMBA4X_GIT_REF"',
         config, config], env=dict(os.environ, TC_ENV_FILE="/dev/null"), text=True,
    ).splitlines()
    subprocess.run(["git", "clone", "--depth", "1", "--branch", ref,
                    url, str(source)], check=True)
    subprocess.run(["sh", "-c", '. "$1"; patch_apply_series Samba "$2" "$3"', "sh",
                    str(ROOT / "build/_patch_helpers.sh"),
                    str(ROOT / "build/patches/samba4x/series"), str(source)], check=True)
    stage(source)
    env = dict(os.environ, PYTHONHASHSEED="1", PYTHON=sys.executable)
    if sanitizers:
        flags = "-fsanitize=address,undefined -fno-omit-frame-pointer"
        env.update(CFLAGS=flags, LDFLAGS=flags,
                   ASAN_OPTIONS="detect_leaks=0:exitcode=86",
                   UBSAN_OPTIONS="halt_on_error=1:exitcode=86")
    options = ["--without-" + item for item in (
        "ad-dc", "ads", "ldap", "acl-support", "pam", "json", "libarchive", "winbind",
        "quotas", "utmp", "automount", "dmapi", "gettext", "syslog", "ldb-lmdb")]
    options += ["--disable-" + item for item in (
        "python", "pthread", "pthreadpool", "tdb-mutex-locking", "cups", "iprint", "avahi")]
    options += ["--bundled-libraries=ALL", "--with-shared-modules=!vfs_snapper",
                "--nonshared-binary=" + ",".join(TARGETS)]
    subprocess.run(["./configure", *options], cwd=source, env=env, check=True)
    host_flags(source)
    subprocess.run([sys.executable, "buildtools/bin/waf", "build", "-j" + str(jobs),
                    "--targets=" + ",".join(TARGETS)], cwd=source, env=env, check=True)
    # Child processes must inherit sanitizer runtime settings as well.
    subprocess.run([sys.executable, "-m", "tests.samba.run", "run", "--source", str(source)],
                   cwd=ROOT, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "run"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        if name == "run":
            command.add_argument("--cross-exec")
    command = commands.add_parser("host")
    command.add_argument("--work", required=True, type=Path)
    command.add_argument("--jobs", default=2, type=int)
    command.add_argument("--sanitizers", action="store_true")
    args = parser.parse_args()
    if args.command == "stage":
        stage(args.source.resolve())
    elif args.command == "run":
        run_tests(args.source.resolve(), args.cross_exec)
    else:
        host(args.work.resolve(), args.jobs, args.sanitizers)


if __name__ == "__main__":
    main()
