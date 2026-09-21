from pathlib import Path
import subprocess
import pytest
from tests.native.build import ROOT, compile_modules

@pytest.mark.parametrize('module', ['response', 'scheduler'])
def test_unit_module(tmp_path, module):
    native = ROOT / 'build/native/telemetry'
    output = tmp_path / module
    compile_modules(output, (f'native/telemetry/{module}.c',), flags=('-I', str(native)),
                    extra_sources=(Path(__file__).parent / 'unit' / f'test_{module}.c',))
    subprocess.run([str(output)], check=True, capture_output=True, timeout=5)


def test_payload_device_boundary(tmp_path):
    from hashlib import sha512
    import json
    native = ROOT / 'build/native'
    output = tmp_path / 'payload'
    # Mock plan collection, not the payload schema: isolate router-ID derivation
    # while exercising the same serialization shipped on devices.
    compile_modules(output, ('native/telemetry/payload.c', 'native/vendor/tweetnacl.c',
                             'native/vendor/random.c'),
                    flags=('-I', str(native / 'telemetry'), '-I', str(native / 'common')),
                    extra_sources=(Path(__file__).parent / 'unit/test_payload.c',))
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    expected_input = 'namespace=timecapsulesmb-router-heartbeat-v1\nsyAP=106\nsyAM=TimeCapsule6,106\nsyNm=Name\n"quoted"\n'
    assert payload['schema_version'] == 2
    assert payload['router_mode'] == 'unknown' and payload['links'] == []
    assert payload['plan_error'] == 'mode'
    assert payload['router_id'] == 'tc1-' + sha512(expected_input.encode()).hexdigest()[:64]
