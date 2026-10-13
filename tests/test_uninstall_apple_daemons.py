"""Uninstall puts Apple's /sbin/diskd and /sbin/wcifsnd back in place of boot.sh's guards."""
import os
import shlex
import subprocess

import pytest

from timecapsulesmb.deploy import commands
from timecapsulesmb.deploy.planner import build_uninstall_plan

GUARD = '#!/bin/sh\n# TimeCapsuleSMB guard\nexit 0\n'


@pytest.fixture
def sbin(tmp_path):
    """A RAM-root /sbin with Apple's multi-call binary, and the rendered restore run against it."""
    root = tmp_path / 'sbin'
    root.mkdir()
    (root / 'ACPd').write_bytes(b'apple multi-call')
    command = commands.render_remote_action(commands.RestoreAppleDaemonsAction())
    script = shlex.split(command)[2].replace('/sbin/', f'{root}/')

    def run():
        return subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=10)
    return root, run


def test_guarded_daemons_become_apple_links_again(sbin):
    root, run = sbin
    for name in commands.GUARDED_APPLE_DAEMONS:
        (root / name).write_text(GUARD)
    result = run()
    assert result.returncode == 0, result.stderr
    acpd = (root / 'ACPd').stat().st_ino
    assert all((root / name).stat().st_ino == acpd for name in ('diskd', 'wcifsnd'))
    assert sorted(path.name for path in root.iterdir()) == ['ACPd', 'diskd', 'wcifsnd']
    # A rerun (an interrupted uninstall run again) changes nothing.
    assert run().returncode == 0
    assert all((root / name).stat().st_ino == acpd for name in ('diskd', 'wcifsnd'))


def test_device_without_guards_keeps_apple_links(sbin):
    root, run = sbin
    # A stock device, or a release before the guards.
    for name in ('diskd', 'wcifsnd'):
        os.link(root / 'ACPd', root / name)
    assert run().returncode == 0
    assert all((root / name).stat().st_ino == (root / 'ACPd').stat().st_ino for name in ('diskd', 'wcifsnd'))
    # Nothing is linked or renamed, so no temporary link is left behind.
    assert sorted(path.name for path in root.iterdir()) == ['ACPd', 'diskd', 'wcifsnd']
    assert (root / 'ACPd').stat().st_nlink == 3


def test_unwritable_sbin_fails_and_keeps_the_guard(sbin):
    root, run = sbin
    (root / 'diskd').write_text(GUARD)
    root.chmod(0o555)
    try:
        result = run()
    finally:
        root.chmod(0o755)
    assert result.returncode != 0
    assert f'cannot restore Apple {root}/diskd' in result.stderr
    assert (root / 'diskd').read_text() == GUARD
    assert sorted(path.name for path in root.iterdir()) == ['ACPd', 'diskd']


@pytest.mark.parametrize('reboot', [True, False])
def test_uninstall_restores_after_stopping_the_runtime_and_before_removing_it(reboot):
    plan = build_uninstall_plan('fixture', ['/Volumes/dk2'], ['/Volumes/dk2/.samba4'],
                                reboot_after_uninstall=reboot)
    actions = plan.remote_actions
    restore = actions.index(commands.RestoreAppleDaemonsAction())
    assert actions.index(commands.StopServiceRuntimeAction()) < restore
    assert actions.index(commands.StopProcessAction('wcifsnd')) < restore
    assert all(restore < i for i, action in enumerate(actions) if isinstance(action, commands.RemovePathsAction))
    assert commands.remote_action_to_jsonable(actions[restore]) == {
        'kind': 'restore_apple_daemons', 'args': ['diskd', 'wcifsnd']}
