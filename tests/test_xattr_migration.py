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
from timecapsulesmb.transport.errors import SshCommandTimeout


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
@pytest.mark.parametrize("helper_mode", [0o600, 0o755])
def test_deploy_migration_stages_temporary_binary_and_keeps_persistent_helper(migration, phase, helper_mode):
    m = migration
    # Apple can unmount the HDD; execute the RAM copy. An SSH-pipe upload
    # need not be executable before the later guarded permissions phase.
    m.helper.chmod(helper_mode)
    result = executor.migrate_xattr_tdb_to_hfs(
        m.connection, m.plan, phase=phase, legacy_metadata="netatalk"
    )
    assert m.calls.read_text().splitlines() == [phase, str(m.tdb), "netatalk", *[v.volume_root for v in m.volumes]]
    assert result.roots == tuple(m.volumes)
    assert result.unavailable_roots == ()
    assert m.helper.exists() and m.tdb.read_text() == "pending"
    assert m.helper.stat().st_mode & 0o777 == helper_mode
    assert not (m.root / "tc-xattr-hfs-migrate").exists()
    assert m.mount.call_count == 3  # payload first, then every discovered volume
    assert f"migration_phase={phase} legacy_metadata=netatalk timeout_seconds=21600" in result.output
    assert "started_at=" in result.output and "finished_at=" in result.output
    assert "migration_exit_code=0" in result.output
    for volume in m.volumes:
        assert f"root={volume.volume_root}" in result.output


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



def test_deploy_migration_retains_failure_details_and_uses_long_timeout(migration, monkeypatch):
    m = migration
    m.helper.write_text('#!/bin/sh\necho "opendir failed path=/Volumes/dk2/problem error=Input/output error" >&2\nexit 4\n')
    original = executor.run_ssh
    timeouts = []

    def observe(connection, command, **kwargs):
        if 'migration_log=' in command:
            timeouts.append(kwargs['timeout'])
        return original(connection, command, **kwargs)

    monkeypatch.setattr(executor, 'run_ssh', observe)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        executor.migrate_xattr_tdb_to_hfs(m.connection, m.plan, phase='copy', legacy_metadata='netatalk')
    assert 'opendir failed path=/Volumes/dk2/problem' in caught.value.stdout
    assert 'migration_exit_code=4' in caught.value.stdout
    assert 'Input/output error' in (m.helper.parent / 'logs/xattr-migration-copy.log').read_text()
    assert timeouts == [6 * 60 * 60]
    assert m.tdb.read_text() == 'pending'


@pytest.mark.parametrize('log_available', [True, False])
def test_migration_timeout_recovers_saved_log_without_masking_timeout(migration, monkeypatch, log_available):
    m = migration
    original = executor.run_ssh
    reads = []

    def timeout_ssh(connection, command, **kwargs):
        if 'migration_child=' in command:
            raise SshCommandTimeout('migration SSH deadline exceeded')
        if command.startswith('/usr/bin/tail '):
            reads.append(kwargs)
            if not log_available:
                raise RuntimeError('device offline')
            return subprocess.CompletedProcess([], 0, 'opendir failed path=/Volumes/dk2/problem\nerrors=1\n', '')
        return original(connection, command, **kwargs)

    monkeypatch.setattr(executor, 'run_ssh', timeout_ssh)
    with pytest.raises(SshCommandTimeout, match='migration SSH deadline exceeded') as caught:
        executor.migrate_xattr_tdb_to_hfs(m.connection, m.plan, phase='copy', legacy_metadata='stream')
    assert ('opendir failed' if log_available else 'unavailable') in str(caught.value)
    assert 'xattr-migration-copy.log' in str(caught.value)
    assert reads == [{'check': False, 'timeout': 10}]


@pytest.mark.parametrize('reason', ['initial', 'topology_changed', 'active_users_dropped'])
def test_runtime_activates_shares_without_migrating_legacy_metadata(migration, reason):
    m = migration
    text = load_boot_asset_text('manager.sh')
    text = text[text.index('tc_manager_debug_log() {'):text.index('\ntc_prepare_ram_root\n')]
    library = m.root / 'manager-functions.sh'
    library.write_text(text)
    script = f'''
set -eu
. {shlex.quote(str(library))}
TC_RESOLVED_PAYLOAD_DIR={shlex.quote(str(m.helper.parent))}
TC_PAYLOAD_DIR=$TC_RESOLVED_PAYLOAD_DIR
TC_PAYLOAD_LOG_DIR=$TC_PAYLOAD_DIR/logs
TC_TAB=$(printf '\\t')
tc_log() {{ :; }}
tc_now_seconds() {{ echo 1000; }}
tc_elapsed_seconds_since() {{ echo 0; }}
tc_manager_log_topology_rows() {{ :; }}
tc_manager_activate_topology() {{ :; }}
tc_manager_resolve_payload_from_topology() {{ return 0; }}
tc_manager_build_share_state_from_topology() {{ manager_share_rows=Data; }}
tc_manager_configure_ata_from_topology() {{ :; }}
tc_manager_set_payload_state() {{ echo ready; }}
tc_payload_log_dir_ready() {{ return 0; }}
tc_manager_apply_runtime_from_topology {reason} attached-volume
echo "changed=$TC_MANAGER_DISK_STATE_CHANGED shares=$manager_share_rows"
'''
    result = subprocess.run(['/bin/sh', '-c', script], text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ['ready', 'changed=1 shares=Data']
    assert not m.calls.exists()
    assert m.tdb.read_text() == 'pending'
