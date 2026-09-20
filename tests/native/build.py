"""Compile the same explicit source lists as the device build, once per target."""
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import os
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[2]
_BUILD = tempfile.TemporaryDirectory(prefix='tc-native-build-')


def binary_name(target):
    return 'discoveryd' if target == 'discovery' else target


def build_root(kind):
    coverage = os.environ.get('TC_NATIVE_COVERAGE_DIR')
    if coverage:
        path = Path(coverage) / kind
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(_BUILD.name)

def sources(target: str) -> list[Path]:
    return [ROOT / 'build' / line for line in
            (ROOT / 'build/native' / f'{target}.sources').read_text().splitlines() if line]


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

def compile_native(target, output, *, flags=(), extra_sources=(), exclude=()):
    binary = _compile(target, tuple(flags), tuple(extra_sources), tuple(exclude))
    shutil.copy2(binary, output)
    return output


@lru_cache(maxsize=None)
def _compile(target, flags, extra_sources, exclude):
    output = Path(tempfile.mkdtemp(dir=build_root('products'))) / binary_name(target)
    selected = [p for p in sources(target) if p.name not in exclude]
    role_flags = ['-DTC_SERVICE_MULTICALL', '-D_DNS_SD_LIBDISPATCH=0'] if target == 'service' else []
    common = ['cc', '-D_GNU_SOURCE', '-DTC_NATIVE_TEST', *role_flags, '-Wall', '-Wextra', '-Werror',
              '-Wno-sign-compare', '-Wno-unterminated-string-initialization',
              *instrumentation_flags(), *flags]
    objects = []
    for index, source in enumerate([*selected, *extra_sources]):
        obj = output.parent / f'{index}.o'
        # Unmodified TweetNaCl uses signed shifts in field normalization. Keep
        # ASan and the other UB checks, but don't rewrite cryptography in a split.
        vendor_flags = ['-fno-sanitize=shift'] if Path(source).name == 'tweetnacl.c' else []
        # The vendored Apple stub is compiled unchanged; see build/native/dnssd/README.md.
        if 'dnssd' in Path(source).parts:
            vendor_flags += ['-Wno-unused-but-set-variable', *stub_platform_flags()]
        result = subprocess.run([*common, *vendor_flags, '-c', str(source), '-o', str(obj)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)
        objects.append(obj)
    result = subprocess.run(['cc', *instrumentation_flags(), *(str(p) for p in objects), '-o', str(output)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return output
