"""Host-packaged shutdown must work even when every old script is missing."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.device_ps import FakePs, process, ps_row
from timecapsulesmb.device.migration_jobs import running_migrations
from timecapsulesmb.device.processes import render_stop_service_runtime, render_wait_for_idle_jobs

# The fake tools below answer ps as the device does (tests/device_ps.py) from
# rows of "pid stat ucomm command".
FAKE_PS = f'''
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from tests.device_ps import answer, process
def ps(rows):
    out, status, _vanished = answer([process(row) for row in rows], sys.argv[2:])
    sys.stdout.write(out)
    return status
def listing():
    return 'ax' in sys.argv[2:]
'''


@pytest.mark.parametrize('stubborn', [False, True])
def test_service_supervisor_stops_before_workers_and_leaves_apple_alone(tmp_path, stubborn):
    # Apple owns diskd, AFP and mDNSResponder. Only our path/roles are eligible;
    # a shell containing those strings and an unrelated `service` are not.
    state = tmp_path / 'ps.json'
    rows = [
        '12 S service service: role=mdns nbns=disabled',
        '13 S service service: role=netbios nbns=ready',
        '14 S service /mnt/Flash/service telemetry --daemon --control-fd 6',
        '15 S service /mnt/Memory/samba4/sbin/service --collect-policy',
        '20 S service /mnt/Flash/service run',
        '30 S service service: role=manager',
        '31 S service /mnt/Flash/service manager',
        '27 S sh /bin/sh /mnt/Flash/boot.sh',
        '28 S sh sh /mnt/Flash/start-samba.sh',
        '29 S sh /bin/sh /mnt/Flash/rc.local',
        '21 S service /sbin/service run',
        '22 S sh sh -c /mnt/Flash/service run',
        '23 Z service /mnt/Flash/service run',
        '24 S mDNSResponder /sbin/mDNSResponder -d',
        '25 S diskd diskd -i lo0 -d local.',
        '26 S afpserver /sbin/afpserver',
        '32 S service service: role=discovery nbns=ready mode=payload',
        '33 S service service: role=job storage',
    ]
    # A successful scan must return success even when its final row belongs
    # to the other phase (a worker while we are selecting supervisors).
    rows.append(rows.pop(0))
    state.write_text(json.dumps(rows))
    log = tmp_path / 'signals'
    helper = tmp_path / 'helper.py'
    helper.write_text(FAKE_PS + f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
rows = json.loads(state.read_text())
if sys.argv[1] == 'ps':
    sys.exit(ps(rows))
else:
    pid = sys.argv[-1]
    with open({str(log)!r}, 'a') as out: out.write(pid + '\\n')
    if not {stubborn!r}:
        state.write_text(json.dumps([r for r in rows if r.split()[0] != pid]))
''')
    import sys
    script = render_stop_service_runtime(attempts=0, supervisor_seconds=0)
    script = script.replace('/bin/ps', f'{sys.executable} {helper} ps')
    script = script.replace('/bin/kill', f'{sys.executable} {helper} kill')
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)
    if stubborn:
        assert result.returncode == 1
        assert 'did not stop' in result.stderr
        assert log.read_text().splitlines() == ['20', '30', '31', '27', '28', '29']
    else:
        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines() == ['20', '30', '31', '27', '28', '29', '13', '14', '15', '32', '33', '12']
        assert json.loads(state.read_text()) == [r for r in rows if r.split()[0] not in {'12','13','14','15','20','27','28','29','30','31','32','33'}]


IDLE_JOB_ROWS = [
    ('tc-xattr-hfs-mi', 'S', 1), ('xattr-hfs-migrate', 'S', 1),
    ('debug', 'S', 1), ('telemetry', 'S', 1), ('heartbeat', 'S', 1),
    ('tc-xattr-hfs-mi', 'Z', 0), ('afpserver', 'S', 0),
    ('sh /bin/sh /mnt/Flash/migrate.sh', 'S', 1),
    ('sh /mnt/Flash/xattr-migrate-wrapper.sh', 'S', 1),
    ('sh sh -c /mnt/Flash/migrate.sh', 'S', 0),
    ('service service: role=telemetry --daemon', 'S', 1),
    ('service service: role=job storage', 'S', 1),
    ('service service: role=discovery nbns=ready', 'S', 0),
]


@pytest.mark.parametrize('comm,state,expected', IDLE_JOB_ROWS)
def test_active_metadata_and_diagnostic_jobs_block_cleanup(tmp_path, comm, state, expected):
    fake = FakePs(tmp_path, [process(f'17 {state} {comm}')])
    result = subprocess.run(['/bin/sh', '-c', fake.install(render_wait_for_idle_jobs(attempts=0))], capture_output=True)
    assert result.returncode == expected


@pytest.mark.parametrize('comm,state,expected', IDLE_JOB_ROWS)
def test_python_and_shell_agree_on_which_rows_are_migrations(tmp_path, comm, state, expected):
    # device.migration_jobs finds the migrations the shell guard waits for or
    # stops; the telemetry and debug jobs are the shell's alone. Both read
    # the same device listing.
    from timecapsulesmb.device.migration_jobs import JOBS_PS_COMMAND

    migrations = {'tc-xattr-hfs-mi', 'xattr-hfs-migrate', 'sh /bin/sh /mnt/Flash/migrate.sh',
                  'sh /mnt/Flash/xattr-migrate-wrapper.sh'}
    fake = FakePs(tmp_path, [process(f'17 {state} {comm}')])
    listing = subprocess.run(fake.install(JOBS_PS_COMMAND), shell=True, capture_output=True, text=True)
    assert listing.returncode == 0, listing.stderr
    assert bool(running_migrations(listing.stdout)) == (expected == 1 and comm in migrations)


@pytest.mark.parametrize('sync_fails', [False, True])
def test_flush_syncs_once_without_waiting_and_propagates_a_failure(tmp_path, sync_fails):
    # One sync and its exit status, with no settle wait: the ACP restart runs
    # its own sync. The installer's handling of a failed sync is tested in
    # test_cli_deploy.
    import shlex
    from timecapsulesmb.deploy.executor import FLUSH_REMOTE_FILESYSTEMS_COMMAND
    script = FLUSH_REMOTE_FILESYSTEMS_COMMAND.replace('/bin/sync', 'test_sync').replace('/bin/sleep', 'sleep')
    log = tmp_path / 'flush-order'
    helpers = f'''
test_sync() {{ echo sync >> {shlex.quote(str(log))}; [ {int(sync_fails)} = 0 ]; }}
sleep() {{ echo "sleep $1" >> {shlex.quote(str(log))}; }}
'''
    result = subprocess.run(['/bin/sh', '-c', helpers + script])
    assert (result.returncode == 0) == (not sync_fails)
    assert log.read_text().splitlines() == ['sync']

@pytest.mark.parametrize('scenario', [
    'manager_starts_helper', 'boot_spawns_manager', 'shell_needs_kill',
    'stubborn_shell', 'stubborn_native', 'stubborn_worker', 'ps_failure',
])
def test_shutdown_rescans_all_supervisors_before_service_workers(tmp_path, scenario):
    import shlex
    import sys

    state = tmp_path / 'processes.json'
    rows = {
        '12': '12 S service service: role=mdns nbns=disabled',
        '20': '20 S service /mnt/Flash/service run',
        '27': '27 S sh /bin/sh /mnt/Flash/boot.sh',
        '30': '30 S sh /bin/sh /mnt/Flash/manager.sh',
        '31': '31 S sh /bin/sh /mnt/Flash/watchdog.sh',
        '90': '90 S mDNSResponder /sbin/mDNSResponder -d',
        '91': '91 S afpserver /sbin/afpserver',
        '92': '92 S diskd /sbin/diskd -i lo0 -d local.',
        '93': '93 S service /sbin/service run',
        '94': '94 S sh sh -c /mnt/Flash/manager.sh',
        '95': '95 Z sh /bin/sh /mnt/Flash/manager.sh',
    }
    state.write_text(json.dumps({'rows': rows, 'signals': [], 'scans': 0}))
    tool = tmp_path / 'process-tool.py'
    tool.write_text(FAKE_PS + f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
scenario = {scenario!r}
d = json.loads(state.read_text())
if sys.argv[1] == 'ps':
    if scenario == 'ps_failure': sys.exit(1)
    if listing():
        d['scans'] += 1
    # Without stopping manager first, it can start a normal slow ACP query
    # between the worker snapshot and its absence check (the review repro).
    if scenario == 'manager_starts_helper' and d['scans'] >= 4 and '30' in d['rows']:
        d['rows']['33'] = '33 S service /mnt/Flash/service --print-samba-identity'
    status = ps(d['rows'].values())
    state.write_text(json.dumps(d))
    sys.exit(status)
else:
    sig, pid = sys.argv[-2:]
    d['signals'].append([sig, pid])
    # Model the child being forked just before the boot process exits. A
    # second live scan must catch that manager before any worker is stopped.
    if scenario == 'boot_spawns_manager' and pid == '27':
        d['rows']['32'] = '32 S sh /mnt/Flash/manager.sh'
        d['rows']['33'] = '33 S service /mnt/Memory/samba4/sbin/service --print-samba-identity'
    stubborn = {{'stubborn_shell':'30', 'stubborn_native':'20', 'stubborn_worker':'12'}}.get(scenario)
    if pid != stubborn and not (scenario == 'shell_needs_kill' and pid == '30' and sig == '-TERM'):
        d['rows'].pop(pid, None)
state.write_text(json.dumps(d))
''')
    script = render_stop_service_runtime(attempts=1, supervisor_seconds=2)
    script = script.replace('/bin/ps', shlex.join([sys.executable, str(tool), 'ps']))
    script = script.replace('/bin/kill', shlex.join([sys.executable, str(tool), 'kill']))
    script = script.replace('sleep 1', ':')
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)
    final = json.loads(state.read_text())
    signals = final['signals']
    ids = [pid for sig, pid in signals]
    # Apple owns these daemons. Neither their lifecycle nor the SSH command
    # containing a managed path may be mistaken for one of our launchers.
    assert all(pid not in ids for pid in ('90', '91', '92', '93', '94', '95'))
    if scenario == 'ps_failure':
        assert result.returncode != 0 and not signals
    elif scenario.startswith('stubborn_'):
        assert result.returncode != 0 and 'did not stop' in result.stderr
        if scenario != 'stubborn_worker':
            assert '12' not in ids
        assert not any(sig == '-9' and pid in ('12', '20') for sig, pid in signals)
    else:
        assert result.returncode == 0, result.stderr
        assert set(final['rows']) == {'90', '91', '92', '93', '94', '95'}
        assert all(ids.index(pid) < ids.index('12') for pid in ('20', '27', '30', '31'))
        if scenario == 'boot_spawns_manager':
            assert ids.index('32') < ids.index('12') and ids.index('32') < ids.index('33')
        if scenario == 'shell_needs_kill':
            assert [sig for sig, pid in signals if pid == '30'][:2] == ['-TERM', '-9']


def test_role_observation_ignores_one_shot_diagnostics_and_zombies():
    from timecapsulesmb.device.processes import service_role_processes
    rows = '\n'.join([
        ps_row(10, 'service', 'service: role=manager', ppid=1, stat='S'),
        ps_row(17, 'service', 'service: role=manager waiting=hostname', ppid=1, stat='S'),
        # The title a device shows now: its start, then any wait and stuck list.
        ps_row(18, 'service', 'service: role=manager started=12 waiting=hostname stuck=3166:smbd:biowait:600', ppid=1, stat='S'),
        ps_row(11, 'service', 'service: role=discovery nbns=ready', ppid=10, stat='S'),
        ps_row(12, 'service', '/mnt/Flash/service discovery --netbios-name NAS', ppid=10, stat='S'),
        ps_row(13, 'service', '/mnt/Flash/service --print-link-plan', ppid=10, stat='S'),
        ps_row(14, 'service', 'service: role=discovery nbns=ready', ppid=10, stat='Z'),
        ps_row(15, 'service', '/mnt/Flash/service telemetry --once role=discovery', ppid=10, stat='S'),
        ps_row(16, 'service', 'service: role=telemetry --daemon', ppid=10, stat='S'),
    ])
    assert [process.pid for process in service_role_processes(rows, 'manager')] == [10, 17, 18]
    assert [process.pid for process in service_role_processes(rows, 'discovery')] == [11, 12]
    assert [process.pid for process in service_role_processes(rows, 'telemetry')] == [16]


@pytest.mark.parametrize('stuck_row,expected_label,manager_timeout', [
    ('30 S service service: role=manager', 'manager', True),
    # While Samba waits for the device hostname the title carries that state.
    ('35 S service service: role=manager waiting=hostname', 'manager', True),
    # The title a device shows now: started= always follows the role.
    ('36 S service service: role=manager started=12', 'manager', True),
    ('37 S service service: role=manager started=12 waiting=hostname stuck=3166:smbd:biowait:600', 'manager', True),
    ('31 S service /mnt/Flash/service manager', 'manager', True),
    ('32 S sh /bin/sh /mnt/Flash/manager.sh', 'manager', True),
    ('33 S service service: role=discovery nbns=ready', 'service', False),
    ('34 S sh /bin/sh /mnt/Flash/boot.sh', 'boot', False),
])
def test_stuck_process_is_named_so_deploy_can_classify_a_stuck_manager(tmp_path, stuck_row, expected_label, manager_timeout):
    # Deploy maps "process manager did not stop" to its manager_stop_timeout
    # error. That text must come from the stop script deploy actually runs.
    import shlex
    import sys
    from timecapsulesmb.services.deploy import _manager_stop_timed_out

    tool = tmp_path / 'tool.py'
    tool.write_text(FAKE_PS + f'''
if sys.argv[1] == 'ps':
    sys.exit(ps([{stuck_row!r}]))
''')
    script = render_stop_service_runtime(attempts=0, supervisor_seconds=0)
    script = script.replace('/bin/ps', shlex.join([sys.executable, str(tool), 'ps']))
    script = script.replace('/bin/kill', shlex.join([sys.executable, str(tool), 'kill']))
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)

    assert result.returncode == 1
    assert f'process {expected_label} did not stop' in result.stderr
    assert _manager_stop_timed_out(RuntimeError(result.stderr)) is manager_timeout


def _run_manager_stop(tmp_path, *, manager_scans, supervisor_seconds=None, extra_rows=()):
    """Run the stop script against a manager that ignores TERM until it has
    been seen by `manager_scans` process scans (None: it never exits)."""
    import shlex
    import sys
    from timecapsulesmb.device.processes import STUCK_PROCESS_PS_COMMAND

    state = tmp_path / 'manager.json'
    state.write_text(json.dumps({'scans': 0, 'sleeps': 0}))
    tool = tmp_path / 'manager-tool.py'
    tool.write_text(FAKE_PS + f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
d = json.loads(state.read_text())
limit = {manager_scans!r}
if sys.argv[1] == 'ps':
    if listing():
        d['scans'] += 1
    ps(['30 S service service: role=manager'] if limit is None or d['scans'] <= limit else [])
elif sys.argv[1] == 'stuck-ps':
    rows = ['30 1 30 S 0 select 0:00.00 service']
    rows += {list(extra_rows)!r}
    print('\\n'.join(rows))
elif sys.argv[1] == 'sleep':
    d['sleeps'] += 1
state.write_text(json.dumps(d))
''')
    kwargs = {} if supervisor_seconds is None else {'supervisor_seconds': supervisor_seconds}
    script = render_stop_service_runtime(attempts=0, **kwargs)
    fake = lambda name: shlex.join([sys.executable, str(tool), name])
    # Only report_stuck's listing: ps_rows lists the same columns.
    stuck_listing = STUCK_PROCESS_PS_COMMAND + ' 2>/dev/null | while'
    assert stuck_listing in script
    script = script.replace(stuck_listing, fake('stuck-ps') + ' 2>/dev/null | while')
    script = script.replace('/bin/ps', fake('ps'))
    script = script.replace('/bin/kill', fake('kill'))
    script = script.replace('sleep 1', fake('sleep'))
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)
    return result, json.loads(state.read_text())


def test_supervisor_stop_waits_out_the_managers_child_grace(tmp_path):
    # The manager exits only after its children's TERM grace, SIGKILL and reap.
    # A manager that needs slightly more than that grace is healthy, not stuck.
    from timecapsulesmb.device.processes import MANAGER_CHILD_GRACE_SECONDS, SUPERVISOR_STOP_SECONDS

    result, state = _run_manager_stop(tmp_path, manager_scans=MANAGER_CHILD_GRACE_SECONDS + 3)

    assert result.returncode == 0, result.stderr
    assert 'did not stop' not in result.stderr
    assert state['sleeps'] == MANAGER_CHILD_GRACE_SECONDS + 3
    assert SUPERVISOR_STOP_SECONDS > MANAGER_CHILD_GRACE_SECONDS + 3


def test_supervisor_budget_outlasts_the_child_grace_the_manager_is_built_with(tmp_path):
    # The stop script cannot ask the manager for its grace, so the two are
    # tied here. The probe is compiled against the production header without
    # a -DTC_CHILD_GRACE_MS override (the host tests shorten it; the device
    # build does not), so it prints what the shipped manager waits.
    from tests.native.build import ROOT, compile_modules
    from timecapsulesmb.device.processes import MANAGER_CHILD_GRACE_SECONDS, SUPERVISOR_STOP_SECONDS

    probe = tmp_path / 'grace.c'
    probe.write_text('#include <stdio.h>\n#include "common/process.h"\n'
                     'int main(void) { printf("%d\\n", TC_CHILD_GRACE_MS); return 0; }\n')
    binary = compile_modules(tmp_path / 'grace', (), flags=('-I', str(ROOT / 'build/native')), extra_sources=(probe,))
    grace_ms = int(subprocess.run([str(binary)], capture_output=True, text=True, check=True).stdout)

    assert grace_ms == MANAGER_CHILD_GRACE_SECONDS * 1000
    # After the grace the manager still sends SIGKILL, reaps and exits.
    assert SUPERVISOR_STOP_SECONDS >= MANAGER_CHILD_GRACE_SECONDS + 5


def test_supervisor_stop_gives_up_after_its_budget_and_reports_what_is_stuck(tmp_path):
    # A manager that never exits still fails, after a bounded wait, and the
    # error shows what it and its children are blocked on. Unrelated
    # processes stay out of the report.
    result, state = _run_manager_stop(
        tmp_path,
        manager_scans=None,
        supervisor_seconds=3,
        extra_rows=['41 30 41 D 127 biowai 0:00.00 smbd', '42 41 41 S 0 select 0:00.00 smbd',
                    '50 1 50 D 127 biowai 0:00.00 other'],
    )

    assert result.returncode == 1
    assert state['sleeps'] == 3
    lines = result.stderr.splitlines()
    assert 'process manager did not stop; retry after its active work finishes' in lines
    assert 'still running: pid=30 ppid=1 stat=S wchan=select comm=service' in lines
    assert 'still running: pid=41 ppid=30 stat=D wchan=biowai comm=smbd' in lines
    assert not any('pid=42' in line or 'pid=50' in line for line in lines)


FSCK_OTHER_MOUNTS = [
    '/dev/md0a on / type ffs (local)',
    '/dev/dk20 on /Volumes/dk20 type hfs (local)',
]


def _run_fsck_script(tmp_path, *, stubborn=None, fsck_rc=0,
                     mounts=None, umount_unmounts=True, mount_fails=False,
                     extra_rows=None, ps_fails=False, script=None, drvctl=''):
    """Run the real fsck script (or `script`, such as Wipe Internal Disk's)
    against fake ps/kill/umount/mount/fsck/drvctl/dd tools. A dd call is
    logged with the pids still running when it wrote.

    Returns (result, logged tool calls, pids still running, mounts left).
    """
    import shlex
    import sys
    from timecapsulesmb.services.maintenance import build_remote_fsck_script

    if mounts is None:
        mounts = ['/dev/dk2 on /Volumes/dk2 type hfs (local)'] + FSCK_OTHER_MOUNTS
    state = tmp_path / 'rows.json'
    rows = {
        '30': '30 S service service: role=manager',
        '31': '31 S service service: role=discovery nbns=ready',
        '40': '40 S smbd /mnt/Memory/samba4/sbin/smbd -F',
        '41': '41 S afpserver /sbin/afpserver',
        '42': '42 S wcifsfs /sbin/wcifsfs',
        '90': '90 S mDNSResponder /sbin/mDNSResponder -d',
        **(extra_rows or {}),
    }
    state.write_text(json.dumps({'rows': rows, 'mounts': mounts}))
    log = tmp_path / 'log'
    tool = tmp_path / 'tool.py'
    tool.write_text(FAKE_PS + f"""
import json, sys
from pathlib import Path
state = Path({str(state)!r})
data = json.loads(state.read_text())
rows = data['rows']
cmd = sys.argv[1]
status = 0
def drop(pid):
    if pid != {stubborn!r}:
        rows.pop(pid, None)
if cmd == 'ps' and {ps_fails!r}:
    sys.exit(1)
if cmd == 'ps':
    status = ps(rows.values())
elif cmd == 'kill':
    drop(sys.argv[-1])
elif cmd == 'pkill':
    name = sys.argv[-1].strip('^$')
    for pid, row in list(rows.items()):
        if row.split()[2] == name:
            drop(pid)
elif cmd == 'mount':
    if {mount_fails!r}:
        sys.exit(1)
    print('\\n'.join(data['mounts']))
elif cmd == 'drvctl':
    sys.stdout.write({drvctl!r})
elif cmd == 'dd':
    with open({str(log)!r}, 'a') as out:
        out.write('dd ' + ' '.join(sys.argv[2:]) + ' running=' + ','.join(sorted(rows)) + '\\n')
else:
    with open({str(log)!r}, 'a') as out:
        out.write(' '.join(sys.argv[1:]) + '\\n')
state.write_text(json.dumps(data))
if cmd == 'umount':
    target = sys.argv[-1]
    kept = [m for m in data['mounts'] if ' on ' + target + ' ' not in m]
    if len(kept) == len(data['mounts']):
        print('umount: ' + target + ': not currently mounted', file=sys.stderr)
        sys.exit(1)
    if not {umount_unmounts!r}:
        print('umount: ' + target + ': Device busy', file=sys.stderr)
        sys.exit(1)
    data['mounts'] = kept
    state.write_text(json.dumps(data))
if cmd == 'fsck_hfs':
    sys.exit({fsck_rc})
sys.exit(status)
""")
    fake = lambda name: shlex.join([sys.executable, str(tool), name])
    if script is None:
        script = build_remote_fsck_script('/dev/dk2', '/Volumes/dk2')
    # The host requests the reboot after the script reports; any reboot tool
    # the script ran itself would show up in the logged calls.
    for real, name in (
        ('/usr/bin/acp', 'acp'),
        ('/sbin/shutdown', 'shutdown'),
        ('/sbin/reboot', 'reboot'),
        ('/bin/ps', 'ps'),
        ('/usr/bin/pkill', 'pkill'),
        ('/bin/kill', 'kill'),
        ('/sbin/umount', 'umount'),
        ('/sbin/mount', 'mount'),
        ('/sbin/fsck_hfs', 'fsck_hfs'),
        ('/sbin/drvctl', 'drvctl'),
        ('/bin/dd', 'dd'),
    ):
        script = script.replace(real, fake(name))
    script = script.replace('sleep 1', ':')
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    data = json.loads(state.read_text())
    return result, calls, set(data['rows']), data['mounts']


@pytest.mark.parametrize(('stubborn', 'fsck_rc'), [
    (None, 0),
    (None, 8),
    ('30', 0),
    ('41', 0),
])
def test_fsck_repairs_only_after_every_managed_process_stopped(tmp_path, stubborn, fsck_rc):
    # The native manager restarts smbd and can remount the volume, so fsck
    # must stop it (and everything else deploy stops) before unmounting, and
    # must not touch the disk at all if anything is still running. fsck's own
    # status must survive to the caller, which then requests the reboot.
    from timecapsulesmb.services.maintenance import fsck_exit_status

    result, calls, remaining, mounts = _run_fsck_script(
        tmp_path, stubborn=stubborn, fsck_rc=fsck_rc)

    if stubborn is None:
        assert result.returncode == fsck_rc, result.stderr
        assert fsck_exit_status(result.stdout) == fsck_rc
        assert remaining == {'90'}  # Apple's mDNSResponder is never ours to stop.
        assert calls == ['umount -f /Volumes/dk2', 'fsck_hfs -fy /dev/dk2']
        assert mounts == FSCK_OTHER_MOUNTS
    else:
        assert result.returncode == 1
        assert 'did not stop' in result.stderr
        assert stubborn in remaining
        # No status line (so the host will not reboot), and nothing touched the disk.
        assert fsck_exit_status(result.stdout) is None
        assert calls == []


@pytest.mark.parametrize('stubborn', [None, '30', '41'])
def test_wipe_zeroes_only_after_every_managed_process_and_afpserver_stopped(tmp_path, stubborn):
    # Wipe Internal Disk starts as fsck does (render_disk_maintenance_preamble):
    # nothing of ours or afpserver may write the disk while its ends are
    # zeroed. It never unmounts: Apple's erase does that itself.
    from tests.test_wipe_disk import TWO_TB, drvctl_output
    from timecapsulesmb.device import wipe
    from timecapsulesmb.services.maintenance import render_disk_maintenance_preamble

    offsets = wipe.wipe_offsets(TWO_TB)
    script = '\n'.join([*render_disk_maintenance_preamble(), wipe.render_wipe_script('sd0', 2, TWO_TB, offsets)])
    result, calls, remaining, mounts = _run_fsck_script(
        tmp_path, stubborn=stubborn, script=script, drvctl=drvctl_output('0x200', '0xe8e088b0'))

    if stubborn is None:
        assert result.returncode == 0, result.stderr
        assert remaining == {'90'}
        assert calls == [
            f'dd if=/dev/zero of=/dev/rsd0c bs={offsets.block_size} count={offsets.head_count} running=90',
            f'dd if=/dev/zero of=/dev/rsd0c bs={offsets.block_size} seek={offsets.tail_seek} '
            f'count={offsets.tail_count} running=90',
        ]
    else:
        # Something still running: nothing is written, and without the
        # script's lines the host reports the stop as failed.
        assert result.returncode == 1
        assert stubborn in remaining
        assert calls == []
        assert wipe.WIPE_LINE_PREFIX not in result.stdout
    assert mounts == ['/dev/dk2 on /Volumes/dk2 type hfs (local)'] + FSCK_OTHER_MOUNTS


@pytest.mark.parametrize(('case', 'mounts', 'umount_unmounts', 'mount_fails'), [
    # umount failed and the volume is still mounted where it was.
    ('busy', None, False, False),
    # Something remounted the device elsewhere; umount of the old path fails.
    ('moved', ['/dev/dk2 on /Volumes/other type hfs (local)'] + FSCK_OTHER_MOUNTS, True, False),
    # The mount table cannot be read, so unmounting cannot be confirmed.
    ('no-table', None, True, True),
])
def test_fsck_never_repairs_a_volume_that_is_still_mounted(tmp_path, case, mounts, umount_unmounts, mount_fails):
    from timecapsulesmb.services.maintenance import (
        FSCK_NOT_UNMOUNTED_MESSAGE,
        fsck_exit_status,
        fsck_failure_message,
    )

    result, calls, remaining, _ = _run_fsck_script(
        tmp_path, mounts=mounts,
        umount_unmounts=umount_unmounts, mount_fails=mount_fails)

    assert result.returncode == 1
    assert remaining == {'90'}
    # fsck never ran, and without a status line the host does not reboot.
    assert calls == ['umount -f /Volumes/dk2']
    status = fsck_exit_status(result.stdout)
    assert status is None
    assert fsck_failure_message(status, result.stdout) == FSCK_NOT_UNMOUNTED_MESSAGE
    if case == 'busy':
        # umount's own diagnostics now reach the output.
        assert 'Device busy' in result.stdout


def test_fsck_repairs_a_volume_apple_already_unmounted(tmp_path):
    # Apple unmounts idle disks to save power: umount then fails with "not
    # currently mounted", which is exactly the state fsck needs.
    from timecapsulesmb.services.maintenance import fsck_exit_status

    result, calls, _, _ = _run_fsck_script(tmp_path, mounts=FSCK_OTHER_MOUNTS)

    assert result.returncode == 0, result.stderr
    assert fsck_exit_status(result.stdout) == 0
    assert 'not currently mounted' in result.stdout
    assert calls == ['umount -f /Volumes/dk2', 'fsck_hfs -fy /dev/dk2']


# Ubuntu CI's /bin/sh is dash; macOS has both.
SHELLS = [shell for shell in ('/bin/sh', '/bin/dash') if os.path.exists(shell)]
STOP_LOOP_SLEEP = '[ "$attempt" -ge "$limit" ] || sleep 1'


def _stop_with_fake_ps(tmp_path, processes, *, shell='/bin/sh', on_loop_sleep=lambda fake: ':'):
    """Run the stop script against tests/device_ps. on_loop_sleep(fake) is
    the command run in place of the wait between passes; ps_rows' own second
    look waits for nothing."""
    fake = FakePs(tmp_path, processes)
    script = fake.install(render_stop_service_runtime(attempts=1, supervisor_seconds=1))
    assert STOP_LOOP_SLEEP in script
    script = script.replace(STOP_LOOP_SLEEP, STOP_LOOP_SLEEP.replace('sleep 1', on_loop_sleep(fake)))
    script = script.replace('sleep 1', ':')
    return subprocess.run([shell, '-c', script], capture_output=True, text=True), fake


@pytest.mark.parametrize('shell', SHELLS)
def test_stop_waits_for_an_unreadable_shell_or_service_but_never_signals_it(tmp_path, shell):
    # A process whose command line could not be read may be ours or someone
    # else's: the stop waits for it, then fails naming it, and leaves it alone.
    from timecapsulesmb.services.deploy import _manager_stop_timed_out

    result, fake = _stop_with_fake_ps(tmp_path, [
        process('20 S service /mnt/Flash/service run'),
        process('40 D sh /bin/sh /mnt/Flash/boot.sh', stat_later='D'),
        process('41 S service service: role=discovery', argv='unreadable'),
        process('42 D smbd /mnt/Memory/samba4/sbin/smbd -F'),
    ], shell=shell)

    assert result.returncode == 1
    lines = result.stderr.splitlines()
    assert 'process sh did not show its command line (pid 40); retry after its active work finishes' in lines
    assert 'process service did not show its command line (pid 41); retry after its active work finishes' in lines
    assert [pid for _signal, pid in fake.signals] == ['20']
    assert '40' not in fake.command_reads()
    # A service asleep in the kernel throughout may be the manager: deploy
    # gives the stuck-service advice.
    assert _manager_stop_timed_out(RuntimeError(result.stderr))


def test_an_unreadable_shell_alone_is_not_a_stuck_manager(tmp_path):
    from timecapsulesmb.services.deploy import _manager_stop_timed_out

    result, _ = _stop_with_fake_ps(tmp_path, [process('40 D sh /bin/sh /mnt/Flash/boot.sh', stat_later='D')])

    assert result.returncode == 1
    assert result.stderr.splitlines()[0] == (
        'process sh did not show its command line (pid 40); retry after its active work finishes'
    )
    assert not _manager_stop_timed_out(RuntimeError(result.stderr))


@pytest.mark.parametrize('shell', SHELLS)
def test_stop_finishes_once_an_unreadable_process_exits(tmp_path, shell):
    # Seen unreadable on the first pass and waited for; it exits during the
    # wait between passes, and the next pass finds nothing left.
    result, fake = _stop_with_fake_ps(tmp_path, [
        process('20 S service /mnt/Flash/service run'),
        process('40 D sh /bin/sh /mnt/Flash/boot.sh', stat_later='D'),
    ], shell=shell, on_loop_sleep=lambda fake: f"{fake.command('exit')} 40")

    assert result.returncode == 0, result.stderr
    assert fake.pids == set()
    assert [pid for _signal, pid in fake.signals] == ['20']
    assert ['-o', 'stat=', '-p', '40'] in fake.calls
    assert '40' not in fake.command_reads()


def test_stop_signals_a_process_whose_disk_wait_ended_by_the_second_look(tmp_path):
    result, fake = _stop_with_fake_ps(tmp_path, [process('20 D service service: role=manager', stat_later='S')])

    assert result.returncode == 0, result.stderr
    assert fake.signals == [['-TERM', '20']]


@pytest.mark.parametrize('shell', SHELLS)
@pytest.mark.parametrize('row', [
    '17 S sh /bin/sh /mnt/Flash/migrate.sh',
    '17 S service service: role=job storage',
    '17 S sh sh -c true',
])
def test_idle_jobs_wait_for_an_unreadable_shell_or_service(tmp_path, row, shell):
    # It may be a migration or a job: cleanup waits for it, whatever it is.
    fake = FakePs(tmp_path, [process(row, argv='unreadable')])
    result = subprocess.run(
        [shell, '-c', fake.install(render_wait_for_idle_jobs(attempts=0))], capture_output=True, text=True
    )

    assert result.returncode == 1
    assert 'still active' in result.stderr


@pytest.mark.parametrize('shell', SHELLS)
def test_idle_jobs_never_expand_a_shells_arguments(tmp_path, shell):
    # Expanded in a directory holding a file named sh, "*" would become "sh"
    # and the next word would pass for the script that shell runs.
    work = tmp_path / 'cwd'
    work.mkdir()
    (work / 'sh').write_text('')
    fake = FakePs(tmp_path, [process('17 S sh * /mnt/Flash/migrate.sh')])
    result = subprocess.run(
        [shell, '-c', fake.install(render_wait_for_idle_jobs(attempts=0))], capture_output=True, text=True, cwd=work,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('shell', SHELLS)
def test_stop_idle_jobs_never_signals_an_unreadable_process(tmp_path, shell):
    from timecapsulesmb.device.processes import render_stop_idle_jobs

    fake = FakePs(tmp_path, [
        process('17 S sh /bin/sh /mnt/Flash/migrate.sh', argv='unreadable'),
        process('18 S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate multi copy'),
    ])
    script = fake.install(render_stop_idle_jobs(attempts=1)).replace('sleep 1', ':')
    result = subprocess.run([shell, '-c', script], capture_output=True, text=True)

    assert result.returncode == 1
    assert result.stderr.splitlines() == ['process sh did not show its command line (pid 17)']
    assert fake.signals == [['-TERM', '18']]
    assert fake.pids == {'17'}


def test_fsck_touches_nothing_while_an_earlier_fsck_hfs_still_runs(tmp_path):
    # A repair whose session dropped keeps running; a second fsck_hfs on the
    # same disk would repair it under the first. Nothing is stopped either.
    from timecapsulesmb.services.maintenance import (
        FSCK_ALREADY_RUNNING_LINE,
        FSCK_ALREADY_RUNNING_MESSAGE,
        fsck_exit_status,
        fsck_failure_message,
    )

    result, calls, remaining, mounts = _run_fsck_script(
        tmp_path, mounts=FSCK_OTHER_MOUNTS, extra_rows={'77': '77 D fsck_hfs /sbin/fsck_hfs -fy /dev/dk2'})

    assert result.returncode == 1
    assert result.stdout.splitlines() == [FSCK_ALREADY_RUNNING_LINE]
    assert calls == []
    assert {'30', '40', '41', '77'} <= remaining
    assert mounts == FSCK_OTHER_MOUNTS
    assert fsck_exit_status(result.stdout) is None
    assert fsck_failure_message(None, result.stdout) == FSCK_ALREADY_RUNNING_MESSAGE


def test_fsck_ignores_a_finished_fsck_hfs_left_as_a_zombie(tmp_path):
    result, calls, _remaining, _ = _run_fsck_script(
        tmp_path, extra_rows={'77': '77 Z fsck_hfs fsck_hfs'})

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ['umount -f /Volumes/dk2', 'fsck_hfs -fy /dev/dk2']


def test_fsck_refuses_when_it_cannot_tell_whether_fsck_hfs_runs(tmp_path):
    from timecapsulesmb.services.maintenance import FSCK_ALREADY_RUNNING_LINE

    result, calls, remaining, _ = _run_fsck_script(tmp_path, ps_fails=True)

    assert result.returncode == 1
    assert FSCK_ALREADY_RUNNING_LINE in result.stdout.splitlines()
    assert calls == []
    assert '30' in remaining
