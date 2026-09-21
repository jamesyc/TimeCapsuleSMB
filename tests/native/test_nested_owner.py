"""No command group can escape the real C worker's stop/drain boundary."""
import subprocess
import os
import signal
import pytest
from tests.native.build import ROOT, instrumentation_flags

@pytest.fixture(scope='module')
def owner_driver(tmp_path_factory):
    root = tmp_path_factory.mktemp('nested-owner')
    binary = root / 'owner'
    modules = ['process', 'worker', 'parent', 'acp']
    subprocess.run(['cc', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(),
                    '-DTC_CHILD_GRACE_MS=150', '-DTC_ACP_TIMEOUT_SECONDS=1', f'-DTC_ACP_PATH="{binary}"', '-I', str(ROOT / 'build/native'),
                    *(str(ROOT / f'build/native/common/{name}.c') for name in modules),
                    str(ROOT / 'tests/native/unit/test_nested_owner.c'), '-o', str(binary)],
                   check=True, capture_output=True)
    return binary

@pytest.mark.parametrize('case', ['crash', 'term', 'parent-eof', 'timeout', 'grandchild', 'acp', 'acp-descendant', 'acp-timeout'])
def test_nested_command_cannot_outlive_drained_job(owner_driver, tmp_path, case):
    try:
        result = subprocess.run([str(owner_driver), case], cwd=tmp_path, start_new_session=True,
                                capture_output=True, text=True, timeout=15)
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
                except ProcessLookupError: pass
