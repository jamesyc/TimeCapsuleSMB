"""Fault injection around real collector processes, pipes, and process groups."""
import os
from pathlib import Path
import signal
import subprocess

import pytest

from tests.native.build import ROOT, instrumentation_flags


@pytest.fixture(scope='module')
def device_driver(tmp_path_factory):
    work = tmp_path_factory.mktemp('acp-driver')
    native = ROOT / 'build/native/telemetry'
    unit = Path(__file__).parent / 'unit'
    acp = work / 'acp'
    subprocess.run(['cc', str(Path(__file__).parent / 'integration/acp_fixture.c'), '-o', str(acp)],
                   check=True, capture_output=True, timeout=30)
    flags = ['cc', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(),
             '-I', str(native), '-I', str(unit), f'-DTC_ACP_PATH="{acp}"', '-DTC_ACP_TIMEOUT_SECONDS=1']
    obj = work / 'device.o'
    subprocess.run([*flags, '-DTC_TEST_DEVICE_FAULTS', '-include', str(unit / 'device_faults.h'),
                    '-c', str(native / 'device.c'), '-o', str(obj)], check=True, capture_output=True, timeout=30)
    binary = work / 'test-device'
    subprocess.run([*flags, str(unit / 'test_device.c'), str(obj), '-o', str(binary)],
                   check=True, capture_output=True, timeout=30)
    return binary


@pytest.mark.parametrize('scenario', [
    'normal', 'byte_reads', 'pipe', 'fork', 'nonblock', 'read_cloexec', 'write_cloexec',
    'cancel_before_group', 'child_group', 'initial_clock', 'running_clock',
    'select_error', 'select_eintr', 'read_error', 'read_eintr', 'read_eagain', 'wait_eintr',
    'cancel_before_fork', 'cancel_after_fork', 'reap_stuck',
])
def test_collector_syscall_failures_and_retries(device_driver, tmp_path, scenario):
    calls = tmp_path / 'calls'
    env = {**os.environ, 'TC_TEST_ACP_CALLS': str(calls)}
    if scenario == 'reap_stuck': env['TC_TEST_ACP_MODE'] = 'ignore_term'
    process = subprocess.Popen([str(device_driver), scenario], env=env, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, (scenario, stdout.decode(), stderr.decode())
    finally:
        # Kill only groups created by this test if a regression interrupted its
        # own cleanup. ACP deliberately belongs to a separate process group.
        if calls.exists():
            for line in calls.read_text().splitlines():
                if line.split()[2] != 'parent': continue
                try: os.killpg(int(line.split()[1]), signal.SIGKILL)
                except ProcessLookupError: pass
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.communicate(timeout=5)
