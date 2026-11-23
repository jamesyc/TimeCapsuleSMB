from pathlib import Path
import subprocess
import pytest
from tests.native.build import ROOT, compile_modules

@pytest.mark.parametrize('module', ['response', 'planner', 'errclass'])
def test_unit_module(tmp_path, module):
    native = ROOT / 'build/native/telemetry'
    output = tmp_path / module
    compile_modules(output, (f'native/telemetry/{module}.c',), flags=('-I', str(native)),
                    extra_sources=(Path(__file__).parent / 'unit' / f'test_{module}.c',))
    subprocess.run([str(output)], check=True, capture_output=True, timeout=5)


def test_payload_device_boundary(tmp_path):
    import json
    native = ROOT / 'build/native'
    output = tmp_path / 'payload'
    # acp, the plan builder and the interface table are scripted in
    # test_payload.c; the payload and its heartbeat facts are the shipped code.
    compile_modules(output, ('native/telemetry/payload.c', 'native/telemetry/heartbeat_facts.c',
                             'native/telemetry/errclass.c', 'native/telemetry/health.c', 'native/telemetry/health_json.c',
                             'native/vendor/tweetnacl.c', 'native/vendor/random.c'),
                    flags=('-I', str(native / 'telemetry'), '-I', str(native / 'common')),
                    extra_sources=(Path(__file__).parent / 'unit/test_payload.c',))
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
    fresh, cached = (json.loads(line) for line in run.stdout.splitlines() if line)
    assert fresh['schema_version'] == 2 and fresh['agent_version'] == '4'
    assert fresh['router_id'] == 'serial123' and fresh['mac'] == '80:ea:96:e6:58:68'
    assert fresh['device_name'] == 'Name\n"quoted"' and fresh['router_mode'] == 'unknown'
    assert fresh['links'] == [] and 'plan_error' not in fresh
    assert cached['router_id'] == fresh['router_id'] and cached['plan_age_sec'] == 0
    assert fresh['delivery'] == {'failed': 0, 'since': None,
                                 'classes': {'local_resource': 0, 'acp_unavailable': 0, 'network': 0, 'server': 0}}
    assert cached['delivery']['failed'] == 3 and cached['delivery']['classes']['local_resource'] == 2


def test_heartbeat_uptime_ignores_wall_clock_steps(tmp_path):
    native = ROOT / 'build/native'
    output = tmp_path / 'uptime'
    compile_modules(output, ('native/telemetry/device.c', 'native/common/acp.c', 'native/common/config.c'),
                    flags=('-I', str(native / 'telemetry'), '-I', str(native / 'common')),
                    extra_sources=(Path(__file__).parent / 'unit/test_uptime.c',))
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=5)
    assert run.returncode == 0, run.stderr
