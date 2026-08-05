"""Run native behavioral tests and retain an LLVM HTML/summary coverage report."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tests.native.build import ROOT


def llvm_tool(name):
    found = shutil.which(name)
    if found:
        return found
    return subprocess.check_output(['xcrun', '--find', name], text=True).strip()


def main():
    output = Path(tempfile.mkdtemp(prefix='tc-native-coverage-'))
    env = {**os.environ, 'TC_NATIVE_COVERAGE': '1', 'TC_NATIVE_COVERAGE_DIR': str(output),
           'LLVM_PROFILE_FILE': str(output / '%m-%p.profraw')}
    result = subprocess.run([str(ROOT / '.venv/bin/pytest'), 'tests/native/integration',
                             'tests/test_deploy_modules.py', '-q'], cwd=ROOT, env=env)
    if result.returncode:
        return result.returncode
    profiles = sorted(output.glob('*.profraw'))
    binaries = [p for kind in ('products', 'cases') for p in (output / kind).rglob('*')
                if p.is_file() and p.suffix != '.o' and os.access(p, os.X_OK)]
    if not profiles or not binaries:
        raise RuntimeError('native tests produced no coverage data')
    data = output / 'native.profdata'
    subprocess.run([llvm_tool('llvm-profdata'), 'merge', '-sparse', *(str(p) for p in profiles),
                    '-o', str(data)], check=True)
    objects = [arg for p in binaries[1:] for arg in ('-object', str(p))]
    common = [str(binaries[0]), *objects, '-instr-profile', str(data),
              '-ignore-filename-regex', r'(tests/|vendor/)']
    subprocess.run([llvm_tool('llvm-cov'), 'report', *common], check=True)
    subprocess.run([llvm_tool('llvm-cov'), 'show', *common, '-format=html',
                    '-output-dir', str(output / 'html')], check=True)
    print(f'Native coverage: {output / "html/index.html"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
