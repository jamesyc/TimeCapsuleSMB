"""Compile the unified native service from the production source manifest.

Outputs go through tests/executables.py: links are deterministic, so workers
and runs that build the same binary share one file, and macOS checks it on
its first exec only once.
"""
from pathlib import Path
import fcntl
import hashlib
import subprocess
import sys
import tempfile
import shutil
import os
import re
from functools import lru_cache

from tests.executables import write_executable

ROOT = Path(__file__).resolve().parents[2]
_BUILD = tempfile.TemporaryDirectory(prefix='tc-native-build-')


def build_root(kind):
    coverage = os.environ.get('TC_NATIVE_COVERAGE_DIR')
    if coverage:
        path = Path(coverage) / kind
        path.mkdir(parents=True, exist_ok=True)
        return path
    # One directory for the whole pytest run (tests/conftest.py), so xdist
    # workers compile each object once between them.
    shared = os.environ.get('TC_NATIVE_OBJECT_DIR')
    if kind == 'objects' and shared:
        return Path(shared)
    return Path(_BUILD.name)

def sources() -> list[Path]:
    return [ROOT / 'build' / line for line in
            (ROOT / 'build/native/service.sources').read_text().splitlines() if line]


def header_define(header, name) -> int:
    """The integer a header under build/native #defines as name, so a test
    follows the production value instead of copying it."""
    text = (ROOT / 'build/native' / header).read_text()
    match = re.search(rf'^#define {re.escape(name)} (\d+)(?:LL)?$', text, re.M)
    assert match, f'{name} not in {header}'
    return int(match.group(1))


def stub_platform_flags():
    """Apple's dns_sd stub reads sockaddr sa_len unless NOT_HAVE_SA_LEN is
    defined, which Apple's own Linux build does; NetBSD and macOS have the
    field (review finding 8)."""
    return ['-DNOT_HAVE_SA_LEN'] if sys.platform.startswith('linux') else []


def instrumentation_flags():
    if os.environ.get('TC_NATIVE_SANITIZERS'):
        return ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
    if os.environ.get('TC_NATIVE_COVERAGE'):
        return ['-fprofile-instr-generate', '-fcoverage-mapping']
    return []

def compile_service(output, *, flags=(), extra_sources=(), exclude=()):
    binary = _compile(tuple(flags), tuple(extra_sources), tuple(exclude))
    return write_executable(output, binary.read_bytes())


def compile_modules(output, modules, *, flags=(), extra_sources=()):
    production = set(sources())
    selected = tuple(ROOT / 'build' / module for module in modules)
    if not set(selected) <= production:
        raise AssertionError("unit test module is not part of service.sources")
    binary = _compile_selected(selected, tuple(flags), tuple(extra_sources))
    return write_executable(output, binary.read_bytes())


def compile_program(output, *arguments, shared=True):
    """cc arguments -o output, for fixtures built outside service.sources.

    A test that changes the program's mode needs shared=False: links share
    the mode, so a chmod would reach every worker using the same build."""
    with tempfile.TemporaryDirectory() as directory:
        built = Path(directory) / Path(output).name
        result = subprocess.run(['cc', *map(str, arguments), '-o', str(built)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)
        if not shared:
            shutil.move(built, output)
            return Path(output)
        return write_executable(output, built.read_bytes())


def _compiler_flags(flags, instrumentation):
    return ('cc', '-D_GNU_SOURCE', '-DTC_NATIVE_TEST', '-D_DNS_SD_LIBDISPATCH=0',
            '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare', '-Wno-unterminated-string-initialization',
            *instrumentation, *flags)


def _vendor_flags(source):
    flags = []
    if source.name == 'tweetnacl.c':
        # Unmodified TweetNaCl uses signed shifts in field normalization.
        flags.append('-fno-sanitize=shift')
    if 'dnssd' in source.parts:
        # The vendored Apple stub is compiled unchanged.
        flags += ['-Wno-unused-but-set-variable', *stub_platform_flags()]
    return tuple(flags)


def _code_flags(arguments):
    """The arguments that change the object beyond what preprocessing shows."""
    kept, operand = [], False
    for argument in arguments:
        if operand:
            operand = False
        elif argument in ('-I', '-include'):
            operand = True
        elif not argument.startswith(('-D', '-U', '-I')):
            kept.append(argument)
    return kept


@lru_cache(maxsize=None)
def _compile_object(source, flags, instrumentation):
    arguments = [*_compiler_flags(flags, instrumentation), *_vendor_flags(source)]
    # Keyed by the preprocessed source, not the -D flags: fixtures define
    # paths under each worker's own directory, which most sources never use,
    # and those sources are then one object for every worker.
    result = subprocess.run([*arguments, '-E', str(source)], capture_output=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace'))
    key = hashlib.sha256(result.stdout + repr(_code_flags(arguments)).encode()).hexdigest()[:20]
    directory = build_root('objects')
    directory.mkdir(parents=True, exist_ok=True)
    obj = directory / f'{key}.o'
    # Workers starting the same fixture wait for the one compiling it instead
    # of all compiling it at once. A finished object appears only by rename.
    with open(directory / f'{key}.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if obj.exists():
            return obj
        partial = directory / f'{key}.{os.getpid()}.partial.o'
        result = subprocess.run([*arguments, '-c', str(source), '-o', str(partial)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            partial.unlink(missing_ok=True)
            raise AssertionError(result.stderr)
        os.replace(partial, obj)
    return obj


@lru_cache(maxsize=None)
def _compile(flags, extra_sources, exclude):
    selected = tuple(p for p in sources() if p.name not in exclude)
    return _compile_selected(selected, flags, extra_sources)


@lru_cache(maxsize=None)
def _compile_selected(selected, flags, extra_sources):
    output = Path(tempfile.mkdtemp(dir=build_root('products'))) / 'service'
    instrumentation = tuple(instrumentation_flags())
    objects = [_compile_object(Path(source), flags, instrumentation)
               for source in (*selected, *extra_sources)]
    result = subprocess.run(['cc', *instrumentation, *(str(p) for p in objects), '-o', str(output)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return output
