from __future__ import annotations

import os
import shlex
import subprocess

import pytest

from timecapsulesmb.deploy.boot_assets import load_boot_asset_text
from timecapsulesmb.deploy.commands import StopTelemetryAction, render_remote_action, remote_action_to_jsonable
from timecapsulesmb.deploy.planner import build_uninstall_plan
from timecapsulesmb.deploy.executor import remote_uninstall_payload
from timecapsulesmb.transport.ssh import SshConnection
from unittest import mock


@pytest.fixture
def cleanup_rig(tmp_path):
    memory = tmp_path / 'Memory with spaces'; memory.mkdir()
    calls = tmp_path / 'calls'
    ps = tmp_path / 'ps'; mounts = tmp_path / 'mounts'
    ps.write_text(''); mounts.write_text('tmpfs on /mnt/Memory type tmpfs (local)\n')
    tools = {}
    for name, body in {
        'pkill': f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n',
        'ps': f'cat {shlex.quote(str(ps))}\nexit "${{TC_TEST_PS_RC:-0}}"\n',
        'mount': f'cat {shlex.quote(str(mounts))}\nexit "${{TC_TEST_MOUNT_RC:-0}}"\n',
    }.items():
        tool = tmp_path / (name + '-tool'); tool.write_text('#!/bin/sh\n' + body); tool.chmod(0o755)
        tools[name] = tool
    helper = memory / 'samba4/sbin/telemetry'; helper.parent.mkdir(parents=True)
    helper.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "${TC_TEST_VERSION:-3}" ;;\n--cleanup) exit "${TC_TEST_CLEANUP_RC:-0}" ;;\n*) exit 2 ;;\nesac\n')
    helper.chmod(0o755)

    def run(*, cleanup=False, **env):
        script = load_boot_asset_text('telemetry-cleanup.sh').replace('/mnt/Memory', str(memory))
        for source, name in [('/usr/bin/pkill', 'pkill'), ('/bin/ps', 'ps'), ('/sbin/mount', 'mount')]:
            # These are shell fixtures, not device executables. Direct exec
            # of newly created scripts stalls under concurrent macOS runs;
            # invoke the known interpreter while keeping real child processes.
            script = script.replace(source, '/bin/sh ' + shlex.quote(str(tools[name])))
        script = script.replace('"$cleanup_bin" --', '/bin/sh "$cleanup_bin" --')
        script += '\nsleep() { :; }\n' + ('tc_cleanup_telemetry_for_uninstall' if cleanup else 'tc_prepare_telemetry_reset')
        return subprocess.run(['/bin/sh', '-c', script], env={**os.environ, **env}, capture_output=True, text=True, timeout=5)
    return memory, calls, ps, mounts, helper, run


def legacy_tree(memory):
    legacy = memory / 'tc-telemetry'
    (legacy / 'job-abc/nested').mkdir(parents=True)
    (legacy / 'job-abc/nested/leftover').write_text('old scratch data')
    return legacy


def test_quiescent_legacy_tree_removed_and_fixed_files_preserved(cleanup_rig, tmp_path):
    memory, calls, ps, _, _, run = cleanup_rig
    legacy = legacy_tree(memory)
    outside = tmp_path / 'outside'; outside.write_text('preserve')
    (legacy / 'outside-link').symlink_to(outside)
    (memory / 'debug').write_text('active fixed-file work')
    ps.write_text('Z debug\nS sh\n')
    result = run()
    assert result.returncode == 0, result.stderr
    assert not legacy.exists()
    assert outside.read_text() == 'preserve'
    assert (memory / 'debug').read_text() == 'active fixed-file work'
    assert calls.read_text().splitlines() == ['^telemetry$']


@pytest.mark.parametrize('process', ['telemetry', 'debug', 'heartbeat'])
def test_live_legacy_work_defers_cleanup(cleanup_rig, process):
    memory, _, ps, _, _, run = cleanup_rig
    legacy = legacy_tree(memory); ps.write_text('S ' + process + '\n')
    result = run()
    assert result.returncode == 75
    assert 'still active' in result.stderr
    assert (legacy / 'job-abc/nested/leftover').exists()


@pytest.mark.parametrize('failure', ['ps', 'mount'])
def test_probe_failure_preserves_legacy_files(cleanup_rig, failure):
    memory, _, _, _, _, run = cleanup_rig
    legacy = legacy_tree(memory)
    result = run(**{f'TC_TEST_{failure.upper()}_RC': '1'})
    assert result.returncode == 1
    assert 'cannot inspect' in result.stderr
    assert legacy.exists()


def test_nested_mount_prevents_recursive_legacy_cleanup(cleanup_rig):
    memory, _, _, mounts, _, run = cleanup_rig
    legacy = legacy_tree(memory)
    mounts.write_text(f'/dev/dk2 on {legacy}/job-abc/nested type hfs\n')
    result = run()
    assert result.returncode == 1
    assert 'mounted legacy workspace' in result.stderr
    assert legacy.exists()


@pytest.mark.parametrize('status', ['1', '75'])
def test_uninstall_propagates_cleanup_failure_or_busy(cleanup_rig, status):
    memory, _, _, _, _, run = cleanup_rig
    (memory / 'debug').write_text('preserve')
    result = run(cleanup=True, TC_TEST_CLEANUP_RC=status)
    assert result.returncode == int(status)
    assert (memory / 'debug').read_text() == 'preserve'


def test_old_helper_cannot_delete_fixed_files_without_ownership(cleanup_rig):
    memory, _, _, _, _, run = cleanup_rig
    assert run(cleanup=True, TC_TEST_VERSION='2').returncode == 0
    (memory / 'debug.sig').write_text('leftover')
    result = run(cleanup=True, TC_TEST_VERSION='2')
    assert result.returncode == 1
    assert 'cleanup helper is unavailable' in result.stderr
    assert (memory / 'debug.sig').exists()


def test_uninstall_stops_before_deleting_files_when_cleanup_is_busy():
    plan = build_uninstall_plan('host', ['/Volumes/dk2'], ['/Volumes/dk2/.samba4'], reboot_after_uninstall=False)
    cleanup = StopTelemetryAction(cleanup=True)
    cleanup_command = render_remote_action(cleanup)
    executed = []
    def ssh(_connection, command):
        executed.append(command)
        if command == cleanup_command:
            raise RuntimeError('telemetry busy')
    with mock.patch('timecapsulesmb.deploy.executor.run_ssh', side_effect=ssh):
        with pytest.raises(RuntimeError, match='telemetry busy'):
            remote_uninstall_payload(SshConnection('host', 'password', ''), plan)
    assert executed == [render_remote_action(action) for action in plan.remote_actions[:plan.remote_actions.index(cleanup) + 1]]
    assert remote_action_to_jsonable(cleanup) == {'kind': 'stop_telemetry', 'cleanup': True}
    assert '/mnt/Memory/debug' in plan.verify_absent_targets
    assert '/mnt/Memory/debug.sig' in plan.verify_absent_targets
