"""Host-packaged shutdown must work even when every old script is missing."""
import json
import subprocess

import pytest

from timecapsulesmb.device.processes import render_stop_service_runtime, render_wait_for_idle_jobs


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
    helper.write_text(f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
rows = json.loads(state.read_text())
if sys.argv[1] == 'ps':
    print('\\n'.join(rows))
else:
    pid = sys.argv[-1]
    with open({str(log)!r}, 'a') as out: out.write(pid + '\\n')
    if not {stubborn!r}:
        state.write_text(json.dumps([r for r in rows if r.split()[0] != pid]))
''')
    import sys
    script = render_stop_service_runtime(attempts=0, supervisor_seconds=0)
    script = script.replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', f'{sys.executable} {helper} ps')
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


@pytest.mark.parametrize('comm,state,expected', [
    ('tc-xattr-hfs-mi', 'S', 1), ('xattr-hfs-migrate', 'S', 1),
    ('debug', 'S', 1), ('telemetry', 'S', 1), ('heartbeat', 'S', 1),
    ('tc-xattr-hfs-mi', 'Z', 0), ('afpserver', 'S', 0),
    ('sh /bin/sh /mnt/Flash/migrate.sh', 'S', 1),
    ('sh /mnt/Flash/xattr-migrate-wrapper.sh', 'S', 1),
    ('sh sh -c /mnt/Flash/migrate.sh', 'S', 0),
    ('service service: role=telemetry --daemon', 'S', 1),
    ('service service: role=job storage', 'S', 1),
    ('service service: role=discovery nbns=ready', 'S', 0),
])
def test_active_metadata_and_diagnostic_jobs_block_cleanup(comm, state, expected):
    script = render_wait_for_idle_jobs(attempts=0).replace(
        '/bin/ps axww -o stat= -o ucomm= -o command=', f"printf '%s\\n' '{state} {comm}'"
    )
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True)
    assert result.returncode == expected


@pytest.mark.parametrize('failed_sync', [0, 1, 2])
def test_flash_flush_waits_ten_seconds_and_propagates_each_failure(tmp_path, failed_sync):
    # Apple may reformat unclean Flash at boot, losing ACP settings and SSH
    # keys. Both syncs must succeed before the installer can request reboot.
    import shlex
    from timecapsulesmb.deploy.executor import FLUSH_REMOTE_FILESYSTEMS_COMMAND
    script = shlex.split(FLUSH_REMOTE_FILESYSTEMS_COMMAND)[2]
    script = script.replace('/bin/sync', 'test_sync').replace('/bin/sleep', 'test_sleep')
    log = tmp_path / 'flush-order'
    helpers = f'''
count=0
test_sync() {{
    count=$((count + 1))
    echo sync >> {shlex.quote(str(log))}
    [ "$count" != {failed_sync} ]
}}
test_sleep() {{ echo "sleep $1" >> {shlex.quote(str(log))}; }}
'''
    result = subprocess.run(['/bin/sh', '-c', helpers + script])
    assert (result.returncode == 0) == (failed_sync == 0)
    assert log.read_text().splitlines() == (['sync'] if failed_sync == 1 else ['sync', 'sleep 10', 'sync'])

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
    tool.write_text(f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
scenario = {scenario!r}
d = json.loads(state.read_text())
if sys.argv[1] == 'ps':
    if scenario == 'ps_failure': sys.exit(1)
    d['scans'] += 1
    # Without stopping manager first, it can start a normal slow ACP query
    # between the worker snapshot and its absence check (the review repro).
    if scenario == 'manager_starts_helper' and d['scans'] >= 4 and '30' in d['rows']:
        d['rows']['33'] = '33 S service /mnt/Flash/service --print-samba-identity'
    print('\\n'.join(d['rows'].values()))
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
    script = script.replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', shlex.join([sys.executable, str(tool), 'ps']))
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
    from timecapsulesmb.device.processes import service_role_lines
    rows = '\n'.join([
        '10 1 S 0:00 service service: role=manager',
        '17 1 S 0:00 service service: role=manager waiting=hostname',
        '11 10 S 0:00 service service: role=discovery nbns=ready',
        '12 10 S 0:00 service /mnt/Flash/service discovery --netbios-name NAS',
        '13 10 S 0:00 service /mnt/Flash/service --print-link-plan',
        '14 10 Z 0:00 service service: role=discovery nbns=ready',
        '15 10 S 0:00 service /mnt/Flash/service telemetry --once role=discovery',
        '16 10 S 0:00 service service: role=telemetry --daemon',
    ])
    assert [line.split()[0] for line in service_role_lines(rows,'manager')]==['10','17']
    assert [line.split()[0] for line in service_role_lines(rows,'discovery')]==['11','12']
    assert [line.split()[0] for line in service_role_lines(rows,'telemetry')]==['16']


@pytest.mark.parametrize('stuck_row,expected_label,manager_timeout', [
    ('30 S service service: role=manager', 'manager', True),
    # While Samba waits for the device hostname the title carries that state.
    ('35 S service service: role=manager waiting=hostname', 'manager', True),
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
    tool.write_text(f'''
import sys
if sys.argv[1] == 'ps':
    print({stuck_row!r})
''')
    script = render_stop_service_runtime(attempts=0, supervisor_seconds=0)
    script = script.replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', shlex.join([sys.executable, str(tool), 'ps']))
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
    tool.write_text(f'''
import json, sys
from pathlib import Path
state = Path({str(state)!r})
d = json.loads(state.read_text())
limit = {manager_scans!r}
if sys.argv[1] == 'ps':
    d['scans'] += 1
    if limit is None or d['scans'] <= limit:
        print('30 S service service: role=manager')
elif sys.argv[1] == 'stuck-ps':
    rows = ['30 1 S select service']
    rows += {list(extra_rows)!r}
    print('\\n'.join(rows))
elif sys.argv[1] == 'sleep':
    d['sleeps'] += 1
state.write_text(json.dumps(d))
''')
    kwargs = {} if supervisor_seconds is None else {'supervisor_seconds': supervisor_seconds}
    script = render_stop_service_runtime(attempts=0, **kwargs)
    fake = lambda name: shlex.join([sys.executable, str(tool), name])
    script = script.replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', fake('ps'))
    script = script.replace(STUCK_PROCESS_PS_COMMAND, fake('stuck-ps'))
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
        extra_rows=['41 30 D biowai smbd', '42 41 S select smbd', '50 1 D biowai other'],
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
                     mounts=None, umount_unmounts=True, mount_fails=False):
    """Run the real fsck script against fake ps/kill/umount/mount/fsck tools.

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
    }
    state.write_text(json.dumps({'rows': rows, 'mounts': mounts}))
    log = tmp_path / 'log'
    tool = tmp_path / 'tool.py'
    tool.write_text(f"""
import json, sys
from pathlib import Path
state = Path({str(state)!r})
data = json.loads(state.read_text())
rows = data['rows']
cmd = sys.argv[1]
def drop(pid):
    if pid != {stubborn!r}:
        rows.pop(pid, None)
if cmd == 'ps-full':
    print('\\n'.join(rows.values()))
elif cmd == 'ps-short':
    print('\\n'.join(' '.join(r.split()[1:]) for r in rows.values()))
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
""")
    fake = lambda name: shlex.join([sys.executable, str(tool), name])
    script = build_remote_fsck_script('/dev/dk2', '/Volumes/dk2')
    # The host requests the reboot after the script reports; any reboot tool
    # the script ran itself would show up in the logged calls.
    for real, name in (
        ('/usr/bin/acp', 'acp'),
        ('/sbin/shutdown', 'shutdown'),
        ('/sbin/reboot', 'reboot'),
        ('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', 'ps-full'),
        ('ps axww -o stat= -o ucomm= -o command=', 'ps-short'),
        ('/usr/bin/pkill', 'pkill'),
        ('/bin/kill', 'kill'),
        ('/sbin/umount', 'umount'),
        ('/sbin/mount', 'mount'),
        ('/sbin/fsck_hfs', 'fsck_hfs'),
    ):
        script = script.replace(real, fake(name))
    script = script.replace('sleep 1', ':').replace('/tmp/tcapsule-ps.', f'{tmp_path}/ps.')
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
