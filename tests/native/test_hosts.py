"""Map the device hostname in Apple's /etc/hosts: once, byte-preserving, and
without ever touching lines that are not ours."""
import os
import socket
import stat
import subprocess
import threading
import time

import pytest

from tests.native.build import ROOT, compile_modules
from timecapsulesmb.device.probe import DeviceHostnameProbeResult


@pytest.fixture(scope='module')
def hosts_tool(tmp_path_factory):
    binary = tmp_path_factory.mktemp('hosts-native') / 'hosts'
    compile_modules(binary, ('native/service/hosts.c',),
                    flags=('-I', str(ROOT / 'build/native')),
                    extra_sources=(ROOT / 'tests/native/unit/test_hosts.c',))
    return binary


def update(tool, path, name):
    run = subprocess.run([str(tool), str(path), name], capture_output=True, text=True, check=True)
    result = int(run.stdout.strip().removeprefix('result='))
    return result, run.stderr


APPLE = '#\t$NetBSD: hosts,v 1.7 2004/08/29 13:26:17 chs Exp $\n::1\t\t\tlocalhost localhost.\n127.0.0.1\t\tlocalhost localhost.\n'


def test_missing_mapping_is_appended_once_and_then_left_alone(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text(APPLE)
    assert update(hosts_tool, path, 'capsule') == (1, f'stage: mapped capsule in {path}\n')
    assert path.read_text() == APPLE + '127.0.0.1\tcapsule capsule.local\n'
    before = os.stat(path)
    assert update(hosts_tool, path, 'capsule') == (0, '')
    after = os.stat(path)
    # Nothing is rewritten when the mapping is already there.
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize('line', ['192.0.2.1 capsule\n', '::1 capsule.local # Apple entry\n',
                                  '127.0.0.1\tlocalhost capsule\n'])
def test_existing_mapping_by_anyone_is_kept_unchanged(hosts_tool, tmp_path, line):
    path = tmp_path/'hosts'; path.write_text(APPLE + line)
    before = os.stat(path)
    assert update(hosts_tool, path, 'capsule') == (0, '')
    assert path.read_text() == APPLE + line
    assert os.stat(path).st_ino == before.st_ino


def test_a_mention_in_a_comment_is_not_a_mapping(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text('127.0.0.1 localhost # capsule\n')
    assert update(hosts_tool, path, 'capsule')[0] == 1
    assert path.read_text() == '127.0.0.1 localhost # capsule\n127.0.0.1\tcapsule capsule.local\n'


def test_rename_removes_only_our_exact_line_for_the_old_name(hosts_tool, tmp_path):
    kept = ('127.0.0.1\tlocalhost oldname\n'           # Apple's line naming the old host
            '127.0.0.1 oldname oldname.local\n'        # not our exact form (space, not tab)
            '127.0.0.1\toldname oldname.local extra\n'  # not our exact form (extra word)
            '# 127.0.0.1\toldname oldname.local\n'      # a comment
            '\n192.168.1.170 tcsmb-192-168-1-170\n')    # an SSH client's line (transport/ssh.py)
    path = tmp_path/'hosts'
    # Our line from an earlier name, as this manager or the retired shell wrote it.
    path.write_text(APPLE + '\n127.0.0.1\toldname oldname.local\n' + kept)
    result, log = update(hosts_tool, path, 'newname')
    assert result == 1
    assert path.read_text() == APPLE + '\n' + kept + '127.0.0.1\tnewname newname.local\n'
    assert log == f'stage: removed the stale mapping for oldname\nstage: mapped newname in {path}\n'


def test_every_removed_stale_line_is_logged_in_file_order(hosts_tool, tmp_path):
    names = [f'old{i}' for i in range(10)]
    path = tmp_path/'hosts'
    path.write_text(APPLE + ''.join(f'127.0.0.1\t{name} {name}.local\n' for name in names))
    result, log = update(hosts_tool, path, 'capsule')
    assert result == 1
    assert path.read_text() == APPLE + '127.0.0.1\tcapsule capsule.local\n'
    assert log == ''.join(f'stage: removed the stale mapping for {name}\n' for name in names) + \
        f'stage: mapped capsule in {path}\n'


def test_stale_line_is_removed_even_when_the_new_name_is_already_mapped(hosts_tool, tmp_path):
    path = tmp_path/'hosts'
    path.write_text('127.0.0.1\told old.local\n192.0.2.9 new\n')
    assert update(hosts_tool, path, 'new') == (1, 'stage: removed the stale mapping for old\n')
    assert path.read_text() == '192.0.2.9 new\n'


def test_file_without_a_trailing_newline_gets_exactly_one(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text('127.0.0.1 localhost')
    assert update(hosts_tool, path, 'capsule')[0] == 1
    assert path.read_text() == '127.0.0.1 localhost\n127.0.0.1\tcapsule capsule.local\n'


def test_missing_file_is_created_with_the_mapping(hosts_tool, tmp_path):
    path = tmp_path/'hosts'
    assert update(hosts_tool, path, 'capsule')[0] == 1
    assert path.read_text() == '127.0.0.1\tcapsule capsule.local\n'


def test_apple_read_only_mode_is_preserved(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text(APPLE); path.chmod(0o444)
    assert update(hosts_tool, path, 'capsule')[0] == 1
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o444
    assert not (tmp_path/'.hosts.tc').exists()


def test_leftover_temporary_file_is_replaced(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text(APPLE)
    (tmp_path/'.hosts.tc').write_text('interrupted partial write')
    assert update(hosts_tool, path, 'capsule')[0] == 1
    assert path.read_text() == APPLE + '127.0.0.1\tcapsule capsule.local\n'
    assert not (tmp_path/'.hosts.tc').exists()


@pytest.mark.parametrize('name', ['', 'capsule\ninjected', 'café', 'two words', 'a' * 256])
def test_invalid_name_is_refused_and_changes_nothing(hosts_tool, tmp_path, name):
    path = tmp_path/'hosts'; path.write_text('preserve\n')
    assert update(hosts_tool, path, name) == (-1, 'Invalid argument\n')
    assert path.read_text() == 'preserve\n'
    assert sorted(os.listdir(tmp_path)) == ['hosts']


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses file permissions')
def test_unreadable_file_fails_without_changes(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text(APPLE); path.chmod(0)
    try:
        assert update(hosts_tool, path, 'capsule') == (-1, 'Permission denied\n')
    finally:
        path.chmod(0o644)
    assert path.read_text() == APPLE
    assert sorted(os.listdir(tmp_path)) == ['hosts']


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses directory permissions')
def test_unwritable_directory_fails_and_leaves_the_original(hosts_tool, tmp_path):
    folder = tmp_path/'etc'; folder.mkdir()
    path = folder/'hosts'; path.write_text(APPLE + '127.0.0.1\told old.local\n')
    folder.chmod(0o555)
    try:
        # Nothing claims a removal or a mapping that never reached the file.
        assert update(hosts_tool, path, 'capsule') == (-1, 'Permission denied\n')
    finally:
        folder.chmod(0o755)
    assert path.read_text() == APPLE + '127.0.0.1\told old.local\n'
    assert sorted(os.listdir(folder)) == ['hosts']


@pytest.mark.parametrize('native_test', [True, False], ids=['test-build', 'device-build'])
def test_only_test_builds_take_the_hostname_from_a_file(tmp_path_factory, tmp_path, native_test):
    binary = tmp_path_factory.mktemp('hostname-native') / 'hosts'
    # -U drops the TC_NATIVE_TEST that host builds define, as the device build does.
    flags = ('-I', str(ROOT / 'build/native')) + (() if native_test else ('-UTC_NATIVE_TEST',))
    compile_modules(binary, ('native/service/hosts.c',), flags=flags,
                    extra_sources=(ROOT / 'tests/native/unit/test_hosts.c',))
    name = tmp_path/'hostname'; name.write_text('from-a-file\n')
    run = subprocess.run([str(binary), '--hostname'], capture_output=True, text=True, check=True,
                         env={**os.environ, 'TC_TEST_HOSTNAME': str(name)})
    assert run.stdout == ('from-a-file\n' if native_test else socket.gethostname() + '\n')


def test_test_build_reports_an_unset_hostname_as_empty(hosts_tool, tmp_path):
    name = tmp_path/'hostname'; name.write_text('')
    run = subprocess.run([str(hosts_tool), '--hostname'], capture_output=True, text=True, check=True,
                         env={**os.environ, 'TC_TEST_HOSTNAME': str(name)})
    assert run.stdout == '\n'


PLAIN_CASES = [('capsule', True), ('a.b-c_D9', True), ('a' * 255, True),
               ('', False), ('bad name', False), ('capsule\ninjected', False), ('caf\u00e9', False),
               ('a' * 256, False), ('tab\there', False), ('semi;colon', False)]


def plain(tool, name):
    run = subprocess.run([str(tool), '--plain', name], capture_output=True, text=True, check=True)
    return run.stdout == '1\n'


@pytest.mark.parametrize(('name', 'expected'), PLAIN_CASES)
def test_plain_hostname_is_what_one_hosts_line_can_hold(hosts_tool, name, expected):
    assert plain(hosts_tool, name) is expected


def test_doctor_and_the_manager_agree_on_plain_hostnames(hosts_tool):
    # Doctor repeats the manager's rule in Python; both must decide alike.
    for name, _ in PLAIN_CASES + [('x' * 254 + '.', None), ('-', None), ('.local', None)]:
        assert DeviceHostnameProbeResult(name).plain is plain(hosts_tool, name), name


def test_contents_arriving_in_pieces_are_kept_whole(hosts_tool, tmp_path):
    # Read to end of file, not the size fstat reported: a pipe reports 0 bytes.
    path = tmp_path/'hosts'; os.mkfifo(path)
    first, second = APPLE[:40], APPLE[40:]

    def feed():
        with open(path, 'w') as writer:
            writer.write(first); writer.flush()
            time.sleep(.3)
            writer.write(second)

    thread = threading.Thread(target=feed); thread.start()
    try:
        assert update(hosts_tool, path, 'capsule')[0] == 1
    finally:
        thread.join(timeout=10)
    assert path.read_text() == APPLE + '127.0.0.1\tcapsule capsule.local\n'
    assert stat.S_ISREG(os.stat(path).st_mode)


def test_oversized_file_is_refused_and_left_alone(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; content = '# ' + 'x' * 70000 + '\n'; path.write_text(content)
    assert update(hosts_tool, path, 'capsule') == (-1, 'File too large\n')
    assert path.read_text() == content
    assert sorted(os.listdir(tmp_path)) == ['hosts']
