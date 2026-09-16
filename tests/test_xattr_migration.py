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


@pytest.fixture
def migration(tmp_path, monkeypatch):
    volume = tmp_path / "disk with spaces"
    payload = volume / ".samba4"
    (payload / "private").mkdir(parents=True)
    tdb = payload / "private/xattr.tdb"
    tdb.write_text("pending")
    calls = tmp_path / "calls"
    helper = payload / "xattr-hfs-migrate"
    helper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {shlex.quote(str(calls))}\nexit "${{FAIL_MIGRATION:-0}}"\n')
    helper.chmod(0o755)
    binary = Path("unused")
    plan = build_deployment_plan(
        "test", PayloadHome(str(volume), "/dev/dk2", ".samba4"), binary, binary, binary,
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
    library = manager_library(m.root)
    rows = "\n".join(
        f"wd0\t1\tdk{i+2}\t{v.volume_root}\tData\tuuid-{i}"
        for i, v in enumerate(m.volumes)
    )
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_TAB=$(printf '\\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
manager_topology_rows={shlex.quote(rows)}
FRUIT_METADATA_NETATALK=1
TC_BOOT_XATTR_MIGRATION={enabled}
export FAIL_MIGRATION=0
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
for attempt in 1 2; do
    if tc_manager_migrate_boot_xattrs "$manager_topology_rows"; then echo ok; else echo failed; fi
done
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["ok", "ok"]
    lines = m.calls.read_text().splitlines() if m.calls.exists() else []
    assert [lines[i] for i in range(0, len(lines), 5)] == expected
    assert m.helper.exists() and m.tdb.exists()
    assert not (m.root / "tc-xattr-hfs-migrate").exists()


def test_boot_migration_without_tdb_marks_mounted_volumes_complete(migration):
    # A payload with no legacy TDB has nothing to migrate. The manager must still
    # record the mounted volumes as done, or every later pass would treat them as
    # newly available migration volumes and restart the mDNS advertiser.
    m = migration
    m.tdb.unlink()
    library = manager_library(m.root)
    rows = "\n".join(
        f"wd0\t1\tdk{i+2}\t{v.volume_root}\tData\tuuid-{i}"
        for i, v in enumerate(m.volumes)
    )
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
TC_TAB=$(printf '\\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote(rows)}
tc_log() {{ printf '%s\\n' "$*"; }}
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
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    unavailable = f"metadata migration pending for unavailable volume: device=/dev/dk3 root={m.volumes[1].volume_root}"
    assert result.stdout.splitlines() == [
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
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["clear"]


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
printf 'topology=%s shares=%s payload=%s\n' "$manager_topology_rows" "$manager_share_rows" "$manager_payload_dir"
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "topology=old-topology shares=old-shares payload=/old/payload\n"


def test_boot_migration_failure_is_retried_without_marking_volume_complete(migration):
    m = migration
    library = manager_library(migration.root)
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
TC_TAB=$(printf '\t')
TC_LOG_FILE={shlex.quote(str(migration.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(migration.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote('wd0' + chr(9) + '1' + chr(9) + 'dk2' + chr(9) + migration.volumes[0].volume_root + chr(9) + 'Data' + chr(9) + 'uuid')}
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
export FAIL_MIGRATION=4
if tc_manager_migrate_boot_xattrs "$rows"; then exit 9; fi
if tc_manager_migrate_boot_xattrs "$rows"; then exit 8; fi
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
    ]
    assert not (m.root / "tc-xattr-hfs-migrate").exists()


def test_boot_migration_missing_tdb_records_volume_as_done(migration):
    m = migration
    library = manager_library(m.root)
    m.tdb.unlink()
    rows = "\n".join(
        f"wd0\t1\tdk{i+2}\t{v.volume_root}\tData\tuuid-{i}"
        for i, v in enumerate(m.volumes)
    )
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
TC_TAB=$(printf '\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote(rows)}
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
tc_manager_migrate_boot_xattrs "$rows"
tc_manager_migrate_boot_xattrs "$rows"
if tc_manager_pending_xattr_volume_mounted "$rows"; then exit 9; fi
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not m.calls.exists()
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert m.helper.exists() and not m.tdb.exists()


def test_boot_migration_skips_offline_volume_then_migrates_it_when_mounted(migration):
    m = migration
    library = manager_library(m.root)
    mounted_external = m.root / "external-mounted"
    rows = "\n".join(
        f"wd0\t1\tdk{i+2}\t{v.volume_root}\tData\tuuid-{i}"
        for i, v in enumerate(m.volumes)
    )
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
TC_TAB=$(printf '\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
FRUIT_METADATA_NETATALK=1
rows={shlex.quote(rows)}
tc_log() {{ :; }}
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
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    lines = m.calls.read_text().splitlines()
    assert lines == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "copy", str(m.tdb), "netatalk", m.volumes[1].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[1].volume_root,
    ]


def test_boot_migration_runs_and_removes_ram_copy(migration):
    m = migration
    library = manager_library(m.root)
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_BOOT_XATTR_MIGRATION=1
refresh_reason=initial
TC_TAB=$(printf '\\t')
TC_LOG_FILE={shlex.quote(str(m.root / 'boot.log'))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
manager_topology_rows={shlex.quote('wd0' + chr(9) + '1' + chr(9) + 'dk2' + chr(9) + m.volumes[0].volume_root + chr(9) + 'Data' + chr(9) + 'uuid')}
FRUIT_METADATA_NETATALK=1
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
if ! tc_manager_migrate_boot_xattrs "$manager_topology_rows"; then exit 9; fi
'''
    result = subprocess.run(["/bin/sh", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert m.calls.read_text().splitlines() == [
        "copy", str(m.tdb), "netatalk", m.volumes[0].volume_root,
        "cleanup", str(m.tdb), "netatalk", m.volumes[0].volume_root,
    ]
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert m.tdb.exists() and m.helper.exists()


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
