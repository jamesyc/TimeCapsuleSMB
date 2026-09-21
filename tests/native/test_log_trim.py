import subprocess
from tests.native.build import ROOT, instrumentation_flags
from tests.native.cases import compile_case, native_case_source


def test_live_log_writer_survives_bounded_trim(tmp_path):
    binary = tmp_path / 'log-trim'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(),
                    '-I', str(ROOT / 'build/native'), str(ROOT / 'build/native/common/log.c'),
                    str(ROOT / 'tests/native/unit/test_log_trim.c'), '-o', str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=5)


def test_timestamped_logging_truncates_long_lines_without_heap():
    binary = compile_case(native_case_source("timestamped_logging_truncates_long_lines_without_heap"))
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "A" * 5000 not in result.stderr
    assert 4000 <= result.stderr.count("A") < 5000
    assert result.stderr.endswith("\n")
