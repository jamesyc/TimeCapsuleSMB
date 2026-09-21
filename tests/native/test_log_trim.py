import subprocess
from tests.native.build import ROOT, compile_modules
from tests.native.cases import compile_case, native_case_source


def test_live_log_writer_survives_bounded_trim(tmp_path):
    binary = tmp_path / 'log-trim'
    compile_modules(binary, ('native/common/log.c',),
                    flags=('-I', str(ROOT / 'build/native')),
                    extra_sources=(ROOT / 'tests/native/unit/test_log_trim.c',))
    subprocess.run([str(binary)], cwd=tmp_path, check=True, timeout=5)


def test_timestamped_logging_truncates_long_lines_without_heap():
    binary = compile_case(native_case_source("timestamped_logging_truncates_long_lines_without_heap"))
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "A" * 5000 not in result.stderr
    assert 4000 <= result.stderr.count("A") < 5000
    assert result.stderr.endswith("\n")
