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


@pytest.mark.parametrize(('mode', 'curl_sleep'), [('step', '0.2'), ('expire', '10')])
def test_http_deadline_uses_monotonic_clock(tmp_path, mode, curl_sleep):
    # The boot heartbeat is in flight when NTP steps the wall clock: a forward
    # step must not abort it, and a hung curl must still be killed on time.
    native = ROOT / 'build/native'
    curl = tmp_path / 'curl'
    curl.write_text('#!/bin/sh\ncat >/dev/null\nsleep "$FAKE_CURL_SLEEP"\nprintf \'ok\\n200\'\n')
    curl.chmod(0o755)
    output = tmp_path / 'http'
    compile_modules(output, ('native/telemetry/http.c', 'native/common/acp.c'),
                    flags=('-I', str(native / 'telemetry'), '-I', str(native / 'common'),
                           f'-DTC_CURL_PATH="{curl}"', '-DTC_HTTP_DEADLINE_MS=2000'),
                    extra_sources=(Path(__file__).parent / 'unit/test_http.c',))
    run = subprocess.run([str(output), mode], capture_output=True, text=True, timeout=15,
                         env={'PATH': '/usr/bin:/bin', 'FAKE_CURL_SLEEP': curl_sleep})
    assert run.returncode == 0, run.stderr
