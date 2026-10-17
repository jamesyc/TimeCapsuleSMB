"""No command group can escape the real C worker's stop/drain boundary."""
import subprocess
import os
import signal
import pytest
from tests.native.build import ROOT, compile_modules

ACP_TIMEOUT_SECONDS = 3
COMMAND_TIMEOUT_SECONDS = 3

@pytest.fixture(scope='module')
def owner_driver(tmp_path_factory):
    root = tmp_path_factory.mktemp('nested-owner')
    binary = root / 'owner'
    modules = [f'native/common/{name}.c' for name in ('process', 'worker', 'parent', 'acp')]
    compile_modules(binary, modules,
                    flags=('-DTC_CHILD_GRACE_MS=150', f'-DTC_ACP_TIMEOUT_SECONDS={ACP_TIMEOUT_SECONDS}',
                           f'-DTEST_COMMAND_TIMEOUT_SECONDS={COMMAND_TIMEOUT_SECONDS}',
                           f'-DTC_ACP_PATH="{binary}"', '-I', str(ROOT / 'build/native')),
                    extra_sources=(ROOT / 'tests/native/unit/test_nested_owner.c',))
    return binary

@pytest.fixture(scope='module')
def owner_hang_seconds(owner_driver):
    # The driver derives its alarm from the timeouts it was built with. This
    # run also pays macOS's first-exec check outside the child's deadline.
    result = subprocess.run([str(owner_driver), 'hang-seconds'], capture_output=True, text=True,
                            check=True, timeout=10)
    return int(result.stdout)

@pytest.mark.parametrize('record,expected', [
    (None, '0 0'),
    ('', '0 0'),
    ('12345', '0 0'),
    ('12345 12344', '0 0'),
    ('12345 12344 pending', '0 0'),
    ('12345 12344 12300\n', '12345 12300'),
])
def test_record_reader_waits_for_complete_process_identity(owner_driver, tmp_path, record, expected):
    if record is not None:
        (tmp_path / 'command').write_text(record)
    result = subprocess.run([str(owner_driver), 'read-record'], cwd=tmp_path,
                            capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout.strip() == expected

@pytest.mark.parametrize('case', ['crash', 'term', 'parent-eof', 'timeout', 'grandchild', 'acp', 'acp-descendant', 'acp-timeout'])
def test_nested_command_cannot_outlive_drained_job(owner_driver, owner_hang_seconds, tmp_path, case):
    try:
        # The driver's own alarm fires first; this only bounds a driver that ignores it.
        result = subprocess.run([str(owner_driver), case], cwd=tmp_path, start_new_session=True,
                                capture_output=True, text=True, timeout=owner_hang_seconds + 10)
        assert result.returncode == 0, result.stderr
    finally:
        # A failed assertion must not leave the deliberately stubborn fixture.
        path = tmp_path / 'command'
        if path.exists():
            fields = path.read_text().split()
            if len(fields) == 3:
                group = int(fields[2])
                assert group != os.getpgrp()
                try: os.killpg(group, signal.SIGKILL)
                # macOS reports EPERM for a group left with only zombies.
                except (ProcessLookupError, PermissionError): pass
