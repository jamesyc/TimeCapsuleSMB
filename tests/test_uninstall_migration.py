"""Uninstall runs the real job stopper before deleting payloads or rebooting.

Uninstall removes everything a migration or diagnostic job works on, so it
stops such a job (SIGTERM, then SIGKILL) instead of waiting for it.
"""
from types import SimpleNamespace
import json
import shlex
import subprocess
import sys

import pytest

from timecapsulesmb.app.ops import maintenance
from timecapsulesmb.deploy import commands, executor
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.device.processes import render_stop_idle_jobs
from timecapsulesmb.services import maintenance as maintenance_service
from timecapsulesmb.services.callbacks import OperationCallbacks

MIGRATOR = '41 S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate --log /Volumes/dk2/.samba4/logs/xattr-migration-copy.log multi copy'
# Each scenario: the ps rows, and the signals each pid dies on.
SCENARIOS = {
    'stops_on_term': ([MIGRATOR], {'41': ['TERM', 'KILL']}),
    'needs_kill': (['42 S xattr-hfs-migrate /mnt/Memory/tc-xattr-hfs-migrate multi cleanup'], {'42': ['KILL']}),
    'legacy_shell': (['43 S sh /bin/sh /mnt/Flash/migrate.sh'], {'43': ['TERM', 'KILL']}),
    'unkillable': ([MIGRATOR], {'41': []}),
    'zombie': (['44 Z tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate'], {}),
    'unrelated': (['45 S afpserver /sbin/afpserver'], {}),
    'ps_failure': (None, {}),
}


def fake_tools(tmp_path, scenario):
    """A ps that lists the live rows and a kill that ends a pid on its signals."""
    rows, dies_on = SCENARIOS[scenario]
    alive = tmp_path / 'alive.json'
    signals = tmp_path / 'signals'
    alive.write_text(json.dumps(rows or []))
    ps = tmp_path / 'ps.py'
    ps.write_text(f'''import json, sys
if {rows is None!r}: sys.exit(1)
print('\\n'.join(json.load(open({str(alive)!r}))))
''')
    kill = tmp_path / 'kill.py'
    kill.write_text(f'''import json, sys
signal, pid = sys.argv[1].lstrip('-'), sys.argv[2]
open({str(signals)!r}, 'a').write(f'{{signal}} {{pid}}\\n')
if signal in {dies_on!r}.get(pid, []):
    rows = [row for row in json.load(open({str(alive)!r})) if row.split()[0] != pid]
    json.dump(rows, open({str(alive)!r}, 'w'))
''')
    script = render_stop_idle_jobs(attempts=2).replace(
        '/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', shlex.join([sys.executable, str(ps)]),
    ).replace('/bin/kill', shlex.join([sys.executable, str(kill)])).replace('sleep 1', ':')
    return script, signals


@pytest.mark.parametrize('scenario', list(SCENARIOS))
@pytest.mark.parametrize('no_wait', [False, True])
def test_uninstall_stops_a_running_migration_before_removing_it(monkeypatch, tmp_path, scenario, no_wait):
    script, signals = fake_tools(tmp_path, scenario)
    monkeypatch.setattr(commands, 'render_stop_idle_jobs', lambda: script)
    guard = commands.render_remote_action(commands.StopIdleJobsAction())
    calls = []
    def ssh(connection, command, **_kwargs):
        calls.append(command)
        if command == guard:
            result = subprocess.run(shlex.split(command), capture_output=True, text=True)
            if result.returncode: raise RuntimeError(result.stderr or 'process inspection failed')
    monkeypatch.setattr(executor, 'run_ssh', ssh)
    monkeypatch.setattr(maintenance, 'load_request_config', lambda *a: {})
    monkeypatch.setattr(maintenance, 'resolve_request_connection', lambda *a, **kw: SimpleNamespace(host='fixture', password='pw'))
    monkeypatch.setattr(maintenance, 'require_confirmation', lambda *a: None)
    monkeypatch.setattr(maintenance_service.storage_service, 'mount_mast_volumes_with_diagnostics',
                        lambda *a, **kw: [SimpleNamespace(volume_root='/Volumes/dk2')])
    reboot = []
    monkeypatch.setattr(maintenance_service, 'reboot_device', lambda *a, wait, **kw: reboot.append('wait' if wait else 'request'))
    monkeypatch.setattr(maintenance_service, 'verify_post_uninstall', lambda *a: True)
    monkeypatch.setattr(maintenance_service, 'render_post_uninstall_verification', lambda *a: [])
    context = SimpleNamespace(stage=lambda *a: None, log=lambda *a: None, to_operation_callbacks=OperationCallbacks)

    if scenario in ('unkillable', 'ps_failure'):
        with pytest.raises(RuntimeError) as failure:
            maintenance.uninstall_operation({'no_wait': no_wait}, context)
        assert not any(c.startswith('rm -rf ') for c in calls)
        assert not reboot
        if scenario == 'unkillable':
            assert 'job tc-xattr-hfs-mi (pid 41) did not stop' in str(failure.value)
    else:
        maintenance.uninstall_operation({'no_wait': no_wait}, context)
        first_remove = next(i for i, c in enumerate(calls) if c.startswith('rm -rf '))
        assert calls.index(guard) < first_remove
        assert reboot == ['request' if no_wait else 'wait']
    sent = signals.read_text().split('\n')[:-1] if signals.exists() else []
    assert sent == {
        'stops_on_term': ['TERM 41'],
        'needs_kill': ['TERM 42', 'TERM 42', 'KILL 42'],
        'legacy_shell': ['TERM 43'],
        'unkillable': ['TERM 41', 'TERM 41', 'KILL 41'],
        'zombie': [], 'unrelated': [], 'ps_failure': [],
    }[scenario]


def test_uninstall_guard_precedes_every_payload_removal():
    plan = build_uninstall_plan('fixture', ['/Volumes/dk2', '/Volumes/dk3'],
                                ['/Volumes/dk2/.samba4', '/Volumes/dk3/.samba4'])
    guard = plan.remote_actions.index(commands.StopIdleJobsAction())
    assert commands.WaitForIdleJobsAction() not in plan.remote_actions
    assert all(guard < i for i,a in enumerate(plan.remote_actions) if isinstance(a, commands.RemovePathsAction))
