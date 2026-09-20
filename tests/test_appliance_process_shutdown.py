"""Host-packaged shutdown must work even when every old script is missing."""
import json
from pathlib import Path
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
        '27 S sh /bin/sh /mnt/Flash/boot.sh',
        '28 S sh sh /mnt/Flash/start-samba.sh',
        '29 S sh /bin/sh /mnt/Flash/rc.local',
        '21 S service /sbin/service run',
        '22 S sh sh -c /mnt/Flash/service run',
        '23 Z service /mnt/Flash/service run',
        '24 S mDNSResponder /sbin/mDNSResponder -d',
        '25 S diskd diskd -i lo0 -d local.',
        '26 S afpserver /sbin/afpserver',
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
    script = render_stop_service_runtime(attempts=0)
    script = script.replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', f'{sys.executable} {helper} ps')
    script = script.replace('/bin/kill', f'{sys.executable} {helper} kill')
    result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True)
    if stubborn:
        assert result.returncode == 1
        assert 'did not stop' in result.stderr
        assert log.read_text().splitlines() == ['20', '27', '28', '29']
    else:
        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines() == ['20', '27', '28', '29', '13', '14', '15', '12']
        assert json.loads(state.read_text()) == [r for r in rows if r.split()[0] not in {'12','13','14','15','20','27','28','29'}]


@pytest.mark.parametrize('comm,state,expected', [
    ('tc-xattr-hfs-mi', 'S', 1), ('xattr-hfs-migrate', 'S', 1),
    ('debug', 'S', 1), ('telemetry', 'S', 1), ('heartbeat', 'S', 1),
    ('tc-xattr-hfs-mi', 'Z', 0), ('afpserver', 'S', 0),
    ('sh /bin/sh /mnt/Flash/migrate.sh', 'S', 1),
    ('sh /mnt/Flash/xattr-migrate-wrapper.sh', 'S', 1),
    ('sh sh -c /mnt/Flash/migrate.sh', 'S', 0),
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
        d['rows']['33'] = '33 S service /mnt/Memory/samba4/sbin/service --print-smb-bind-interfaces --retain-policy'
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
    script = render_stop_service_runtime(attempts=1)
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
