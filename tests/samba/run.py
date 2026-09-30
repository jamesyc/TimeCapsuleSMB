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
TARGETS = ("tc_pthreadpool_sync_test", "tc_aio_fork_test", "tc_durable_reconnect_test",
           "tc_streams_xattr_test", "tc_native_metadata_test", "tc_xattr_migrate_test", "tc_storage_reload_test",
           "tc_native_links_test", "tc_catia_links_test", "tc_at_emulation_test", "tc_file_growth_test",
           "tc_fork_repair_test")
MIGRATOR_TARGET = "tc_xattr_hfs_migrate"
SMBD_TARGET = "smbd/smbd"
# Compile the production accept/fork call site as well as the extracted helper.
BUILD_TARGETS = (SMBD_TARGET, *TARGETS, MIGRATOR_TARGET)
AIO_CASES = (
    "read", "short", "empty", "zero", "oversized", "read_error", "pwrite", "append", "fsync",
    "pwrite_error", "append_error", "fsync_error",
    "sync_read", "sync_read_error", "sync_pwrite", "sync_pwrite_error",
    "sync_append", "sync_append_error", "sync_fsync", "sync_fsync_error",
    "queue", "cancel_queued", "cancel_active", "queued_fork_failure", "teardown", "orphan_error",
    "dispatch_failure", "allocation_failure", "response_failure",
    "limits", "unlimited", "cleanup", "full_buffer", "over_buffer",
    "exit_frames", "exit_frames_debug", "exit_no_frames", "exit_late_frames", "data_page_writes",
)
DURABLE_CASES = (
    "transition", "exhausted", "already_disconnected", "client_mismatch",
    "create_mismatch", "owner_mismatch", "not_durable", "database_failure", "v1_reconnect",
)

STREAM_CASES = ("hfs_windows_boundary", "charset_types", "root_delete", "nested_delete", "extent_delete", "missing_primary",
                "missing_path", "invalid_stream", "primary_error", "extent_error", "roundtrip_shrink",
                "shrink_missing", "short_read", "read_error")
NATIVE_METADATA_CASES = (
    "syscall_abi", "native_xattrs", "native_xattr_list", "non_hfs_tdb",
    "finderinfo", "finderinfo_views", "resource_backend", "resource_views", "stream_boundary",
    "link_xattrs",
)
XATTR_MIGRATE_CASES = (
    "guard", "appledouble", "embedded_xattrs", "resource", "cleanup", "tdb", "errors", "resume", "scan",
    "orphans", "multi", "oversized", "long_names", "folder_forks", "hfs",
)
NATIVE_LINKS_CASES = ("apple_format", "format_limits", "parse_rejects", "convert_created", "convert_write_only",
                      "convert_refused", "sole_open", "commit_races", "commit_failures", "rollback_races",
                      "metadata", "read_xsym", "write_xsym", "reparse_created", "reparse_refused", "capabilities",
                      "dos_mode", "nofollow_errno")
CATIA_LINKS_CASES = ("catia_links",)
AT_EMULATION_CASES = ("calls", "absolute", "errors", "flags", "renamed", "rename", "long_paths",
                      "cross_directory", "listing_changes", "fds", "times", "time_range", "fdopendir")
FILE_GROWTH_CASES = ("unchecked", "stale_size", "fits", "exceeds", "boundary", "not_hfs", "no_volume",
                     "fstat_error", "real_volume", "real_resource_fork", "call_write", "call_pwrite_send",
                     "call_set_filelen", "call_offload")
# The NetBSD 6 fork() repair (patch 0070); "kernel" also checks that the
# NetBSD 4 kernel needs none. Skipped where the build does not link it.
FORK_REPAIR_CASES = ("kernel", "bounds", "registry", "regions", "read_after_repair", "nested", "fallback")
STORAGE_RELOAD_CASES = ("descriptors", "sentinels", "identity", "aio", "callbacks",
                        "root", "root_widen", "root_rename", "root_no_fds", "root_aio", "root_failed", "root_unchanged")


# Where patch 0065 checks HFS growth before any I/O: (file, first line of the
# region, head of the function that ends it). Each region is contiguous.
GROWTH_CALLERS = (
    ("source3/smbd/fileio.c", "static ssize_t real_write_file(", "static ssize_t real_write_file("),
    ("source3/smbd/smb2_aio.c", "struct pwrite_fsync_state {", "ssize_t pwrite_fsync_recv("),
    ("source3/smbd/vfs.c", "int vfs_set_filelen(", "int vfs_set_filelen("),
    ("source3/modules/vfs_default.c", "struct vfswrap_offload_write_state {",
     "static struct tevent_req *vfswrap_offload_write_send("),
)


