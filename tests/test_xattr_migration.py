from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from timecapsulesmb.deploy import executor
from timecapsulesmb.deploy.boot_assets import load_boot_asset_text
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.device.storage import PayloadHome
from timecapsulesmb.transport.ssh import SshConnection


# Stands in for the real migrator. `fingerprint` answers with a checksum of
# the file so the manager's checkpoint logic sees a generation that moves
# with the database contents; copy/cleanup are recorded and can be told to
# fail, retire the TDB, or quarantine it, like the real helper does.
FAKE_MIGRATOR = """#!/bin/sh
if [ "$1" = fingerprint ]; then
    [ -f "$2" ] || exit 3
    set -- $(cksum "$2")
    printf 'fingerprint=%s-%s\\n' "$2" "$1"
    exit 0
fi
printf "%s\\n" "$@" >> {calls}
if [ "$1" = cleanup ] && [ -n "${{MIGRATION_OUTCOME:-}}" ]; then
    case "$MIGRATION_OUTCOME" in
        retire) rm -f "$2" ;;
        quarantine) mv "$2" "$2.orphaned.1" ;;
        retire_rows) printf 'retired' >"$2" ;;
    esac
fi
exit "${{FAIL_MIGRATION:-0}}"
"""


@pytest.fixture
def migration(tmp_path, monkeypatch):
    volume = tmp_path / "disk with spaces"
    payload = volume / ".samba4"
    (payload / "private").mkdir(parents=True)
    tdb = payload / "private/xattr.tdb"
    tdb.write_text("pending")
    calls = tmp_path / "calls"
    helper = payload / "xattr-hfs-migrate"
    helper.write_text(FAKE_MIGRATOR.format(calls=shlex.quote(str(calls))))
    helper.chmod(0o755)
    binary = Path("unused")
    plan = build_deployment_plan(
        "test", PayloadHome(str(volume), "/dev/dk2", ".samba4"), binary, binary,
        xattr_migrator_path=helper, rsync_path=binary, service_path=binary, telemetry_path=binary,
    )
    volumes = [SimpleNamespace(volume_root=str(volume), device_path="/dev/dk2"),
               SimpleNamespace(volume_root=str(tmp_path / "external"), device_path="/dev/dk3")]
    mount = Mock(return_value=True)
    discover = Mock(return_value=volumes)
    monkeypatch.setattr(executor, "ensure_volume_root_mounted_conn", mount)
    monkeypatch.setattr(executor, "read_mast_volumes_conn", discover)

    def local_ssh(_connection, command, *, check=True, **_kwargs):
        # Run the production script against a temporary fake device, including
        # its traps and failure branches. Never issue an actual SSH connection.
        command = command.replace("/mnt/Memory", str(tmp_path)).replace("/bin/sync", "true")
        return subprocess.run(shlex.split(command), text=True, capture_output=True, check=check)

    monkeypatch.setattr(executor, "run_ssh", local_ssh)
    return SimpleNamespace(plan=plan, connection=SshConnection("test", "unused", ""),
                           tdb=tdb, helper=helper, calls=calls, mount=mount, discover=discover,
                           volumes=volumes, root=tmp_path)


@pytest.mark.parametrize("phase", ["copy", "cleanup"])
def test_deploy_migration_stages_temporary_binary_and_keeps_persistent_helper(migration, phase):
    m = migration
    result = executor.migrate_xattr_tdb_to_hfs(
        m.connection, m.plan, phase=phase, legacy_metadata="netatalk"
    )
    assert m.calls.read_text().splitlines() == [phase, str(m.tdb), "netatalk", *[v.volume_root for v in m.volumes]]
    assert result.roots == tuple(m.volumes)
    assert result.unavailable_roots == ()
    assert m.helper.exists() and m.tdb.read_text() == "pending"
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert m.mount.call_count == 3  # payload first, then every discovered volume


def test_no_tdb_skips_discovery_and_scan_but_keeps_helper(migration):
    m = migration
    m.tdb.unlink()
    result = executor.migrate_xattr_tdb_to_hfs(
        m.connection, m.plan, phase="cleanup", legacy_metadata="stream"
    )
    assert "no_legacy_tdb" in result.output
    m.discover.assert_not_called()
    assert not m.calls.exists() and m.helper.exists()


@pytest.mark.parametrize("stage", ["payload", "empty_inventory", "helper"])
def test_deploy_migration_failure_preserves_pending_metadata(migration, monkeypatch, stage):
    m = migration
    if stage == "payload":
        m.mount.return_value = False
    elif stage == "empty_inventory":
        m.discover.return_value = []
    else:
        monkeypatch.setenv("FAIL_MIGRATION", "4")
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        executor.migrate_xattr_tdb_to_hfs(m.connection, m.plan, phase="copy", legacy_metadata="stream")
    assert m.tdb.read_text() == "pending" and m.helper.exists()
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    if stage != "helper":
        assert not m.calls.exists()


def test_deploy_migration_skips_unavailable_volumes(migration):
    m = migration
    m.mount.side_effect = [True, True, False]

    result = executor.migrate_xattr_tdb_to_hfs(
        m.connection, m.plan, phase="copy", legacy_metadata="stream"
    )

    assert result.roots == (m.volumes[0],)
    assert result.unavailable_roots == (m.volumes[1].volume_root,)
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "stream", m.volumes[0].volume_root
    ]


