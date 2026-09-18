import os
from pathlib import Path
import subprocess
import pytest
from tests.native.build import ROOT

@pytest.mark.parametrize('module', ['response', 'scheduler'])
def test_unit_module(tmp_path, module):
    native = ROOT / 'build/native/telemetry'
    output = tmp_path / module
    flags = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer'] if os.environ.get('TC_NATIVE_SANITIZERS') else []
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', *flags, '-I', str(native),
                    str(native / f'{module}.c'), str(Path(__file__).parent / 'unit' / f'test_{module}.c'),
                    '-o', str(output)], check=True, capture_output=True, timeout=60)
    subprocess.run([str(output)], check=True, capture_output=True, timeout=5)


def test_payload_device_boundary(tmp_path):
    from hashlib import sha512
    import json
    native = ROOT / 'build/native'
    output = tmp_path / 'payload'
    # Mock plan collection, not the payload schema: isolate router-ID derivation
    # while exercising the same serialization shipped on devices.
    result = subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
        '-Wno-unterminated-string-initialization', '-I', str(native / 'telemetry'), '-I', str(native / 'common'),
        str(native / 'telemetry/payload.c'), str(native / 'vendor/tweetnacl.c'),
        str(native / 'vendor/random.c'), str(Path(__file__).parent / 'unit/test_payload.c'), '-o', str(output)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    expected_input = 'namespace=timecapsulesmb-router-heartbeat-v1\nsyAP=106\nsyAM=TimeCapsule6,106\nsyNm=Name\n"quoted"\n'
    assert payload['schema_version'] == 2
    assert payload['router_mode'] == 'unknown' and payload['links'] == []
    assert payload['plan_error'] == 'mode'
    assert payload['router_id'] == 'tc1-' + sha512(expected_input.encode()).hexdigest()[:64]
