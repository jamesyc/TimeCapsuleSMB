"""Map the device hostname in Apple's /etc/hosts: once, byte-preserving, and
without ever touching lines that are not ours."""
import os
import re
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


# Doctor repeats the manager's reading of /etc/hosts in Python. Both run on the
# same lines here: UTF-8 without NUL bytes, which is what the probe's SSH output
# carries. Each case also states the answer, so both cannot agree on a wrong one.
STALE_LINES = [
    pytest.param('127.0.0.1\told old.local', 'old', id='ours'),
    pytest.param('127.0.0.1\tOld.Name_9 Old.Name_9.local', 'Old.Name_9', id='case-dot-underscore'),
    pytest.param('127.0.0.1\ta\tb a\tb.local', 'a\tb', id='tab-in-name'),
    pytest.param('127.0.0.1\tcaf\u00e9 caf\u00e9.local', 'caf\u00e9', id='utf8-name'),
    pytest.param('127.0.0.1\t' + 'x' * 255 + ' ' + 'x' * 255 + '.local', 'x' * 255, id='255-bytes'),
    pytest.param('127.0.0.1\t' + 'x' * 256 + ' ' + 'x' * 256 + '.local', None, id='256-bytes'),  # longer than the manager's buffer
    pytest.param('127.0.0.1\t' + '\u00e9' * 127 + ' ' + '\u00e9' * 127 + '.local', '\u00e9' * 127, id='254-utf8-bytes'),
    pytest.param('127.0.0.1\t' + '\u00e9' * 128 + ' ' + '\u00e9' * 128 + '.local', None, id='256-utf8-bytes'),  # 128 characters, 256 bytes
    pytest.param('127.0.0.1\told old.local\r', None, id='trailing-cr'),
    pytest.param('127.0.0.1\told old.local ', None, id='trailing-space'),
    pytest.param('127.0.0.1\told old', None, id='no-local'),
    pytest.param('127.0.0.1\told new.local', None, id='names-differ'),
    pytest.param('127.0.0.1 old old.local', None, id='space-not-tab'),
    pytest.param('127.0.0.1\t old old.local', None, id='empty-name'),
    pytest.param('127.0.0.1\tnewname newname.local', None, id='current-name'),
    pytest.param('# 127.0.0.1\told old.local', None, id='comment'),
    pytest.param('', None, id='empty-line'),
]


@pytest.mark.parametrize(('line', 'stale'), STALE_LINES)
def test_doctor_and_the_manager_agree_on_our_stale_lines(hosts_tool, tmp_path, line, stale):
    path = tmp_path/'hosts'; path.write_bytes((APPLE + line + '\n').encode())
    _, log = update(hosts_tool, path, 'newname')
    removed = re.findall(r'^stage: removed the stale mapping for (.*)$', log, re.M)
    assert removed == ([stale] if stale else [])
    assert DeviceHostnameProbeResult('newname', (line,)).stale_names == ((stale,) if stale else ())


MAPPED_LINES = [
    pytest.param('127.0.0.1 capsule', True, id='name'),
    pytest.param('::1\tcapsule.local # Apple entry', True, id='local-name-and-comment'),
    pytest.param('127.0.0.1\tlocalhost capsule', True, id='second-name'),
    pytest.param('127.0.0.1 capsule\r', True, id='trailing-cr'),  # CR separates words
    pytest.param('  127.0.0.1 \t capsule', True, id='leading-blanks'),
    pytest.param('127.0.0.1 ' + 'x' * 1004 + ' capsule', True, id='ends-at-1022'),  # 1022 bytes: within the cut
    pytest.param('127.0.0.1 ' + 'x' * 1006 + ' capsule', False, id='cut-at-1023'),  # the 1023-byte cut falls inside the name
    pytest.param('capsule', False, id='address-only'),  # the address field is not a name
    pytest.param('127.0.0.1 localhost # capsule', False, id='in-comment'),
    pytest.param('# 127.0.0.1 capsule', False, id='commented-out'),
    pytest.param('127.0.0.1 Capsule', False, id='case-differs'),
    pytest.param('127.0.0.1 capsules', False, id='longer-word'),
    pytest.param('127.0.0.1\x0bcapsule', False, id='vertical-tab'),  # only space, tab and CR separate
    pytest.param('127.0.0.1\x0ccapsule', False, id='form-feed'),
    pytest.param('127.0.0.1\x1ccapsule', False, id='file-separator'),
    pytest.param('127.0.0.1\u00a0capsule', False, id='nbsp'),
]


@pytest.mark.parametrize(('line', 'mapped'), MAPPED_LINES)
def test_doctor_and_the_manager_agree_on_what_maps_the_hostname(hosts_tool, tmp_path, line, mapped):
    path = tmp_path/'hosts'; path.write_bytes((line + '\n').encode())
    result, _ = update(hosts_tool, path, 'capsule')
    assert result in (0, 1)
    # The manager leaves a mapped file alone and appends its line otherwise.
    assert (result == 0) is mapped
    assert DeviceHostnameProbeResult('capsule', (line,)).mapped is mapped


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