def test_deploy_cleanup_reuses_only_copy_roots_that_remain_mounted(migration):
    m = migration
    copy = executor.migrate_xattr_tdb_to_hfs(
        m.connection, m.plan, phase="copy", legacy_metadata="stream"
    )
    m.calls.unlink()
    m.mount.reset_mock()
    m.mount.side_effect = [True, False, True]

    cleanup = executor.migrate_xattr_tdb_to_hfs(
        m.connection,
        m.plan,
        phase="cleanup",
        legacy_metadata="stream",
        roots=copy.roots,
    )

    assert cleanup.roots == (m.volumes[1],)
    assert cleanup.unavailable_roots == (m.volumes[0].volume_root,)
    assert m.calls.read_text().splitlines() == [
        "cleanup", str(m.tdb), "stream", m.volumes[1].volume_root
    ]


@pytest.mark.parametrize("phase", ["copy", "cleanup"])
def test_stopping_deploy_migration_stops_helper_and_removes_ram_copy(migration, monkeypatch, phase):
    import os
    import time
    from concurrent.futures import ThreadPoolExecutor

    m = migration
    pid_file = m.root / "deploy-helper.pid"
    m.helper.write_text(f'#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\nexec /bin/sleep 30\n')
    running = SimpleNamespace(process=None)

    def interruptible_ssh(_connection, command, *, check=True, **_kwargs):
        command = command.replace("/mnt/Memory", str(m.root)).replace("/bin/sync", "true")
        args = shlex.split(command)
        process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        running.process = process
        stdout, stderr = process.communicate()
        completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
        return completed

    monkeypatch.setattr(executor, "run_ssh", interruptible_ssh)
    child_pid = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            executor.migrate_xattr_tdb_to_hfs,
            m.connection,
            m.plan,
            phase=phase,
            legacy_metadata="stream",
        )
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pid_file.exists(), "deploy migration did not start"
            child_pid = int(pid_file.read_text())
            running.process.terminate()
            with pytest.raises(subprocess.CalledProcessError):
                future.result(timeout=5)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            assert not (m.root / "tc-xattr-hfs-migrate").exists()
            assert m.tdb.exists() and m.helper.exists()
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass
            if running.process is not None and running.process.poll() is None:
                running.process.kill()


def manager_preamble(m, rows, enabled=1, log="tc_log() { :; }"):
    """Common shell setup: the library, topology rows, and stubs for the
    device-only probes (df, /sbin/mount, acp MaSt) keyed on the fixture's
    volumes: volumes[0] is /dev/dk2, volumes[1] is /dev/dk3."""
    library = manager_library(m.root)
    return f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION={enabled}
TC_TAB=$(printf '\\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote(rows)}
{log}
tc_manager_debug_log() {{ :; }}
sleep() {{ /bin/sleep 0.02; }}
tc_now_seconds() {{ echo "${{FAKE_NOW:-1000}}"; }}
tc_manager_volume_mount_device() {{
    case "$1" in
        {shlex.quote(m.volumes[0].volume_root)}) echo /dev/dk2 ;;
        {shlex.quote(m.volumes[1].volume_root)}) echo /dev/dk3 ;;
    esac
}}
tc_manager_xattr_current_topology_rows() {{ printf '%s\\n' "$rows"; }}
'''


def topology_rows(m):
    return "\n".join(
        f"wd0\t1\tdk{i+2}\t{v.volume_root}\tData\tuuid-{i}"
        for i, v in enumerate(m.volumes)
    )


def run_sh(script):
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def manager_library(tmp_path):
    text = load_boot_asset_text("manager.sh")
    text = text[text.index("tc_manager_debug_log() {"):text.index("\ntc_prepare_ram_root\n")]
    text = text.replace("/mnt/Memory", str(tmp_path)).replace("/bin/sync", "true")
    wrapper = tmp_path / "migrate.sh"
    wrapper.write_text(load_boot_asset_text("migrate.sh"))
    wrapper.chmod(0o755)
    text = text.replace("/mnt/Flash/migrate.sh", str(wrapper))
    library = tmp_path / "manager-functions.sh"
    library.write_text(text)
    return library


@pytest.mark.parametrize("enabled,expected", [
    (1, ["copy", "cleanup"]),
    (0, []),
])
def test_boot_migration_runs_once_per_mounted_volume(migration, enabled, expected):
    m = migration
    script = manager_preamble(m, topology_rows(m), enabled=enabled) + '''
export FAIL_MIGRATION=0
is_volume_root_mounted() { return 0; }
for attempt in 1 2; do
    if tc_manager_migrate_boot_xattrs "$rows"; then echo ok; else echo failed; fi
