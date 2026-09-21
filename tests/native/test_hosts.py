"""Keep Apple's resolver entries and make missing self-resolution repeatable."""
import subprocess
import pytest
from tests.native.build import ROOT, compile_modules


@pytest.fixture(scope='module')
def hosts_tool(tmp_path_factory):
    binary = tmp_path_factory.mktemp('hosts-native') / 'hosts'
    compile_modules(binary, ('native/service/hosts.c',),
                    flags=('-I', str(ROOT / 'build/native')),
                    extra_sources=(ROOT / 'tests/native/unit/test_hosts.c',))
    return binary


@pytest.mark.parametrize('content', ['127.0.0.1 localhost\n192.0.2.1 capsule\n',
                                    '::1 capsule.local # Apple entry\n'])
def test_existing_apple_hostname_mapping_is_unchanged(hosts_tool, tmp_path, content):
    path = tmp_path/'hosts'; path.write_text(content)
    subprocess.run([str(hosts_tool),str(path),'capsule'],check=True)
    assert path.read_text() == content


def test_missing_mapping_appended_once_and_comments_do_not_count(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text('127.0.0.1 localhost # capsule')
    for _ in range(2): subprocess.run([str(hosts_tool),str(path),'capsule'],check=True)
    assert path.read_text() == '127.0.0.1 localhost # capsule\n127.0.0.1\tcapsule capsule.local\n'


def test_invalid_name_never_changes_hosts(hosts_tool, tmp_path):
    path = tmp_path/'hosts'; path.write_text('preserve\n')
    result = subprocess.run([str(hosts_tool),str(path),'capsule\ninjected'])
    assert result.returncode == 1 and path.read_text() == 'preserve\n'
