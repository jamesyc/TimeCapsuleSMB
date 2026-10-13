"""Execute the retained boot shell with controlled native platform utilities."""
import json
import os
import shlex
import subprocess
import sys

import pytest
from tests.executables import write_executable
from timecapsulesmb.deploy.boot_assets import load_boot_asset_text


@pytest.fixture
def boot(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('mount', 'mount_tmpfs', 'mount_mfs', 'uname', 'sysctl', 'service'):
        # One shared file serves every tool name; each records argv[0].
        write_executable(tools / name, f'#!{sys.executable}\n' + '''
import os,sys,json
from pathlib import Path
root=Path(os.environ['BOOT_ROOT']);name=Path(sys.argv[0]).name
with (root/'calls').open('a') as log:log.write(json.dumps([name,*sys.argv[1:]])+'\\n')
if name==os.environ.get('FAIL_TOOL'):sys.exit(1)
if name=='mount' and os.environ.get('MOUNTED')=='1':print('tmpfs on '+str(root/'Locks')+' type tmpfs (local)')
if name=='uname':print(os.environ.get('KERNEL','6.0'))
if name=='sysctl' and sys.argv[1]=='-n':print(os.environ.get('BUFCACHE','5'))
''')
    # The RAM root the diskd and wcifsnd guards edit: a fake multi-call ACPd
    # records the name it was started as (argv[0]'s basename) and its arguments.
    ram = tmp_path / 'ram'
    (ram / 'sbin').mkdir(parents=True)
    (ram / 'usr/libexec').mkdir(parents=True)
    write_executable(ram / 'sbin/ACPd', f'#!{sys.executable}\n' + '''
import os,sys,json
from pathlib import Path
with (Path(os.environ['BOOT_ROOT'])/'calls').open('a') as log:log.write(json.dumps(['multicall',Path(sys.argv[0]).name,*sys.argv[1:]])+'\\n')
''')
    text = load_boot_asset_text('boot.sh')
    replacements = {'/mnt/Memory': str(tmp_path / 'Memory'), '/mnt/Locks': str(tmp_path / 'Locks'),
                    '/sbin/ACPd': str(ram / 'sbin/ACPd'), '/sbin/$1': str(ram / 'sbin/$1'),
                    '/sbin/.$1.': str(ram / 'sbin/.$1.'), '/usr/libexec/': str(ram / 'usr/libexec') + '/',
                    '/root': str(tmp_path / 'root'), '/mnt/Flash/service': shlex.quote(str(tools / 'service')),
                    **{f'/sbin/{name}': shlex.quote(str(tools / name)) for name in ('mount', 'mount_mfs', 'mount_tmpfs', 'sysctl')},
                    '/usr/bin/uname': shlex.quote(str(tools / 'uname'))}
    for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    script = tmp_path / 'boot.sh'
    script.write_text(text)
    def run(*args, **environment):
        return subprocess.run(['/bin/sh', str(script), *args], capture_output=True, timeout=10,
                              env={**os.environ, 'BOOT_ROOT': str(tmp_path), **environment})
    def calls(): return [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    return tmp_path, run, calls


@pytest.mark.parametrize('kernel,mount,amount', [('6.0','mount_tmpfs','4m'),('4.0','mount_mfs','8192')])
def test_boot_prepares_platform_then_execs_native_manager(boot, kernel, mount, amount):
    root, run, calls = boot
    result = run(KERNEL=kernel, BUFCACHE='10')
    assert result.returncode == 0, result.stderr
    operations = calls()
    assert [mount, '-s', amount, 'tmpfs' if kernel.startswith('6') else 'swap', str(root/'Locks')] in operations
    # Apple's kernel tunables stay as the firmware set them, even when the
    # buffer cache is not at the NetBSD default.
    assert not any(call[0] == 'sysctl' for call in operations)
    assert operations[-1] == ['service', 'manager']
    assert (root/'root/tc-netbsd7').resolve() == root/'Memory/samba4'
    assert (root/'Memory/samba4/private').stat().st_mode & 0o777 == 0o700


def test_repeated_boot_preserves_active_locks_and_existing_prefixes(boot):
    root, run, calls = boot
    (root/'Locks').mkdir(); lock = root/'Locks/locking.tdb'; lock.write_bytes(b'active locks')
    (root/'root/tc-netbsd7').mkdir(parents=True)
    (root/'root/tc-netbsd7/keep').write_text('old runtime')
    inode = lock.stat().st_ino
    for _ in range(2): assert run(MOUNTED='1').returncode == 0
    assert lock.stat().st_ino == inode and lock.read_bytes() == b'active locks'
    assert (root/'root/tc-netbsd7/keep').read_text() == 'old runtime'
    assert not any(call[0].startswith('mount_') for call in calls())


def test_existing_plain_locks_are_not_hidden_under_a_new_mount(boot):
    root, run, calls = boot
    (root/'Locks').mkdir(); (root/'Locks/locking.tdb').write_bytes(b'active')
    assert run().returncode == 0
    assert not any(call[0].startswith('mount_') for call in calls())
    assert (root/'Locks/locking.tdb').read_bytes() == b'active'


@pytest.mark.parametrize('kernel,failed,success', [('4.0','mount_mfs',False), ('6.0','mount_tmpfs',True), ('6.0','mount',False)])
def test_mount_failure_retains_firmware_specific_fallback(boot, kernel, failed, success):
    _, run, calls = boot
    result = run(KERNEL=kernel, FAIL_TOOL=failed)
    assert (result.returncode == 0) is success
    assert any(call == ['service','manager'] for call in calls()) is success


def test_boot_rejects_unexpected_arguments_before_preparation(boot):
    root, run, _ = boot
    assert run('restart').returncode == 2
    assert not (root/'Memory').exists()


def test_rc_local_detaches_boot_stdin_and_returns_without_waiting(tmp_path):
    import time
    boot = tmp_path/'boot'
    captured = tmp_path/'input'
    ready = tmp_path/'ready'
    boot.write_text('#!/bin/sh\ncat > '+shlex.quote(str(captured))+'\necho ready > '+shlex.quote(str(ready))+'\n')
    boot.chmod(0o755)
    script = load_boot_asset_text('rc.local').replace('/mnt/Flash/boot.sh',shlex.quote(str(boot)))
    result = subprocess.run(['/bin/sh','-c',script],input='must not reach boot',capture_output=True,text=True,timeout=5)
    assert result.returncode == 0
    deadline = time.monotonic()+5
    while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
    assert ready.exists() and captured.read_text()==''


def _wrapper(root, name='diskd'):
    return root/'ram/sbin'/name


GUARDS = ['diskd', 'wcifsnd']


def _guard_start(root, calls, name, *args):
    before = len(calls())
    result = subprocess.run([str(_wrapper(root, name)), *args], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'BOOT_ROOT': str(root)})
    return result.returncode, calls()[before:]


def test_boot_installs_both_guards_and_still_prepares_and_starts(boot):
    root, run, calls = boot
    result = run(KERNEL='4.0')
    assert result.returncode == 0, result.stderr
    for name in GUARDS:
        # Same inode as ACPd: the multi-call binary, reachable under the daemon's name.
        assert (root/'ram/usr/libexec'/name).stat().st_ino == (root/'ram/sbin/ACPd').stat().st_ino
        wrapper = _wrapper(root, name).read_text()
        assert wrapper.startswith('#!/bin/sh\n') and wrapper.rstrip().endswith('exit 0')
    assert (root/'ram/sbin/ACPd').read_text().startswith(f'#!{sys.executable}')
    assert sorted(path.name for path in (root/'ram/sbin').iterdir()) == ['ACPd', *GUARDS]
    # The commands after the guards still ran: their heredocs swallowed nothing.
    operations = calls()
    assert ['mount_mfs', '-s', '8192', 'swap', str(root/'Locks')] in operations
    assert operations[-1] == ['service', 'manager']


def test_diskd_guard_starts_only_the_manager_loopback_diskd(boot):
    root, run, calls = boot
    assert run().returncode == 0
    log = root/'Memory/samba4/var/runtime.log'

    def start(*args):
        return _guard_start(root, calls, 'diskd', *args)

    assert start('-i', 'lo0', '-d', 'local.') == (0, [['multicall', 'diskd', '-i', 'lo0', '-d', 'local.']])
    # ACPd's starts: nothing runs, one line each in the runtime log.
    assert start('-i', 'bridge0', '-d', 'local.') == (0, [])
    assert start('-i', '', '-d', 'local.') == (0, [])
    assert start() == (0, [])
    lines = log.read_text().splitlines()
    assert [line.split(' ', 2)[2] for line in lines] == [
        'diskd guard: ignored ACPd start: -i bridge0 -d local.',
        'diskd guard: ignored ACPd start: -i  -d local.',
        'diskd guard: ignored ACPd start: ',
    ]
    # Before boot.sh creates the log directory, an ACPd start writes nothing.
    log.unlink()
    (root/'Memory/samba4/var').rename(root/'Memory/samba4/var.away')
    assert start('-i', 'bridge0', '-d', 'local.') == (0, [])
    assert not (root/'Memory/samba4/var').exists()


def test_wcifsnd_guard_runs_nothing_and_discovery_reaches_apple_wcifsnd_by_its_link(boot):
    root, run, calls = boot
    assert run().returncode == 0
    log = root/'Memory/samba4/var/runtime.log'
    # ACPd starts /sbin/wcifsnd without arguments: Apple's daemon never runs,
    # and each start is one line in the runtime log.
    assert _guard_start(root, calls, 'wcifsnd') == (0, [])
    assert _guard_start(root, calls, 'wcifsnd') == (0, [])
    assert [line.split(' ', 2)[2] for line in log.read_text().splitlines()] == [
        'wcifsnd guard: ignored ACPd start'] * 2
    # Discovery's path: the multi-call binary started under the name wcifsnd.
    before = len(calls())
    subprocess.run([str(root/'ram/usr/libexec/wcifsnd')], check=True, timeout=10,
                   env={**os.environ, 'BOOT_ROOT': str(root)})
    assert calls()[before:] == [['multicall', 'wcifsnd']]
    log.unlink()
    (root/'Memory/samba4/var').rename(root/'Memory/samba4/var.away')
    assert _guard_start(root, calls, 'wcifsnd') == (0, [])
    assert not (root/'Memory/samba4/var').exists()


def test_rerunning_boot_keeps_one_link_and_the_same_guard_for_each_daemon(boot):
    root, run, _ = boot
    assert run(MOUNTED='1').returncode == 0
    before = {name: ((root/'ram/usr/libexec'/name).stat().st_ino, _wrapper(root, name).read_text()) for name in GUARDS}
    assert run(MOUNTED='1').returncode == 0
    # The second run links from /sbin/ACPd, never from the guard now at /sbin/NAME.
    for name in GUARDS:
        assert ((root/'ram/usr/libexec'/name).stat().st_ino, _wrapper(root, name).read_text()) == before[name]
        assert before[name][0] == (root/'ram/sbin/ACPd').stat().st_ino
    assert sorted(path.name for path in (root/'ram/sbin').iterdir()) == ['ACPd', *GUARDS]


def test_guard_failure_keeps_apple_daemons_and_still_boots(boot):
    root, run, calls = boot
    for name in GUARDS:
        _wrapper(root, name).write_text(f'apple {name}')
    (root/'ram/usr/libexec').chmod(0o555)  # ln fails
    try:
        result = run()
    finally:
        (root/'ram/usr/libexec').chmod(0o755)
    assert result.returncode == 0, result.stderr
    for name in GUARDS:
        assert _wrapper(root, name).read_text() == f'apple {name}'
    assert sorted(path.name for path in (root/'ram/sbin').iterdir()) == ['ACPd', *GUARDS]
    log = (root/'Memory/samba4/var/rc.local.log').read_text()
    assert 'diskd guard unavailable' in log and 'wcifsnd guard unavailable' in log
    assert calls()[-1] == ['service', 'manager']


def test_failed_guard_write_leaves_no_temporary_file_and_boot_continues(boot):
    root, run, calls = boot
    _wrapper(root, 'wcifsnd').write_text('apple wcifsnd')
    (root/'ram/sbin').chmod(0o555)  # the guard cannot be written beside it
    try:
        result = run()
    finally:
        (root/'ram/sbin').chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert _wrapper(root, 'wcifsnd').read_text() == 'apple wcifsnd'
    assert sorted(path.name for path in (root/'ram/sbin').iterdir()) == ['ACPd', 'wcifsnd']
    assert 'wcifsnd guard unavailable' in (root/'Memory/samba4/var/rc.local.log').read_text()
    assert calls()[-1] == ['service', 'manager']