done
'''
    assert run_sh(script).splitlines() == ["ok", "ok"]
    lines = m.calls.read_text().splitlines() if m.calls.exists() else []
    assert [lines[i] for i in range(0, len(lines), 5)] == expected
    assert m.helper.exists() and m.tdb.exists()
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert not (m.root / "tc-xattr-hfs-migrate.fp").exists()


def test_boot_migration_without_tdb_marks_mounted_volumes_complete(migration):
    # A payload with no legacy TDB has nothing to migrate. The manager must still
    # record the mounted volumes as done, or every later pass would treat them as
    # newly available migration volumes and restart the mDNS advertiser
    # (regression from 7a6622fd). No checkpoint file is ever written for it.
    m = migration
    m.tdb.unlink()
    script = manager_preamble(m, topology_rows(m), log="tc_log() { printf '%s\\n' \"$*\"; }") + f'''
is_volume_root_mounted() {{ [ "$1" = {shlex.quote(m.volumes[0].volume_root)} ]; }}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending=1; else echo pending=0; fi
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending=1; else echo pending=0; fi
tc_manager_migrate_boot_xattrs "$rows"
is_volume_root_mounted() {{ return 0; }}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending=1; else echo pending=0; fi
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending=1; else echo pending=0; fi
'''
    unavailable = f"metadata migration pending for unavailable volume: device=/dev/dk3 root={m.volumes[1].volume_root}"
    assert run_sh(script).splitlines() == [
        "pending=1",
        unavailable,
        f"metadata migration selected roots count=1 roots={m.volumes[0].volume_root}",
        f"metadata migration skipped: no legacy TDB at {m.tdb}",
        "pending=0",
        unavailable,
        "metadata migration skipped: no mounted pending roots",
        # the second volume becomes mounted later and is handled exactly once
        "pending=1",
        f"metadata migration selected roots count=1 roots={m.volumes[1].volume_root}",
        f"metadata migration skipped: no legacy TDB at {m.tdb}",
        "pending=0",
    ]
    assert not m.calls.exists() and m.helper.exists()
    assert not (m.tdb.parent / "xattr-migration-completed.txt").exists()


def test_boot_migration_failure_withholds_share_state(migration):
    m = migration
    library = manager_library(m.root)
    script = f'''
set -eu
. {shlex.quote(str(library))}
tc_now_seconds() {{ echo 1; }}
tc_manager_count_rows() {{ echo 1; }}
tc_log() {{ :; }}
tc_manager_log_topology_rows() {{ :; }}
tc_manager_activate_topology() {{ :; }}
tc_manager_resolve_payload_from_topology() {{ return 0; }}
tc_manager_migrate_boot_xattrs() {{ return 1; }}
tc_manager_clear_payload_state() {{ echo clear; }}
tc_manager_build_share_state_from_topology() {{ echo unexpected-publish; }}
if tc_manager_apply_runtime_from_topology initial rows; then exit 9; fi
'''
    assert run_sh(script).splitlines() == ["clear"]


def test_attachment_migration_failure_preserves_active_share_state(migration):
    library = manager_library(migration.root)
    script = f'''
set -eu
. {shlex.quote(str(library))}
manager_payload_ready=1
manager_payload_dir=/old/payload
manager_payload_volume=/old
manager_payload_device=/dev/old
manager_topology_rows=old-topology
manager_share_rows=old-shares
tc_now_seconds() {{ echo 1; }}
tc_manager_count_rows() {{ echo 1; }}
tc_log() {{ :; }}
tc_manager_log_topology_rows() {{ :; }}
tc_manager_activate_topology() {{ :; }}
tc_manager_resolve_payload_from_topology() {{ return 0; }}
tc_manager_migrate_boot_xattrs() {{ return 1; }}
tc_manager_clear_payload_state() {{ echo unexpected-clear; }}
tc_manager_build_share_state_from_topology() {{ echo unexpected-publish; }}
if tc_manager_apply_runtime_from_topology topology_changed new-topology; then exit 9; fi
printf 'topology=%s shares=%s payload=%s\\n' "$manager_topology_rows" "$manager_share_rows" "$manager_payload_dir"
'''
    assert run_sh(script) == "topology=old-topology shares=old-shares payload=/old/payload\n"


def test_deferred_migration_keeps_runtime_state_without_logging_every_pass(migration):
    # While the retry backoff runs, the refresh has the same outcome as a
    # failure (no publish, previous state kept) but only logs at debug level.
    library = manager_library(migration.root)
    script = f'''
set -eu
. {shlex.quote(str(library))}
manager_payload_ready=1
manager_payload_dir=/old/payload
manager_payload_volume=/old
manager_payload_device=/dev/old
manager_topology_rows=old-topology
tc_now_seconds() {{ echo 1; }}
tc_manager_count_rows() {{ echo 1; }}
tc_log() {{ printf 'log %s\\n' "$*"; }}
tc_manager_debug_log() {{ printf 'debug %s\\n' "$*"; }}
tc_manager_log_topology_rows() {{ :; }}
tc_manager_activate_topology() {{ :; }}
tc_manager_resolve_payload_from_topology() {{ return 0; }}
tc_manager_migrate_boot_xattrs() {{ TC_MANAGER_XATTR_DEFERRED=1; return 1; }}
tc_manager_clear_payload_state() {{ echo clear; }}
tc_manager_build_share_state_from_topology() {{ echo unexpected-publish; }}
if tc_manager_apply_runtime_from_topology topology_changed new-topology; then exit 9; fi
echo "topology=$manager_topology_rows"
manager_payload_ready=0
if tc_manager_apply_runtime_from_topology initial new-topology; then exit 8; fi
'''
    assert run_sh(script).splitlines() == [
        "log manager disk refresh start: reason=topology_changed topology_rows=1",
        "debug metadata migration retry pending; runtime state unchanged",
        "topology=old-topology",
        "log manager disk refresh start: reason=initial topology_rows=1",
        "debug metadata migration retry pending; runtime state unchanged",
        "clear",
    ]


def test_boot_migration_failure_is_retried_without_marking_volume_complete(migration):
    m = migration
    rows = topology_rows(m).splitlines()[0]
    script = manager_preamble(m, rows) + '''
