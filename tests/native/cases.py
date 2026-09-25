"""Native regression cases link modules; Python retains the output assertions."""
from functools import lru_cache
import hashlib
from pathlib import Path
import subprocess
from tests.native.build import ROOT, sources, build_root, compile_modules

_CASES = Path(__file__).with_suffix('')
_DIRECTORY = build_root('cases')
_MODULES = tuple(str(path.relative_to(ROOT / 'build')) for path in sources()
                 if path.parent.name == 'common' or
                 path.name in {'network_commands.c', 'nt_hash.c', 'adisk_txt.c', 'registrant.c',
                               'dnssd_clientstub.c', 'dnssd_ipc.c'})


def native_case_source(name):
    return str(_CASES / f'{name}.c')


def run_case(name, *args, timeout=10):
    binary = compile_case(native_case_source(name))
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


@lru_cache(maxsize=None)
def compile_case(source, flags=()):
    case = Path(source)
    # Flags (e.g. a per-test daemon socket path) get their own output directory.
    suffix = '-' + hashlib.sha256(repr(flags).encode()).hexdigest()[:12] if flags else ''
    directory = _DIRECTORY / f'{case.stem}{suffix}'
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / 'case'
    return compile_modules(binary, _MODULES, flags=('-I', str(ROOT / 'build/native'), *flags),
                           extra_sources=(case,))
