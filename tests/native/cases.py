"""Native regression cases link modules; Python retains the output assertions."""
from functools import lru_cache
from pathlib import Path
import subprocess
from tests.native.build import ROOT, sources, build_root, compile_modules

_CASES = Path(__file__).with_suffix('')
_DIRECTORY = build_root('cases')
_MODULES = tuple(str(path.relative_to(ROOT / 'build')) for path in sources()
                 if path.parent.name == 'common' or
                 path.name in {'network_commands.c', 'nt_hash.c', 'adisk_txt.c'})


def native_case_source(name):
    return str(_CASES / f'{name}.c')


def run_case(name, *args, timeout=10):
    binary = compile_case(native_case_source(name))
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


@lru_cache(maxsize=None)
def compile_case(source):
    case = Path(source)
    directory = _DIRECTORY / case.stem
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / 'case'
    return compile_modules(binary, _MODULES, flags=('-I', str(ROOT / 'build/native')),
                           extra_sources=(case,))