is_volume_root_mounted() { return 0; }
export FAIL_MIGRATION=4
if tc_manager_migrate_boot_xattrs "$rows"; then exit 9; fi
FAKE_NOW=2000
if tc_manager_migrate_boot_xattrs "$rows"; then exit 8; fi
'''
    run_sh(script)
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
    ]
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert not (m.tdb.parent / "xattr-migration-completed.txt").exists()


def test_failed_scans_back_off_instead_of_walking_every_pass(migration):
    # 60 s, 120 s, 240 s ... capped at 30 min; a success resets the schedule.
    m = migration
    rows = topology_rows(m).splitlines()[0]
    script = manager_preamble(m, rows, log="tc_log() { case \"$*\" in \"metadata migration will retry\"*) printf '%s\\n' \"$*\";; esac; }") + '''
is_volume_root_mounted() { return 0; }
export FAIL_MIGRATION=4
attempt() {
    FAKE_NOW=$1
    if tc_manager_migrate_boot_xattrs "$rows"; then echo "$1 ok"; else echo "$1 failed deferred=$TC_MANAGER_XATTR_DEFERRED"; fi
    if tc_manager_pending_xattr_volume_mounted "$rows"; then echo "$1 pending"; else echo "$1 not-pending"; fi
}
attempt 1000
attempt 1030
attempt 1060
attempt 1100
attempt 1180
TC_XATTR_RETRY_MAX_SECONDS=300
attempt 1420
attempt 1720
export FAIL_MIGRATION=0
attempt 2020
attempt 2021
'''
    assert run_sh(script).splitlines() == [
        "metadata migration will retry in 60s", "1000 failed deferred=0", "1000 not-pending",
        "1030 failed deferred=1", "1030 not-pending",
        "metadata migration will retry in 120s", "1060 failed deferred=0", "1060 not-pending",
        "1100 failed deferred=1", "1100 not-pending",
        "metadata migration will retry in 240s", "1180 failed deferred=0", "1180 not-pending",
        "metadata migration will retry in 300s", "1420 failed deferred=0", "1420 not-pending",
        "metadata migration will retry in 300s", "1720 failed deferred=0", "1720 not-pending",
        "2020 ok", "2020 not-pending",
        "2021 ok", "2021 not-pending",
    ]
    lines = m.calls.read_text().splitlines()
    # five failed copy attempts, then one full copy+cleanup; the deferred passes ran nothing
    assert [lines[i] for i in range(0, len(lines), 4)] == ["copy"] * 5 + ["copy", "cleanup"]


def test_boot_migration_skips_offline_volume_then_migrates_it_when_mounted(migration):
    m = migration
    mounted_external = m.root / "external-mounted"
    script = manager_preamble(m, topology_rows(m)) + f'''
is_volume_root_mounted() {{
    [ "$1" = {shlex.quote(m.volumes[0].volume_root)} ] ||
        [ -f {shlex.quote(str(mounted_external))} ]
}}
tc_manager_migrate_boot_xattrs "$rows"
tc_manager_migrate_boot_xattrs "$rows"
: >{shlex.quote(str(mounted_external))}
tc_manager_pending_xattr_volume_mounted "$rows"
tc_manager_migrate_boot_xattrs "$rows"
'''
    run_sh(script)
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "copy", str(m.tdb), "netatalk", m.volumes[1].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[1].volume_root,
    ]
    checkpoint = (m.tdb.parent / "xattr-migration-completed.txt").read_text().splitlines()
    assert checkpoint[0].startswith("xattr-migration-completed: format=1 migration=1 written=")
    assert checkpoint[1].startswith("source: 7-")
    assert checkpoint[2:] == ["volume: uuid=uuid-0", "volume: uuid=uuid-1"]


def test_boot_migration_runs_and_removes_ram_copy(migration):
    m = migration
    rows = topology_rows(m).splitlines()[0]
    script = manager_preamble(m, rows) + '''
