"""Compile the unified native service from the production source manifest."""
from pathlib import Path
import hashlib
import subprocess
import sys
import tempfile
import shutil
import os
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[2]
_BUILD = tempfile.TemporaryDirectory(prefix='tc-native-build-')


def build_root(kind):
    coverage = os.environ.get('TC_NATIVE_COVERAGE_DIR')
    if coverage:
        path = Path(coverage) / kind
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(_BUILD.name)

def sources() -> list[Path]:
    return [ROOT / 'build' / line for line in
            (ROOT / 'build/native/service.sources').read_text().splitlines() if line]


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
    shutil.copy2(binary, output)
    return output


def compile_modules(output, modules, *, flags=(), extra_sources=()):
    production = set(sources())
    selected = tuple(ROOT / 'build' / module for module in modules)
    if not set(selected) <= production:
        raise AssertionError("unit test module is not part of service.sources")
    binary = _compile_selected(selected, tuple(flags), tuple(extra_sources))
    shutil.copy2(binary, output)
    return output


def _compiler_flags(flags, instrumentation):
    return ('cc', '-D_GNU_SOURCE', '-DTC_NATIVE_TEST', '-DTC_SERVICE_MULTICALL', '-D_DNS_SD_LIBDISPATCH=0',
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


@lru_cache(maxsize=None)
def _compile_object(source, flags, instrumentation):
    vendor_flags = _vendor_flags(source)
    key = hashlib.sha256((str(source) + repr(flags) + repr(instrumentation) +
                          repr(vendor_flags)).encode()).hexdigest()[:20]
    directory = build_root('objects')
    directory.mkdir(parents=True, exist_ok=True)
    obj = directory / f'{key}.o'
    result = subprocess.run([*_compiler_flags(flags, instrumentation), *vendor_flags,
                             '-c', str(source), '-o', str(obj)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
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