def cut(text: str, first: str, last: str) -> str:
    """From first up to the end of the function whose head is last (Samba
    style: the body's braces are the only ones in column 0)."""
    start = text.index(first)
    end = text.index("\n}", text.index("\n{", text.index(last, start))) + 2
    return text[start:end]


def stage(source: Path) -> None:
    modules = source / "source3/modules"
    script = modules / "wscript_build"
    marker = "\n# TC_SAMBA_REGRESSION_TARGETS\n"
    original = script.read_text().split(marker)[0]
    for name in TARGETS:
        shutil.copy2(HERE / (name + ".c"), modules / (name + ".c"))
    # Compile the exact static callbacks in this patched tree. Their enclosing
    # server.c main is irrelevant to the routing test and cannot be linked into
    # a second executable; do not maintain copied callback implementations.
    callbacks = []
    for filename, names in {
        "server.c": ("smbd_parent_conf_updated", "smbd_parent_sig_hup_handler"),
        "smb2_process.c": ("smbd_sig_hup_handler", "smbd_conf_updated"),
    }.items():
        text = (source / "source3/smbd" / filename).read_text()
        for name in names:
            head = "static void " + name + "("
            callbacks.append(cut(text, head, head))
    (modules / "tc_storage_reload_callbacks.inc").write_text("\n\n".join(callbacks) + "\n")
    # Likewise the callers tc_file_growth_test drives, so that a hook dropped
    # from them, or moved after their I/O, fails on the host.
    callers = [cut((source / filename).read_text(), first, last) for filename, first, last in GROWTH_CALLERS]
    (modules / "tc_file_growth_callers.inc").write_text("\n\n".join(callers) + "\n")
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
    for case in NATIVE_METADATA_CASES:
        yield TARGETS[4], (case,)
    for case in XATTR_MIGRATE_CASES:
        yield TARGETS[5], (case,)
    for case in STORAGE_RELOAD_CASES:
        yield TARGETS[6], (case,)
    for case in NATIVE_LINKS_CASES:
        yield TARGETS[7], (case,)
    for case in CATIA_LINKS_CASES:
        yield TARGETS[8], (case,)
    for case in AT_EMULATION_CASES:
        yield TARGETS[9], (case,)
    for case in FILE_GROWTH_CASES:
        yield TARGETS[10], (case,)
    for case in FORK_REPAIR_CASES:
        yield TARGETS[11], (case,)


def execution_cases(cross_exec: bool):
    """Upload each large native fixture once on storage-constrained devices."""
    combined_targets = (TARGETS[4], TARGETS[5], TARGETS[6], TARGETS[7], TARGETS[10])
    seen: set[str] = set()
    for target, arguments in cases():
        if target in combined_targets:
            if cross_exec:
                if target in seen:
                    continue
                arguments = ("all",)
            seen.add(target)
        yield target, arguments
    if not cross_exec:
        # Also exercise cross-case cleanup/order under sanitizers. Device runs
        # use this same all-in-one form as their sole native invocation.
        for target in combined_targets:
            if target in seen:
                yield target, ("all",)


def case_timeout(target: str, cross_exec: bool) -> int:
    # time_range creates 1.5 x kern.maxvnodes files on the device's HFS (about
    # 20,000 on NetBSD 6) to read times back from the catalog.
    if cross_exec and target in {TARGETS[4], TARGETS[5], TARGETS[9]}:
        return 180
    return 60 if cross_exec else 25


def run_tests(source: Path, cross_exec: str | None = None) -> None:
    """Timeouts kill the whole local test group, including forked AIO workers."""
    if cross_exec is not None:
        remote_dir = os.environ.get("CROSS_EXEC_REMOTE_DIR", "").rstrip("/")
        if not remote_dir.startswith("/Volumes/"):
            raise RuntimeError(
                "device regression tests require CROSS_EXEC_REMOTE_DIR under /Volumes/"
            )
    for target, arguments in execution_cases(cross_exec is not None):
        binary = source / "bin/default/source3/modules" / target
        if cross_exec:
            binary = binary.with_suffix(".stripped")
        if not binary.is_file():
            raise FileNotFoundError(binary)
        command = ([cross_exec] if cross_exec else []) + [str(binary), *arguments]
        print("RUN", target, *arguments, flush=True)
        process = subprocess.Popen(command, start_new_session=True)
        try:
            result = process.wait(timeout=case_timeout(target, cross_exec is not None))
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
                "--nonshared-binary=" + ",".join(BUILD_TARGETS)]
    subprocess.run(["./configure", *options], cwd=source, env=env, check=True)
    host_flags(source)
    subprocess.run([sys.executable, "buildtools/bin/waf", "build", "-j" + str(jobs),
                    "--targets=" + ",".join(BUILD_TARGETS)], cwd=source, env=env, check=True)
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