is_volume_root_mounted() { return 0; }
if ! tc_manager_migrate_boot_xattrs "$rows"; then exit 9; fi
'''
    run_sh(script)
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[0].volume_root,
    ]
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert not (m.root / "tc-xattr-hfs-migrate.fp").exists()
    assert m.tdb.exists() and m.helper.exists()


# ---- package 7: durable checkpoint -------------------------------------

def checkpoint_path(m):
    return m.tdb.parent / "xattr-migration-completed.txt"


def fingerprint(m):
    out = subprocess.run([str(m.helper), "fingerprint", str(m.tdb)], text=True, capture_output=True, check=True)
    return out.stdout.strip().removeprefix("fingerprint=")


def fingerprint_text(text):
    """What the fake migrator prints for a file with this content."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(text)
    out = subprocess.run(["sh", "-c", 'set -- $(cksum "$1"); printf "%s-%s" "$2" "$1"', "sh", handle.name], text=True, capture_output=True, check=True)
    return out.stdout


def write_checkpoint(m, *volumes, source=None, header=None):
    lines = [header or "xattr-migration-completed: format=1 migration=1 written=5",
             f"source: {source or fingerprint(m)}"]
    lines += [f"volume: uuid={uuid}" for uuid in volumes]
    checkpoint_path(m).write_text("\n".join(lines) + "\n")


def migration_calls(m):
    """(phase, root...) per recorded migrator call; a call spans phase, tdb,
    metadata and one line per root."""
    calls = []
    for line in (m.calls.read_text().splitlines() if m.calls.exists() else []):
        if line in ("copy", "cleanup"):
            calls.append([line])
        else:
            calls[-1].append(line)
    return [(call[0], *call[3:]) for call in calls]


CHECKPOINT_LOG = ("tc_log() { case \"$*\" in \"metadata migration checkpoint\"*|\"metadata migration completed\"*) "
                  "printf '%s\\n' \"$*\";; esac; }")


def test_checkpoint_skips_completed_volumes_across_manager_lifetimes(migration):
    # A successful scan that leaves rows behind (another disk's) must not be
    # repeated by the next manager: it reads the checkpoint, sees its own
    # database generation, and only the newly attached volume is scanned.
    m = migration
    first = run_sh(manager_preamble(m, topology_rows(m), log=CHECKPOINT_LOG) + f'''
is_volume_root_mounted() {{ [ "$1" = {shlex.quote(m.volumes[0].volume_root)} ]; }}
tc_manager_migrate_boot_xattrs "$rows"
''').splitlines()
    assert first == [f"metadata migration checkpoint written: {checkpoint_path(m)} source={fingerprint(m)}"]
    assert migration_calls(m) == [("copy", m.volumes[0].volume_root), ("cleanup", m.volumes[0].volume_root)]
    m.calls.unlink()

    # "Next manager": fresh process, same disk. Nothing to do for volume 0.
    second = run_sh(manager_preamble(m, topology_rows(m), log=CHECKPOINT_LOG) + f'''
is_volume_root_mounted() {{ [ "$1" = {shlex.quote(m.volumes[0].volume_root)} ]; }}
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
for pass in 1 2 3; do tc_manager_migrate_boot_xattrs "$rows"; done
is_volume_root_mounted() {{ return 0; }}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
tc_manager_migrate_boot_xattrs "$rows"
''').splitlines()
    assert second == [
        f"metadata migration checkpoint loaded: volumes=1 source={fingerprint(m)}",
        "not-pending",
        "pending",
        f"metadata migration checkpoint written: {checkpoint_path(m)} source={fingerprint(m)}",
    ]
    assert migration_calls(m) == [("copy", m.volumes[1].volume_root), ("cleanup", m.volumes[1].volume_root)]
    assert checkpoint_path(m).read_text().splitlines()[2:] == ["volume: uuid=uuid-0", "volume: uuid=uuid-1"]


@pytest.mark.parametrize("damage,reason", [
    ("source", "source mismatch recorded=7-deadbeef current=7-"),
    ("format", "unsupported header"),
    ("migration", "unsupported header"),
    ("garbage", "malformed line 3"),
    ("truncated", "truncated file"),
    ("empty", "truncated file"),
])
def test_invalid_checkpoint_causes_rescan_never_false_completion(migration, damage, reason):
    m = migration
    if damage == "source":
        write_checkpoint(m, "uuid-0", "uuid-1", source="7-deadbeef")
    elif damage == "format":
        write_checkpoint(m, "uuid-0", "uuid-1", header="xattr-migration-completed: format=2 migration=1 written=5")
    elif damage == "migration":
        write_checkpoint(m, "uuid-0", "uuid-1", header="xattr-migration-completed: format=1 migration=2 written=5")
    elif damage == "garbage":
        write_checkpoint(m, "uuid-0")
        checkpoint_path(m).write_text(checkpoint_path(m).read_text().replace("volume: uuid=uuid-0", "volume uuid-0"))
    elif damage == "truncated":
        checkpoint_path(m).write_text("xattr-migration-completed: format=1 migration=1 written=5\n")
    else:
        checkpoint_path(m).write_text("")
    out = run_sh(manager_preamble(m, topology_rows(m), log=CHECKPOINT_LOG) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
''').splitlines()
    assert out[0].startswith(f"metadata migration checkpoint ignored ({reason}")
    assert out[0].endswith("); pending volumes will be rescanned")
    assert [call[0] for call in migration_calls(m)] == ["copy", "cleanup"]
    # rewritten from what this run proved, against the current generation
    assert checkpoint_path(m).read_text().splitlines()[1:] == [
        f"source: {fingerprint(m)}", "volume: uuid=uuid-0", "volume: uuid=uuid-1",
    ]


