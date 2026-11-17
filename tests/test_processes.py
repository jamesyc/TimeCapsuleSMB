import os
import signal
import subprocess
import sys
import time
from unittest import mock

import pytest

from tests.processes import kill_acp_fixture_groups, kill_group, signal_if_ours


def session(*command):
    return subprocess.Popen(list(command), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_kills_a_live_member():
    process = session('sleep', '30')
    try:
        kill_group(process.pid)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        process.kill(); process.wait()


def test_a_group_left_with_only_an_unreaped_zombie_is_not_an_error():
    # Darwin answers EPERM here; Linux signals the zombie and succeeds.
    process = session('true')
    try:
        wait_until_zombie(process.pid)
        kill_group(process.pid)
    finally:
        process.wait()


def test_a_zombie_leader_does_not_shield_a_live_member():
    # Darwin answers EPERM only when no live member took the signal, so
    # ignoring it never hides a survivor.
    leader = subprocess.Popen(['/bin/sh', '-c', 'sleep 30 & echo $!'], start_new_session=True,
                              stdout=subprocess.PIPE, text=True)
    member = int(leader.stdout.readline())
    try:
        wait_until_zombie(leader.pid)
        kill_group(leader.pid)
        deadline = time.monotonic() + 10
        while not member_gone(member):
            assert time.monotonic() < deadline, 'the live member survived kill_group'
            time.sleep(0.02)
    finally:
        # The unreaped leader keeps the group's ID from being reused.
        kill_group(leader.pid)
        leader.wait(); leader.stdout.close()


def process_state(pid):
    return subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


def wait_until_zombie(pid):
    # Exited but not reaped. os.waitid(WNOWAIT) would say so directly, but
    # macOS builds of Python before 3.13 lack it.
    deadline = time.monotonic() + 10
    while not process_state(pid).startswith('Z'):
        assert time.monotonic() < deadline, f'{pid} did not exit'
        time.sleep(0.02)


def member_gone(pid):
    # The orphaned member is reparented to init, which reaps it; a zombie
    # left behind for a moment counts as gone.
    state = process_state(pid)
    return not state or state.startswith('Z')


def test_a_group_that_is_gone_is_not_an_error():
    process = session('true')
    process.wait()
    kill_group(process.pid)


@pytest.mark.parametrize('pgid', [0, 1, -1, 'own'])
def test_refuses_groups_that_are_not_the_tests_own(pgid):
    pgid = os.getpgrp() if pgid == 'own' else pgid
    with mock.patch.object(os, 'killpg') as killpg, pytest.raises(ValueError):
        kill_group(pgid)
    killpg.assert_not_called()


def test_other_errors_still_raise():
    with mock.patch.object(os, 'killpg', side_effect=OSError(22, 'Invalid argument')), \
            pytest.raises(OSError):
        kill_group(12345)


def test_acp_fixture_groups_kills_only_recorded_group_leaders(tmp_path):
    leader, child = session('sleep', '30'), session('sleep', '30')
    calls = tmp_path / 'calls'
    calls.write_text(f'MaSt {leader.pid} parent 1.0\nMaSt {child.pid} child 1.1\n')
    try:
        kill_acp_fixture_groups(calls)
        assert leader.wait(timeout=10) == -signal.SIGKILL
        assert child.poll() is None
    finally:
        for process in (leader, child):
            process.kill(); process.wait()


def test_acp_fixture_groups_without_records_does_nothing(tmp_path):
    with mock.patch.object(os, 'killpg') as killpg:
        kill_acp_fixture_groups(tmp_path / 'calls')
    killpg.assert_not_called()


def sleeper_in(folder):
    # The folder appears in its command line, as a test fixture's path does.
    return session(sys.executable, '-c', 'import time; time.sleep(30)', str(folder / 'fixture'))


def test_signal_if_ours_signals_a_process_running_from_the_folder(tmp_path):
    process = sleeper_in(tmp_path)
    try:
        signal_if_ours([process.pid], tmp_path, signal.SIGKILL)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        process.kill(); process.wait()


@pytest.mark.parametrize('folder', ['other', 'manager1'])
def test_signal_if_ours_leaves_a_process_from_elsewhere_alone(tmp_path, folder):
    # "manager1" is a prefix of the process's folder "manager10", not its folder.
    process = sleeper_in(tmp_path / 'manager10')
    try:
        with mock.patch.object(os, 'kill') as kill:
            signal_if_ours([process.pid], tmp_path / folder, signal.SIGKILL)
        kill.assert_not_called()
        assert process.poll() is None
    finally:
        process.kill(); process.wait()


def test_signal_if_ours_ignores_a_pid_that_is_gone(tmp_path):
    process = sleeper_in(tmp_path)
    process.kill(); process.wait()
    with mock.patch.object(os, 'kill') as kill:
        signal_if_ours([process.pid], tmp_path, signal.SIGKILL)
    kill.assert_not_called()


def test_signal_if_ours_tolerates_an_exit_between_the_check_and_the_signal(tmp_path):
    process = sleeper_in(tmp_path)
    try:
        with mock.patch.object(os, 'kill', side_effect=ProcessLookupError):
            signal_if_ours([process.pid], tmp_path, signal.SIGTERM)
    finally:
        process.kill(); process.wait()


def test_signal_if_ours_reads_every_pid_in_one_listing(tmp_path):
    ours, elsewhere, gone = sleeper_in(tmp_path), sleeper_in(tmp_path.parent / 'elsewhere'), sleeper_in(tmp_path)
    gone.kill(); gone.wait()
    try:
        with mock.patch.object(subprocess, 'run', wraps=subprocess.run) as run:
            signal_if_ours([ours.pid, elsewhere.pid, gone.pid], tmp_path, signal.SIGKILL)
        assert run.call_count == 1
        assert ours.wait(timeout=10) == -signal.SIGKILL
        assert elsewhere.poll() is None
    finally:
        for process in (ours, elsewhere):
            process.kill(); process.wait()


def test_signal_if_ours_without_pids_runs_nothing(tmp_path):
    with mock.patch.object(subprocess, 'run') as run, mock.patch.object(os, 'kill') as kill:
        signal_if_ours([], tmp_path, signal.SIGKILL)
    run.assert_not_called(); kill.assert_not_called()
