"""Native regression cases link modules; Python retains the output assertions."""
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile
import hashlib
from tests.native.build import ROOT, sources, instrumentation_flags, build_root

_CASES = Path(__file__).with_suffix('')
_BUILD = tempfile.TemporaryDirectory(prefix='tc-native-cases-')
_DIRECTORY = build_root('cases')


@lru_cache(maxsize=None)
def compile_object(path, flags):
    key = hashlib.sha256((str(path) + repr(flags)).encode()).hexdigest()[:20]
    obj = _DIRECTORY / f'{key}.o'
    result = subprocess.run(['cc', '-D_GNU_SOURCE', '-DTC_NATIVE_TEST', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(), *flags,
                             '-c', str(path), '-o', str(obj)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return obj


def native_case_source(name):
    return str(_CASES / f'{name}.c')


@lru_cache(maxsize=None)
def compile_case(source):
    case = Path(source)
    config = case.with_suffix('.build').read_text().splitlines()
    target = config[0]
    modules = [p for p in sources(target) if p.name != 'main.c']
    if 'service' in config:
        modules.append(ROOT / 'build/native/service/network_commands.c')
    flags = [f'-D{line}' for line in config[1:] if '=' in line]
    directory = _DIRECTORY / case.stem
    directory.mkdir()
    objects = [compile_object(path, tuple(flags)) for path in modules]
    binary = directory / 'case'
    result = subprocess.run(['cc', '-D_GNU_SOURCE', '-DTC_NATIVE_TEST', '-Wall', '-Wextra', '-Werror', *instrumentation_flags(),
        '-I', str(ROOT / 'build/native'), str(case), *(str(o) for o in objects), '-o', str(binary)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return binary
