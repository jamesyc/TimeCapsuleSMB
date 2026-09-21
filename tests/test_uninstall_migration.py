"""Uninstall executes the real idle guard before deleting payloads or rebooting."""
from types import SimpleNamespace
import shlex
import subprocess
import sys

import pytest

from timecapsulesmb.app.ops import maintenance
from timecapsulesmb.deploy import commands, executor
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.device.processes import render_wait_for_idle_jobs


@pytest.mark.parametrize('scenario', ['active_short', 'active_long', 'finishes', 'zombie', 'unrelated', 'ps_failure'])
@pytest.mark.parametrize('no_wait', [False, True])
def test_uninstall_respects_standalone_migration(monkeypatch, tmp_path, scenario, no_wait):
    rows = {
        'active_short': 'S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate',
        'active_long': 'S xattr-hfs-migrate /mnt/Memory/tc-xattr-hfs-migrate',
        'finishes': 'S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate',
        'zombie': 'Z tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate',
        'unrelated': 'S afpserver /sbin/afpserver',
        'ps_failure': '',
    }
    ps = tmp_path/'ps.py'
    counter = tmp_path/'scans'
    ps.write_text(f'''from pathlib import Path
import sys
p=Path({str(counter)!r})
n=int(p.read_text()) if p.exists() else 0
p.write_text(str(n+1))
if {scenario!r} == 'ps_failure': sys.exit(1)
if {scenario!r} != 'finishes' or n == 0: print({rows[scenario]!r})
''')
    script = render_wait_for_idle_jobs(attempts=2).replace(
        '/bin/ps axww -o stat= -o ucomm= -o command=',
        shlex.join([sys.executable, str(ps)]),
    ).replace('sleep 1', ':')
    monkeypatch.setattr(commands, 'render_wait_for_idle_jobs', lambda: script)
    guard = commands.render_remote_action(commands.WaitForIdleJobsAction())
    calls = []
    def ssh(connection, command):
        calls.append(command)
        if command == guard:
            result = subprocess.run(shlex.split(command), capture_output=True, text=True)
            if result.returncode: raise RuntimeError(result.stderr or 'process inspection failed')
    monkeypatch.setattr(executor, 'run_ssh', ssh)
    monkeypatch.setattr(maintenance, 'load_request_config', lambda *a: {})
    monkeypatch.setattr(maintenance, 'resolve_request_connection', lambda *a, **kw: SimpleNamespace(host='fixture'))
    monkeypatch.setattr(maintenance, 'require_confirmation', lambda *a: None)
    monkeypatch.setattr(maintenance.storage_service, 'mount_mast_volumes_with_diagnostics',
                        lambda *a, **kw: [SimpleNamespace(volume_root='/Volumes/dk2')])
    reboot = []
    monkeypatch.setattr(maintenance, 'request_reboot', lambda *a, **kw: reboot.append('request'))
    monkeypatch.setattr(maintenance, 'request_reboot_and_wait', lambda *a, **kw: reboot.append('wait'))
    monkeypatch.setattr(maintenance, 'verify_post_uninstall', lambda *a: True)
    monkeypatch.setattr(maintenance, 'render_post_uninstall_verification', lambda *a: [])
    context = SimpleNamespace(stage=lambda *a: None, log=lambda *a: None, to_operation_callbacks=lambda: None)
    busy = scenario in ('active_short', 'active_long', 'ps_failure')
    if busy:
        with pytest.raises(RuntimeError):
            maintenance.uninstall_operation({'no_wait': no_wait}, context)
        assert not any(c.startswith('rm -rf ') for c in calls)
        assert not reboot
    else:
        maintenance.uninstall_operation({'no_wait': no_wait}, context)
        first_remove = next(i for i,c in enumerate(calls) if c.startswith('rm -rf '))
        assert calls.index(guard) < first_remove
        assert reboot == ['request' if no_wait else 'wait']
    assert int(counter.read_text()) == (3 if scenario.startswith('active') else 2 if scenario == 'finishes' else 1)


def test_uninstall_guard_precedes_every_payload_removal():
    plan = build_uninstall_plan('fixture', ['/Volumes/dk2', '/Volumes/dk3'],
                                ['/Volumes/dk2/.samba4', '/Volumes/dk3/.samba4'])
    guard = plan.remote_actions.index(commands.WaitForIdleJobsAction())
    assert all(guard < i for i,a in enumerate(plan.remote_actions) if isinstance(a, commands.RemovePathAction))