SOURCE_LOG = ("tc_log() { case \"$*\" in \"metadata migration source\"*|\"metadata migration checkpoint\"*"
              "|\"metadata migration: legacy\"*) printf '%s\\n' \"$*\";; esac; }")


def test_changed_source_during_manager_lifetime_invalidates_completion(migration):
    # Review 2 R4: a restored or foreign xattr.tdb mid-lifetime must be
    # noticed on the normal disk pass. The observer keys on a change
    # signature (inode/size/mtime), not on mtime ordering, so a restore that
    # carries an *older* mtime (`mv xattr.tdb.bak xattr.tdb`) is seen too.
    m = migration
    out = run_sh(manager_preamble(m, topology_rows(m).splitlines()[0], log=SOURCE_LOG) + f'''
is_volume_root_mounted() {{ return 0; }}
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
printf 'restored-from-backup' >{shlex.quote(str(m.tdb))}.bak
touch -t 200101010000 {shlex.quote(str(m.tdb))}.bak
mv -f {shlex.quote(str(m.tdb))}.bak {shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
echo "completed=[${{TC_MANAGER_XATTR_MIGRATED_VOLUMES:-}}]"
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    assert out[0].startswith("metadata migration checkpoint written:")
    assert out[1] == "not-pending"
    assert out[2].startswith("metadata migration source changed outside migration: recorded=7-")
    assert " current=20-" in out[2] and out[2].endswith("; completed volumes forgotten")
    assert out[3:5] == ["pending", "completed=[]"]
    assert out[5] == f"metadata migration checkpoint written: {checkpoint_path(m)} source={fingerprint(m)}"
    assert out[6] == "not-pending"
    assert [call[0] for call in migration_calls(m)] == ["copy", "cleanup", "copy", "cleanup"]


def test_identical_database_copied_back_is_not_rescanned(migration):
    # Same bytes, new inode and mtime (a copy of the file put back): one
    # fingerprint, still the same source, nothing pending, nothing rewritten,
    # and the next pass compares signatures only.
    m = migration
    out = run_sh(manager_preamble(m, topology_rows(m).splitlines()[0], log=SOURCE_LOG) + f'''
is_volume_root_mounted() {{ return 0; }}
tc_manager_migrate_boot_xattrs "$rows"
cp {shlex.quote(str(m.tdb))} {shlex.quote(str(m.tdb))}.copy
touch -t 203001010000 {shlex.quote(str(m.tdb))}.copy
mv -f {shlex.quote(str(m.tdb))}.copy {shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
printf 'changed-after-the-copy' >{shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    assert out[0].startswith("metadata migration checkpoint written:")
    assert out[1:3] == ["not-pending", "not-pending"]
    # ...and a later real change after the future-dated copy is still caught.
    assert out[3].startswith("metadata migration source changed outside migration:")
    assert out[4] == "pending"
    assert [call[0] for call in migration_calls(m)] == ["copy", "cleanup"]


def test_tdb_introduced_after_a_no_tdb_completion_is_noticed_on_the_disk_pass(migration):
    # Review 2 R4: no TDB -> volumes complete -> a TDB appears (a restore):
    # the completed set said nothing about it, so the volumes are pending.
    m = migration
    m.tdb.unlink()
    out = run_sh(manager_preamble(m, topology_rows(m), log=SOURCE_LOG) + f'''
is_volume_root_mounted() {{ return 0; }}
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
printf 'pending' >{shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
rm -f {shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    assert out == [
        "not-pending",
        "metadata migration: legacy TDB appeared or changed outside migration; completed volumes forgotten",
        "pending",
        f"metadata migration checkpoint written: {checkpoint_path(m)} source={fingerprint_text('pending')}",
        "not-pending",
        "metadata migration: legacy TDB disappeared outside migration; nothing to migrate until one appears",
        "not-pending",
    ]
    assert [call[0] for call in migration_calls(m)] == ["copy", "cleanup"]


def test_tdb_restored_after_quarantine_is_noticed_on_the_disk_pass(migration):
    m = migration
    out = run_sh(manager_preamble(m, topology_rows(m), log=SOURCE_LOG) + f'''
is_volume_root_mounted() {{ return 0; }}
MIGRATION_OUTCOME=quarantine tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
cp {shlex.quote(str(m.tdb))}.orphaned.1 {shlex.quote(str(m.tdb))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    # No checkpoint was ever written (the TDB was quarantined by the run).
    assert out == [
        "not-pending",
        "metadata migration: legacy TDB appeared or changed outside migration; completed volumes forgotten",
        "pending",
    ]


def test_own_row_retirement_preserves_prior_completion(migration):
    # Cleanup rewrites the database (rows retired). The checkpoint records
    # the generation *after* our own write, so the next manager still trusts
    # volume 0 while adding volume 1.
    m = migration
    run_sh(manager_preamble(m, topology_rows(m)) + f'''
is_volume_root_mounted() {{ [ "$1" = {shlex.quote(m.volumes[0].volume_root)} ]; }}
MIGRATION_OUTCOME=retire_rows tc_manager_migrate_boot_xattrs "$rows"
''')
    assert m.tdb.read_text() == "retired"
    assert checkpoint_path(m).read_text().splitlines()[1] == f"source: {fingerprint(m)}"
    m.calls.unlink()
    out = run_sh(manager_preamble(m, topology_rows(m), log=CHECKPOINT_LOG) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
''').splitlines()
    assert out[0].startswith("metadata migration checkpoint loaded: volumes=1")
    assert migration_calls(m) == [("copy", m.volumes[1].volume_root), ("cleanup", m.volumes[1].volume_root)]


@pytest.mark.parametrize("outcome", ["retire", "quarantine"])
def test_retired_or_quarantined_tdb_removes_checkpoint(migration, outcome):
    m = migration
    write_checkpoint(m, "uuid-9")
    out = run_sh(manager_preamble(m, topology_rows(m), log="tc_log() { case \"$*\" in \"metadata migration checkpoint\"*|\"metadata migration retired\"*) printf '%s\\n' \"$*\";; esac; }") + f'''
is_volume_root_mounted() {{ return 0; }}
MIGRATION_OUTCOME={outcome} tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    assert out[1:] == [
        f"metadata migration retired the legacy TDB: {m.tdb}",
        "metadata migration checkpoint removed: legacy TDB is gone",
        "not-pending",
    ]
    assert out[0].startswith("metadata migration checkpoint loaded: volumes=1")
    assert not m.tdb.exists() and not checkpoint_path(m).exists()
    assert (m.tdb.parent / "xattr.tdb.orphaned.1").exists() == (outcome == "quarantine")


def test_volume_without_uuid_is_never_durable(migration):
    # Without a UUID the only identity is a reused /dev/dkN name, so completion
    # stays process-local and the next manager scans the volume again.
    m = migration
    rows = f"wd0\t1\tdk2\t{m.volumes[0].volume_root}\tData\t\nwd0\t1\tdk3\t{m.volumes[1].volume_root}\tData\tuuid-1"
    assert run_sh(manager_preamble(m, rows) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines() == ["not-pending"]
    assert checkpoint_path(m).read_text().splitlines()[2:] == ["volume: uuid=uuid-1"]
    m.calls.unlink()
    run_sh(manager_preamble(m, rows) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
''')
    assert migration_calls(m) == [("copy", m.volumes[0].volume_root), ("cleanup", m.volumes[0].volume_root)]


def test_replacement_disk_with_reused_device_name_is_not_skipped(migration):
    m = migration
    write_checkpoint(m, "uuid-0")
    rows = f"wd0\t1\tdk2\t{m.volumes[0].volume_root}\tData\tuuid-new"
    run_sh(manager_preamble(m, rows) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
''')
    assert migration_calls(m) == [("copy", m.volumes[0].volume_root), ("cleanup", m.volumes[0].volume_root)]
    assert checkpoint_path(m).read_text().splitlines()[2:] == ["volume: uuid=uuid-0", "volume: uuid=uuid-new"]


@pytest.mark.parametrize("change", ["device", "uuid", "mast_unavailable"])
def test_identity_change_during_scan_is_not_recorded(migration, change):
    # The walk proves nothing about a disk that was swapped underneath it.
    m = migration
    rows = topology_rows(m).splitlines()[0]
    scanned = shlex.quote(str(m.root / "scanned"))
    if change == "device":
        after = f'tc_manager_volume_mount_device() {{ if [ -f {scanned} ]; then echo /dev/dk7; else echo /dev/dk2; fi; }}'
    elif change == "uuid":
        after = f'tc_manager_xattr_current_topology_rows() {{ if [ -f {scanned} ]; then printf "%s\\n" "${{rows%uuid-0}}uuid-other"; else printf "%s\\n" "$rows"; fi; }}'
    else:
        after = f'tc_manager_xattr_current_topology_rows() {{ [ ! -f {scanned} ] || return 1; printf "%s\\n" "$rows"; }}'
    m.helper.write_text(m.helper.read_text().replace(
        'exit "${FAIL_MIGRATION:-0}"', f'[ "$1" != cleanup ] || : >{scanned}\nexit "${{FAIL_MIGRATION:-0}}"'))
    out = run_sh(manager_preamble(m, rows, log="tc_log() { case \"$*\" in \"metadata migration identity\"*|\"metadata migration will retry\"*) printf '%s\\n' \"$*\";; esac; }") + f'''
is_volume_root_mounted() {{ return 0; }}
{after}
if tc_manager_migrate_boot_xattrs "$rows"; then echo unexpected-success; fi
echo "completed=[${{TC_MANAGER_XATTR_MIGRATED_VOLUMES:-}}]"
''').splitlines()
    assert out[0].startswith("metadata migration identity check failed:")
    assert out[1:] == ["metadata migration will retry in 60s", "completed=[]"]
    assert not checkpoint_path(m).exists()


def test_unexpected_mount_before_scan_is_skipped(migration):
    m = migration
    rows = topology_rows(m).splitlines()[0]
    out = run_sh(manager_preamble(m, rows, log="tc_log() { printf '%s\\n' \"$*\"; }") + '''
is_volume_root_mounted() { return 0; }
tc_manager_volume_mount_device() { echo /dev/dk9; }
tc_manager_migrate_boot_xattrs "$rows"
''').splitlines()
    assert out == [
        f"metadata migration pending for volume with unexpected mount: root={m.volumes[0].volume_root} mounted='/dev/dk9' expected=/dev/dk2",
        "metadata migration skipped: no mounted pending roots",
    ]
    assert not m.calls.exists()


def test_checkpoint_write_failure_keeps_migration_success_but_not_durable(migration):
    m = migration
    rows = topology_rows(m).splitlines()[0]
    out = run_sh(manager_preamble(m, rows, log=CHECKPOINT_LOG) + f'''
is_volume_root_mounted() {{ return 0; }}
chmod 500 {shlex.quote(str(m.tdb.parent))}
if tc_manager_migrate_boot_xattrs "$rows"; then echo ok; else echo failed; fi
chmod 700 {shlex.quote(str(m.tdb.parent))}
if tc_manager_pending_xattr_volume_mounted "$rows"; then echo pending; else echo not-pending; fi
''').splitlines()
    assert out == [
        f"metadata migration checkpoint not written: cannot write {checkpoint_path(m)}.tmp",
        "metadata migration completed without a durable checkpoint",
        "ok",
        "not-pending",
    ]
    assert not checkpoint_path(m).exists() and not (m.tdb.parent / "xattr-migration-completed.txt.tmp").exists()


def test_crash_between_cleanup_and_checkpoint_rescans(migration):
    # Cleanup rewrote the database but the manager died before publishing:
    # the old checkpoint's source no longer matches, so nothing is skipped.
    m = migration
    write_checkpoint(m, "uuid-0", "uuid-1")
    m.tdb.write_text("rows retired by an interrupted cleanup")
    run_sh(manager_preamble(m, topology_rows(m)) + '''
is_volume_root_mounted() { return 0; }
tc_manager_migrate_boot_xattrs "$rows"
''')
    assert [call[0] for call in migration_calls(m)] == ["copy", "cleanup"]


def test_checkpoint_replacement_failure_leaves_no_partial_file(migration):
    # The temporary file is only ever renamed over the checkpoint; when that
    # rename fails the temporary is removed and the migration still counts.
    m = migration
    # mv would drop the file into a directory of that name, so block that
    # target too: the rename has nowhere to land and must fail.
    (checkpoint_path(m) / "xattr-migration-completed.txt.tmp").mkdir(parents=True)
    rows = topology_rows(m).splitlines()[0]
    out = run_sh(manager_preamble(m, rows, log=CHECKPOINT_LOG) + '''
is_volume_root_mounted() { return 0; }
if tc_manager_migrate_boot_xattrs "$rows"; then echo ok; else echo failed; fi
''').splitlines()
    assert out == [
        f"metadata migration checkpoint not written: cannot replace {checkpoint_path(m)}",
        "metadata migration completed without a durable checkpoint",
        "ok",
    ]
    assert checkpoint_path(m).is_dir()
    assert not (m.tdb.parent / "xattr-migration-completed.txt.tmp").exists()


def test_wrapper_cancellation_terminates_helper_and_removes_ram_copy(migration):
    m = migration
    pid_file = m.root / "helper.pid"
    m.helper.write_text(
        f'#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\nwhile :; do :; done\n'
    )
    wrapper = m.root / "migrate.sh"
    wrapper.write_text(load_boot_asset_text("migrate.sh"))
    wrapper.chmod(0o755)
    ram = m.root / ".tc-xattr-hfs-migrate.wrapper"
    log = m.root / "wrapper.log"
    script = f'''
set -eu
# Symbolic names: USR1/USR2 are 10/12 on Linux but 30/31 on macOS/BSD.
trap ':' USR1 USR2
{shlex.quote(str(wrapper))} copy {shlex.quote(str(m.tdb))} netatalk \\
    {shlex.quote(str(m.helper))} {shlex.quote(str(ram))} "$$" {shlex.quote(str(log))} \\
    {shlex.quote(m.volumes[0].volume_root)} &
wrapper_pid=$!
i=0
while [ ! -f {shlex.quote(str(pid_file))} ] && [ "$i" -lt 200000 ]; do i=$((i + 1)); done
helper_pid=$(cat {shlex.quote(str(pid_file))})
kill -TERM "$wrapper_pid"
wrapper_status=0
wait "$wrapper_pid" || wrapper_status=$?
[ "$wrapper_status" -ne 0 ]
helper_alive=0
kill -0 "$helper_pid" 2>/dev/null && helper_alive=1
[ "$helper_alive" -eq 0 ]
[ ! -e {shlex.quote(str(ram))} ]
rm -f {shlex.quote(str(pid_file))} {shlex.quote(str(log))}
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_manager_traps_remain_normal_after_migration(migration):
    m = migration
    library = manager_library(m.root)
    marker = m.root / "normal-trap"
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
TC_TAB=$(printf '\\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote('wd0' + chr(9) + '1' + chr(9) + 'dk2' + chr(9) + m.volumes[0].volume_root + chr(9) + 'Data' + chr(9) + 'uuid')}
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
manager_stop() {{ echo normal > {shlex.quote(str(marker))}; exit 0; }}
trap 'manager_stop' 15
tc_manager_migrate_boot_xattrs "$rows"
kill -TERM $$
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert marker.read_text().strip() == "normal"
