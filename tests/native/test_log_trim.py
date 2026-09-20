import subprocess
from tests.native.build import ROOT, instrumentation_flags


def test_live_log_writer_survives_bounded_trim(tmp_path):
    binary = tmp_path / 'log-trim'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(),
                    '-I', str(ROOT / 'build/native'), str(ROOT / 'build/native/common/log.c'),
                    str(ROOT / 'tests/native/unit/test_log_trim.c'), '-o', str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=5)
