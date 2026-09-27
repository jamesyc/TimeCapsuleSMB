"""Run the real managed-rsync readiness script against fake ps and fstat.

Only the daemon is managed. A client run by hand on the device, or a server
sshd starts for a remote client, is also named rsync (issue #346) and must
neither satisfy nor fail the checks.
"""
import shlex
import subprocess
import sys
from unittest import mock

import pytest

from timecapsulesmb.device.probe import (
    ReadinessProbeResult,
    probe_managed_rsync_conn,
    probe_managed_runtime_once_conn,
)
from timecapsulesmb.transport.ssh import SshConnection

DAEMON = '40 S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach --config=/mnt/Memory/samba4/etc/rsyncd.conf'
CHILD = '41 S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach --config=/mnt/Memory/samba4/etc/rsyncd.conf'
CLIENT = '50 S rsync /mnt/Memory/samba4/sbin/rsync -rlptD --info=progress2 /Volumes/dk2/ShareRoot/ 192.168.1.248::shareroot/'
SERVER = '51 S rsync rsync --server -logDtpre.iLsfxCIvu . /Volumes/dk2/ShareRoot/'
SSH_DAEMON = '52 S rsync rsync --server --daemon .'
GLOB_CLIENT = '53 S rsync rsync -a /Volumes/dk2/* /tmp/x'
ZOMBIE_DAEMON = '42 Z rsync (rsync) --daemon'


@pytest.fixture
def device(tmp_path):
    payload = tmp_path / 'payload'
    payload.mkdir()
    (payload / 'rsync').write_text('#!/bin/sh\n')
    (payload / 'rsync').chmod(0o755)
    (payload / 'rsyncd.conf').write_text('[shareroot]\n')
    ram = tmp_path / 'ram'
    ram.mkdir()
    (ram / 'rsync').write_text('#!/bin/sh\n')
    (ram / 'rsync').chmod(0o755)
    (ram / 'rsyncd.conf').write_text('[shareroot]\n')
    ps = tmp_path / 'ps.txt'
    bound = tmp_path / 'bound.txt'
    fstat_log = tmp_path / 'fstat.log'
    fstat = tmp_path / 'fstat.py'
    fstat.write_text(
        'import sys\n'
        f'open({str(fstat_log)!r}, "a").write(sys.argv[-1] + "\\n")\n'
        f'if sys.argv[-1] in open({str(bound)!r}).read().split():\n'
        '    print("root rsync " + sys.argv[-1] + " 4* internet stream tcp *:873")\n'
    )
    # The probe's working directory holds a file a client's glob would
    # expand to if the script let the shell expand ps output.
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / '--daemon').write_text('')

    def run(rows, *, enabled, bound_pids=(), probe=probe_managed_rsync_conn):
        ps.write_text(''.join(row + '\n' for row in rows))
        bound.write_text(' '.join(bound_pids))
        fstat_log.write_text('')
        config = tmp_path / 'tcapsulesmb.conf'
        config.write_text(f'RSYNC_ENABLED={1 if enabled else 0}\n')

        def run_ssh(connection, command, **kwargs):
            argv = shlex.split(command)
            assert argv[:2] == ['/bin/sh', '-c']
            script = argv[2].replace('/bin/ps axww -o pid= -o stat= -o ucomm= -o command=', f'cat {ps}')
            script = script.replace('/usr/bin/fstat', f'{sys.executable} {fstat}')
            env = {'PATH': '/usr/bin:/bin', 'RUNTIME_CONFIG_FILE': str(config),
                   'RUNTIME_RSYNC_BIN': str(ram / 'rsync'), 'RUNTIME_RSYNC_CONF': str(ram / 'rsyncd.conf')}
            return subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, env=env, cwd=cwd)

        with mock.patch('timecapsulesmb.device.probe.read_runtime_payload_dir_conn', return_value=str(payload)), \
                mock.patch('timecapsulesmb.device.probe.run_ssh', side_effect=run_ssh):
            result = probe(SshConnection('host', 'pw', ''))
        return result, fstat_log.read_text().split()

    return run


def test_disabled_rsync_ignores_clients_and_ssh_servers(device):
    run = device
    result, fstat = run([CLIENT, SERVER, SSH_DAEMON, ZOMBIE_DAEMON], enabled=False)
    assert result.ready, result.lines
    assert 'SKIP:rsync daemon is disabled and not running' in result.lines
    assert fstat == []


def test_disabled_rsync_fails_on_a_running_daemon(device):
    run = device
    result, _ = run([CLIENT, DAEMON], enabled=False)
    assert not result.ready
    assert result.detail == 'rsync daemon is disabled but an rsync process is running'


def test_enabled_rsync_is_not_satisfied_by_a_client(device):
    run = device
    # The client even holds a connection to some :873; it is still not the daemon.
    result, fstat = run([CLIENT, SERVER, SSH_DAEMON], enabled=True, bound_pids=('50', '51', '52'))
    assert not result.ready
    assert 'FAIL:managed rsync process is not running' in result.lines
    assert 'FAIL:managed rsync is not bound to TCP 873' in result.lines
    assert fstat == []


def test_enabled_rsync_checks_only_daemon_pids_for_the_listener(device):
    run = device
    result, fstat = run([CLIENT, DAEMON, CHILD, SERVER], enabled=True, bound_pids=('40',))
    assert result.ready, result.lines
    assert 'PASS:managed rsync process is running' in result.lines
    assert 'PASS:managed rsync is bound to TCP 873' in result.lines
    assert fstat == ['40', '41']


def test_enabled_rsync_ignores_a_zombie_daemon(device):
    run = device
    result, fstat = run([ZOMBIE_DAEMON, CLIENT], enabled=True, bound_pids=('42',))
    assert not result.ready
    assert 'FAIL:managed rsync process is not running' in result.lines
    assert fstat == []


def test_client_glob_arguments_are_not_expanded(device):
    run = device
    # Unexpanded, no argv holds a "--daemon" word. Expanded in the probe's
    # cwd, "*" and "--d*" would both become "--daemon".
    rows = [GLOB_CLIENT, '54 S rsync rsync -a * /tmp/x', '55 S rsync rsync -a --d* /tmp/x']
    result, fstat = run(rows, enabled=True, bound_pids=('53', '54', '55'))
    assert 'FAIL:managed rsync process is not running' in result.lines
    assert fstat == []


def test_runtime_readiness_is_not_held_back_by_a_client_while_rsync_is_disabled(device):
    run = device
    ready = ReadinessProbeResult(ready=True, detail='ready')
    with mock.patch('timecapsulesmb.device.probe.probe_managed_smbd_conn', return_value=ready), \
            mock.patch('timecapsulesmb.device.probe.probe_managed_mdns_conn', return_value=ready):
        result, _ = run([CLIENT, SERVER], enabled=False,
                        probe=lambda connection: probe_managed_runtime_once_conn(
                            connection, smbd_mdns_stagger_seconds=0, mdns_settle_seconds=0))
    assert result.ready, result.detail
    assert 'SKIP:rsync daemon is disabled and not running' in [step.line for step in result.steps]
