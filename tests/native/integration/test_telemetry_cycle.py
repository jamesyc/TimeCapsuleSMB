from __future__ import annotations
import json
from hashlib import sha512
import os
from pathlib import Path
import subprocess
import threading
import time
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from tests.native.build import ROOT, compile_service

# Hang guards only. Under ASan on shared hosted macOS runners, process startup
# alone can take seconds; tests with real deadlines assert elapsed time
# themselves.
HANG_TIMEOUT = 20


def telemetry_command(binary, *args):
    return [str(binary), 'telemetry', *args]

@pytest.fixture(scope='module')
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp('telemetry-integration')
    key = ECC.construct(curve='Ed25519', seed=bytes(range(32)))
    signer = eddsa.new(key, 'rfc8032')
    fixture = root / 'debug'
    subprocess.run(['cc', str(Path(__file__).with_name('debug_fixture.c')), '-o', str(fixture)], check=True, timeout=30)
    acp = root / 'acp'
    subprocess.run(['cc', str(Path(__file__).with_name('acp_fixture.c')), '-o', str(acp)], check=True, timeout=30)
    state = {'mode': 'false', 'calls': [], 'payloads': [], 'hold': threading.Event(), 'signature_started': threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def answer(self, body, status=200):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_POST(self):
            state['calls'].append(('POST', self.path))
            raw = self.rfile.read(int(self.headers['Content-Length']))
            payload = json.loads(raw)
            state['payloads'].append(payload)
            mode = state['mode']
            if mode == 'http_error': return self.answer(b'{"DEBUG":true}', 500)
            if mode == 'malformed': return self.answer(b'{"DEBUG":true}junk')
            if mode == 'oversized': return self.answer(b' ' * 6000)
            if mode == 'legacy': return self.answer(b'{"ok":true,"accepted":1}')
            response = {'DEBUG': mode != 'false'}
            if response['DEBUG']:
                signed_body = b'old request' if mode == 'replay' else raw
                if mode == 'swapped_device':
                    signed_body = json.dumps({**payload, 'router_id': 'other-router'}).encode()
                message = f"tc-debug-v1\n{sha512(signed_body).hexdigest()}\n".encode()
                response['DEBUG_SIGNATURE'] = signer.sign(message).hex()
                if mode == 'tampered_authorization':
                    signature = bytearray.fromhex(response['DEBUG_SIGNATURE'])
                    signature[0] ^= 1
                    response['DEBUG_SIGNATURE'] = signature.hex()
                if mode == 'forged_authorization':
                    attacker = ECC.construct(curve='Ed25519', seed=bytes(reversed(range(32))))
                    response['DEBUG_SIGNATURE'] = eddsa.new(attacker, 'rfc8032').sign(message).hex()
            if mode == 'unsigned': response.pop('DEBUG_SIGNATURE')
            self.answer(json.dumps(response).encode())
        def do_GET(self):
            state['calls'].append(('GET', self.path))
            binary = b'not an executable' if state['mode'] == 'bad_executable' else fixture.read_bytes()
            mode = state['mode']
            if mode in {'partial_binary', 'partial_signature'} and (self.path.endswith('.sig') == (mode == 'partial_signature')):
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.end_headers()
                self.wfile.write(b'truncated')
                self.close_connection = True
                return
            if self.path.endswith('.sig'):
                if mode == 'hold_signature':
                    state['signature_started'].set()
                    state['hold'].wait(timeout=HANG_TIMEOUT)

                if mode == 'missing_signature': return self.answer(b'', 404)
                signature = signer.sign(binary)
                if mode == 'bad_signature': signature = b'\0' * 64
                return self.answer(signature)
            if mode == 'large_binary': return self.answer(b'x' * (1048576 + 50))
            if mode == 'tamper': binary = binary[:-1] + bytes([binary[-1] ^ 1])
            self.answer(binary)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    public = key.public_key().export_key(format='raw')
    (root / 'work').mkdir()
    (root / 'work/keep.txt').write_text('unrelated runtime file')
    config = root / 'test_config.h'
    config.write_text('\n'.join([
        f'#define HEARTBEAT_ENDPOINT "{base}/v1/router-heartbeats"',
        f'#define TC_DEBUG_BASE_URL "{base}/downloads/bin/debug"',
        '#define TC_DEBUG_QUERY ""',
        '#define TC_CLEANUP_INTERVAL_SECONDS 1',
        f'#define TC_ACP_PATH "{acp}"',
        f'#define TC_TELEMETRY_WORK_ROOT "{root}/work"',
        f'#define HEARTBEAT_FLASH_CONFIG_PATH "{root}/config"',
        f'#define TC_FLASH_CONFIG_PATH "{root}/config"',
        '#define TC_HEARTBEAT_PUBLIC_KEY_BYTES ' + ','.join(str(v) for v in public),
    ]))
    (root / 'config').write_text("TC_DEPLOY_RELEASE_TAG='test-release'\n")
    binary = compile_service(root / 'service', flags=['-include', str(config), '-I', str(ROOT / 'build/native')],
                             exclude=['iflist.c'], extra_sources=[ROOT / 'tests/native/integration/iflist_fixture.c'])
    yield root, binary, state
    server.shutdown(); server.server_close(); thread.join(timeout=HANG_TIMEOUT)

@pytest.fixture
def cycle(rig, tmp_path):
    root, binary, state = rig
    (root / 'config').write_text("TC_DEPLOY_RELEASE_TAG='test-release'\n")
    state['calls'].clear(); state['payloads'].clear(); state['mode'] = 'false'
    state['hold'].clear(); state['signature_started'].clear()
    env = {**os.environ, 'TC_TEST_MARKER': str(tmp_path / 'marker')}
    # Prevent user curl/proxy configuration from affecting the local test server.
    for name in list(env):
        if name.lower().endswith('_proxy'): env.pop(name)
    def run(mode, **changes):
        state['mode'] = mode
        result = subprocess.run(telemetry_command(binary, '--once', 'manual'), env={**env, **changes}, capture_output=True, text=True, timeout=HANG_TIMEOUT)
        assert not (root / 'work/debug').exists()
        assert not (root / 'work/debug.sig').exists()
        assert (root / 'work/keep.txt').read_text() == 'unrelated runtime file'
        return result
    return run, state, tmp_path / 'marker', binary, env

@pytest.mark.parametrize('mode', ['false', 'legacy'])
def test_no_debug_means_post_only(cycle, mode):
    run, state, marker, *_ = cycle
    assert run(mode).returncode == 0
    assert state['calls'] == [('POST', '/v1/router-heartbeats')]
    assert not marker.exists()
    payload = state['payloads'][0]
    assert payload['target_lane'] == '6' and len(payload['debug_nonce']) == 32
    assert payload['deploy_release_tag'] == 'test-release'
    assert payload['reason'] == 'manual' and payload['router_id'].startswith('tc1-')

@pytest.mark.parametrize('mode', ['true', 'child_fail'])
def test_verified_debug_executes_and_cleans(cycle, mode):
    run, state, marker, *_ = cycle
    result = run(mode, **({'TC_TEST_FAIL': '1'} if mode == 'child_fail' else {}))
    assert result.returncode == (1 if mode == 'child_fail' else 0)
    assert marker.read_text().startswith('executed\n')
    assert state['calls'] == [('POST', '/v1/router-heartbeats'), ('GET', '/downloads/bin/debug6'), ('GET', '/downloads/bin/debug6.sig')]

@pytest.mark.parametrize('mode', ['http_error', 'malformed', 'oversized', 'unsigned', 'replay', 'swapped_device',
                                 'tampered_authorization', 'forged_authorization',
                                 'tamper', 'missing_signature', 'bad_signature', 'large_binary', 'partial_binary', 'partial_signature', 'bad_executable'])
def test_failure_never_executes(cycle, mode):
    run, state, marker, *_ = cycle
    assert run(mode).returncode != 0
    assert not marker.exists()
    if mode in {'http_error', 'malformed', 'oversized', 'unsigned', 'replay', 'swapped_device',
                'tampered_authorization', 'forged_authorization'}:
        assert len(state['calls']) == 1


def test_running_debug_survives_stop_and_excludes_another_cycle(cycle, tmp_path):
    _, state, marker, binary, env = cycle
    state['mode'] = 'true'
    finish = tmp_path / 'finish'
    process = subprocess.Popen(telemetry_command(binary, '--once', 'manual'), env={**env, 'TC_TEST_FINISH': str(finish)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(marker.exists)
        process.terminate()
        assert process.poll() is None
        blocked = subprocess.run(telemetry_command(binary, '--once'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
        assert blocked.returncode == 75
        finish.touch()
        assert process.wait(timeout=HANG_TIMEOUT) == 0
        assert len(state['calls']) == 3
    finally:
        finish.touch()
        process.communicate(timeout=HANG_TIMEOUT)


def test_print_payload_has_no_network(cycle):
    _, state, _, binary, env = cycle
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'manual'), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert json.loads(result.stdout)['reason'] == 'manual'
    assert state['calls'] == []


@pytest.mark.parametrize('args', [['--once'], ['--daemon']])
@pytest.mark.parametrize('setting', ['TELEMETRY=false\n', "TELEMETRY='false'\n", 'TELEMETRY="false"\n', '  TELEMETRY = false  \r\n'])
def test_device_opt_out_exits_without_network_or_debug_files(cycle, rig, args, setting):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    (root / 'config').write_text(setting)
    state['mode'] = 'true'
    result = subprocess.run(telemetry_command(binary, *args), env=env, capture_output=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert state['calls'] == [] and not marker.exists()
    assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']


@pytest.mark.parametrize('setting', [None, '', '# TELEMETRY=false\n', 'OTHER_TELEMETRY=false\n', 'TELEMETRY=falsehood\n', 'TELEMETRY=true\n'])
def test_reporting_remains_enabled_without_explicit_opt_out(cycle, rig, setting):
    run, state, *_ = cycle
    root, _, _ = rig
    if setting is None: (root / 'config').unlink()
    else: (root / 'config').write_text(setting)
    assert run('false').returncode == 0
    assert state['calls'] == [('POST', '/v1/router-heartbeats')]


def test_idle_daemon_exits_when_device_config_disables_telemetry(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        (root / 'config').write_text('TELEMETRY=false\n')
        assert process.wait(timeout=HANG_TIMEOUT) == 0
        assert len(state['calls']) == 1
    finally:
        if process.poll() is None: process.terminate()
        process.communicate(timeout=HANG_TIMEOUT)


def test_opt_out_keeps_local_cleanup_available(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    (root / 'config').write_text('TELEMETRY=false\n')
    (root / 'work/debug').write_text('stale')
    (root / 'work/debug.sig').write_text('stale')
    result = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']
    assert state['calls'] == []


def wait_until(predicate, seconds=HANG_TIMEOUT):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.01)
    assert predicate()


def test_cleanup_removes_stale_files_without_network(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    for name in ('debug', 'debug.sig'):
        (root / 'work' / name).write_bytes(b'interrupted download')
    for _ in range(2):
        result = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']
    assert state['calls'] == []


def test_cleanup_unlinks_symlinks_without_touching_targets(cycle, rig, tmp_path):
    _, _, _, binary, env = cycle
    root, _, _ = rig
    target = tmp_path / 'precious'; target.write_text('preserved')
    directory = tmp_path / 'directory'; directory.mkdir(); (directory / 'file').write_text('preserved')
    (root / 'work/debug').symlink_to(target)
    (root / 'work/debug.sig').symlink_to(directory)
    result = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == (directory / 'file').read_text() == 'preserved'
    assert not (root / 'work/debug').is_symlink()
    assert not (root / 'work/debug.sig').is_symlink()


def test_directory_at_reserved_name_is_reported_and_blocks_download(cycle, rig):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    directory = root / 'work/debug'; directory.mkdir()
    (directory / 'file').write_text('preserve')
    try:
        for args in (['--cleanup'], ['--once']):
            result = subprocess.run(telemetry_command(binary, *args), env=env, capture_output=True, text=True, timeout=HANG_TIMEOUT)
            assert result.returncode == 1
            assert 'cannot remove' in result.stderr
        assert (directory / 'file').read_text() == 'preserve'
        assert not marker.exists() and state['calls'] == []
    finally:
        (directory / 'file').unlink(); directory.rmdir()


def test_successful_child_cannot_hide_cleanup_error(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'true'
    try:
        result = subprocess.run(telemetry_command(binary, '--once'), env={**env, 'TC_TEST_LEAVE_SIG_DIR': '1'},
                                capture_output=True, text=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 1
        assert 'cannot remove' in result.stderr and 'debug.sig' in result.stderr
        assert not (root / 'work/debug').exists()
    finally:
        (root / 'work/debug.sig').rmdir()


@pytest.mark.parametrize('kill', [False, True])
def test_interrupted_signature_download_cleans_or_recovers_nonexecutable_binary(cycle, rig, kill):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'hold_signature'
    process = subprocess.Popen(telemetry_command(binary, '--once'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert state['signature_started'].wait(timeout=HANG_TIMEOUT)
        assert (root / 'work/debug').exists()
        assert (root / 'work/debug').stat().st_mode & 0o111 == 0
        if kill: process.kill()
        else: process.terminate()
        assert process.wait(timeout=HANG_TIMEOUT) != 0
        if kill:
            assert (root / 'work/debug').exists()
            cleanup = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
            assert cleanup.returncode == 0, cleanup.stderr
        assert not marker.exists()
        assert not (root / 'work/debug').exists()
        assert not (root / 'work/debug.sig').exists()
    finally:
        state['hold'].set()
        if process.poll() is None: process.kill()
        process.communicate(timeout=HANG_TIMEOUT)


@pytest.mark.parametrize('detach', [False, True])
def test_inherited_owner_survives_parent_death_and_cleanup_waits(cycle, rig, tmp_path, detach):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'true'
    finish = tmp_path / 'finish'
    child_env = {**env, 'TC_TEST_FINISH': str(finish)}
    if detach: child_env['TC_TEST_DETACH'] = '1'
    process = subprocess.Popen(telemetry_command(binary, '--once'), env=child_env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        wait_until(lambda: marker.exists() and marker.read_text().startswith('executed\n'))
        if detach:
            assert process.wait(timeout=HANG_TIMEOUT) == 0
        else:
            process.kill(); process.wait(timeout=HANG_TIMEOUT)
        assert (root / 'work/debug').exists()
        assert not (root / 'work/debug.sig').exists()
        for args in (['--cleanup'], ['--once']):
            blocked = subprocess.run(telemetry_command(binary, *args), env=env, capture_output=True, timeout=HANG_TIMEOUT)
            assert blocked.returncode == 75
        assert len(state['calls']) == 3
        finish.touch()
        def recovered():
            return subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT).returncode == 0
        wait_until(recovered)
        assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']
    finally:
        finish.touch()
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.communicate(timeout=HANG_TIMEOUT)


def test_debug_crash_is_reaped_and_cleaned(cycle):
    run, _, marker, *_ = cycle
    assert run('true', TC_TEST_CRASH='1').returncode == 1
    assert marker.read_text().startswith('executed\n')


def test_daemon_housekeeping_cleans_between_heartbeats(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'false'
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        # Simulate a crashed manual invocation while the scheduler is idle.
        import fcntl
        fd = os.open(root / 'work', os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        (root / 'work/debug').write_bytes(b'partial')
        (root / 'work/debug.sig').write_bytes(b'partial')
        os.close(fd)
        wait_until(lambda: not (root / 'work/debug').exists() and not (root / 'work/debug.sig').exists())
        assert len(state['calls']) == 1
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_shared_ram_root_requires_sticky_permissions(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    work = root / 'work'
    original_mode = work.stat().st_mode & 0o7777
    (work / 'debug').write_text('stale')
    try:
        work.chmod(0o777)
        result = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 1 and (work / 'debug').exists()
        work.chmod(0o1777)
        result = subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 0 and not (work / 'debug').exists()
        assert state['calls'] == []
    finally:
        work.chmod(original_mode)
        (work / 'debug').unlink(missing_ok=True)


@pytest.fixture(scope='module')
def production_collector(rig):
    return rig[1]


@pytest.fixture(scope='module')
def short_collector(rig):
    # Only deadline tests need a shortened clock. Ordinary HTTP/JSON tests
    # use the production allowance so fixture startup isn't the assertion.
    root, _, _ = rig
    return compile_service(root / 'service-short-timeout',
                           flags=['-include', str(root / 'test_config.h'),
                                  '-DTC_ACP_TIMEOUT_SECONDS=1', '-I', str(ROOT / 'build/native')],
                           exclude=['iflist.c'], extra_sources=[ROOT / 'tests/native/integration/iflist_fixture.c'])


@pytest.fixture
def acp_calls(tmp_path):
    calls = tmp_path / 'acp-calls'
    yield calls
    # A failed assertion or outer subprocess timeout must not strand a fixture
    # that deliberately ignores TERM or leaves a descendant holding stdout.
    if calls.exists():
        for line in calls.read_text().splitlines():
            if line.split()[2] != 'parent': continue
            try: os.killpg(int(line.split()[1]), signal.SIGKILL)
            except ProcessLookupError: pass


def assert_collectors_stopped(calls):
    for line in calls.read_text().splitlines():
        pid = int(line.split()[1])
        def stopped():
            # Container PID 1 may leave adopted descendants as zombies.
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists():
                try:
                    if stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z': return True
                # Linux procfs can return ESRCH after open when the process exits.
                except (FileNotFoundError, ProcessLookupError): return True
            try: os.kill(pid, 0)
            except ProcessLookupError: return True
            return False
        wait_until(stopped)

@pytest.mark.parametrize('stat_error', [FileNotFoundError, ProcessLookupError])
def test_collector_exit_during_proc_stat_read_is_stopped(tmp_path, monkeypatch, stat_error):
    calls = tmp_path / 'calls'
    calls.write_text('syAP 12345 descendant\n')
    stat_path = Path('/proc/12345/stat')
    read_text = Path.read_text
    exists = Path.exists

    def read(path, *args, **kwargs):
        if path == stat_path:
            raise stat_error('collector exited during read')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'exists', lambda path: True if path == stat_path else exists(path))
    monkeypatch.setattr(Path, 'read_text', read)
    # Both errors mean the process is gone; no kill(0) probe is needed.
    def unexpected_probe(*args):
        pytest.fail('probed an already-exited collector')
    monkeypatch.setattr(os, 'kill', unexpected_probe)
    assert_collectors_stopped(calls)


@pytest.mark.parametrize('state,probe_error,expected', [
    ('Z', None, True),
    ('S', ProcessLookupError, True),
    ('S', None, False),
])
def test_collector_cleanup_distinguishes_zombie_gone_and_live(tmp_path, monkeypatch, state, probe_error, expected):
    calls = tmp_path / 'calls'
    calls.write_text('syAP 12345 descendant\n')
    stat_path = Path('/proc/12345/stat')
    read_text = Path.read_text
    exists = Path.exists

    def read(path, *args, **kwargs):
        if path == stat_path:
            return f'12345 (acp fixture) {state} 1 12345'
        return read_text(path, *args, **kwargs)

    def probe(pid, signal_number):
        assert (pid, signal_number) == (12345, 0)
        assert state != 'Z'
        if probe_error:
            raise probe_error('collector exited before probe')

    monkeypatch.setattr(Path, 'exists', lambda path: True if path == stat_path else exists(path))
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(os, 'kill', probe)
    def check(predicate):
        assert predicate() is expected
    monkeypatch.setitem(globals(), 'wait_until', check)
    assert_collectors_stopped(calls)


@pytest.mark.parametrize('mode', ['hang', 'line_hang', 'closed_hang', 'ignore_term', 'descendant', 'oversized', 'crash', 'drip', 'drip_after_line', 'nul'])
def test_acp_failure_aborts_cycle_reaps_children_and_releases_lock(cycle, short_collector, acp_calls, mode):
    _, state, marker, binary, env = cycle
    calls = acp_calls
    state['mode'] = 'true'
    started = time.monotonic()
    result = subprocess.run(telemetry_command(short_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': mode,
                            'TC_TEST_ACP_CALLS': str(calls)}, capture_output=True, text=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 1
    assert time.monotonic() - started < 5
    assert 'acp: syAP' in result.stderr
    if mode in ('drip', 'drip_after_line'): assert 'timed out' in result.stderr
    assert state['calls'] == [] and not marker.exists()
    assert_collectors_stopped(calls)
    assert subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT).returncode == 0
    # A fresh invocation can collect/post after the failed owner exits.
    state['mode'] = 'false'
    assert subprocess.run(telemetry_command(binary, '--once'), env=env, capture_output=True, timeout=HANG_TIMEOUT).returncode == 0
    assert len(state['calls']) == 1


@pytest.mark.parametrize('value,expected', [
    ('x' * 255, 'x' * 255),
    ('  Café "Capsule"\t\r\nignored second line', 'Café "Capsule"'),
    ('\t \r', ''),
])
@pytest.mark.parametrize('no_newline', [False, True])
def test_acp_preserves_complete_first_line_at_eof_and_buffer_boundary(cycle, value, expected, no_newline):
    run, state, *_ = cycle
    options = {'TC_TEST_ACP_MODE': 'value', 'TC_TEST_ACP_KEY': 'syNm', 'TC_TEST_ACP_VALUE': value}
    if no_newline: options['TC_TEST_ACP_NO_NEWLINE'] = '1'
    assert run('false', **options).returncode == 0
    assert len(state['calls']) == 1
    assert state['payloads'][0]['device_name'] == expected


def test_acp_rejects_a_first_line_one_byte_beyond_the_buffer(cycle):
    run, state, marker, *_ = cycle
    result = run('true', TC_TEST_ACP_MODE='value', TC_TEST_ACP_KEY='syNm', TC_TEST_ACP_VALUE='x' * 256)
    assert result.returncode == 1 and 'oversized output' in result.stderr
    assert state['calls'] == [] and not marker.exists()


def test_acp_exec_does_not_inherit_workspace_or_pipe_descriptors(cycle):
    run, state, *_ = cycle
    assert run('false', TC_TEST_ACP_MODE='inspect_fds').returncode == 0
    # A descriptor leak makes the fixture fail; missing optional ACP fields
    # must not disguise that failure as an otherwise successful heartbeat.
    assert state['payloads'][0]['device_syap'] == '119'


def test_acp_exec_failure_aborts_without_posting(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    acp = root / 'acp'
    permissions = acp.stat().st_mode & 0o777
    try:
        acp.chmod(0o600)
        result = subprocess.run(telemetry_command(binary, '--once'), env=env, capture_output=True, text=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 1 and 'exec' in result.stderr
        assert state['calls'] == []
    finally:
        acp.chmod(permissions)


@pytest.mark.parametrize('key', ['syAM', 'syNm', 'sySN', 'waMA', 'raMA'])
def test_acp_timeout_at_later_field_never_posts_partial_identity(cycle, short_collector, acp_calls, key):
    _, state, _, _, env = cycle
    calls = acp_calls
    result = subprocess.run(telemetry_command(short_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': 'hang',
                            'TC_TEST_ACP_KEY': key, 'TC_TEST_ACP_CALLS': str(calls)},
                            capture_output=True, text=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 1 and f'acp: {key} timed out' in result.stderr
    assert state['calls'] == []
    assert calls.read_text().splitlines()[-1].startswith(key + ' ')
    assert_collectors_stopped(calls)


@pytest.mark.parametrize('args', [['--daemon'], ['--once'], ['--print-payload']])
@pytest.mark.parametrize('mode,stop_signal', [('ignore_term', signal.SIGTERM), ('descendant', signal.SIGTERM),
                                             ('closed_hang', signal.SIGINT)])
def test_stop_during_acp_collection_is_prompt_even_with_long_timeout(cycle, production_collector, acp_calls, args, mode, stop_signal):
    _, state, _, _, env = cycle
    calls = acp_calls
    process = subprocess.Popen(telemetry_command(production_collector, *args), env={**env,
        'TC_TEST_ACP_MODE': mode, 'TC_TEST_ACP_CALLS': str(calls)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: calls.exists() and calls.stat().st_size > 0)
        if mode == 'descendant': wait_until(lambda: ' descendant' in calls.read_text())
        started = time.monotonic()
        process.send_signal(stop_signal)
        out, err = process.communicate(timeout=HANG_TIMEOUT)
        assert process.returncode == 1 and b'cancelled' in err
        assert time.monotonic() - started < 3
        assert out == b'' and state['calls'] == []
        assert_collectors_stopped(calls)
    finally:
        if process.poll() is None: process.kill()
        process.communicate(timeout=HANG_TIMEOUT)


@pytest.mark.parametrize('mode', ['empty', 'nonzero', 'drain'])
def test_acp_unavailable_fields_and_extra_output_preserve_normal_reporting(cycle, mode):
    run, state, *_ = cycle
    assert run('false', TC_TEST_ACP_MODE=mode).returncode == 0
    payload = state['payloads'][0]
    assert payload['device_syap'] == ('119' if mode == 'drain' else '')
    assert payload['router_id'].startswith('tc1-')


def test_default_acp_deadline_allows_slow_success_and_stops_at_twenty_seconds(cycle, production_collector, acp_calls):
    _, state, _, _, env = cycle
    result = subprocess.run(telemetry_command(production_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': 'slow'},
                            capture_output=True, timeout=25)
    assert result.returncode == 0 and len(state['calls']) == 1
    state['calls'].clear()
    calls = acp_calls
    started = time.monotonic()
    result = subprocess.run(telemetry_command(production_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': 'hang',
                            'TC_TEST_ACP_CALLS': str(calls)}, capture_output=True, text=True, timeout=25)
    elapsed = time.monotonic() - started
    assert result.returncode == 1 and 'timed out' in result.stderr
    assert 20 <= elapsed < 24
    assert state['calls'] == []
    assert_collectors_stopped(calls)


def test_each_acp_command_gets_its_own_deadline(cycle, short_collector, acp_calls):
    _, state, _, _, env = cycle
    # Six 300ms probes exceed this fixture's 1s timeout in aggregate, but each
    # individual probe fits. No whole-payload deadline should cut them short.
    result = subprocess.run(telemetry_command(short_collector, '--once', 'manual'),
                            env={**env, 'TC_TEST_ACP_MODE': 'slow_each', 'TC_TEST_ACP_KEY': '*',
                                 'TC_TEST_ACP_CALLS': str(acp_calls)},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    # Schema v2 then collects the device plan's ten keys under one 30 s
    # budget, again one child at a time with its own per-key deadline.
    assert [line.split()[0] for line in acp_calls.read_text().splitlines()] == [
        'syAP', 'syAM', 'syNm', 'sySN', 'waMA', 'raMA',
        'raNA', 'raDS', 'waNM', 'usbF', 'laIP', 'waIP', 'waLL', 'gnRo', 'syNm', 'waMA',
    ]
    assert len(state['calls']) == 1


def test_print_payload_uses_current_schema_without_posting(cycle):
    """The diagnostic command emits the shipped schema without sending HTTP."""
    _run, state, _marker, binary, env = cycle
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'manual'), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['schema_version'] == 2
    assert 'router_mode' in payload and 'links' in payload
    assert state['calls'] == []


def test_schema_v2_payload_reports_plan_availability(cycle):
    """v3.1.0 posts schema v2: the plan fields report raw availability. The
    fake acp answers 0x77 for every key, which decodes as no router mode and
    no permissions."""
    run, state, *_ = cycle
    assert run('false').returncode == 0
    payload = state['payloads'][0]
    assert payload['schema_version'] == 2
    assert payload['router_id'].startswith('tc1-') and payload['deploy_release_tag'] == 'test-release'
    assert payload['router_mode'] == 'unknown'
    assert payload['wan_setup_allowed'] is None and payload['disks_over_wan'] is None
    assert payload['guest_enabled'] is False
    assert payload['plan_error'] == 'mode'
    # Native NBNS is always on, so the router no longer reports a constant.
    assert 'nbns_enabled' not in payload
    assert payload['debug_logging'] is False
    assert payload['advertise_afp'] is False
    assert 'mdns_daemon' not in payload and 'mdns_registrant_status' not in payload
    assert isinstance(payload['links'], list)
    for link in payload['links']:
        assert set(link) == {'name', 'role', 'families'}
        assert link['role'] in {'lan', 'wan', 'guest', 'isolated'}
        assert set(link['families']) <= {'ipv4', 'ipv6'}


@pytest.mark.parametrize('nbns,smb_debug,mdns_debug,afp', [
    (0, 0, 0, 0), (1, 0, 1, 1), (1, 1, 0, 0), (0, 1, 1, 1),
])
def test_compact_settings_and_healthy_plan(cycle, rig, nbns, smb_debug, mdns_debug, afp):
    run, state, *_ = cycle
    root, *_ = rig
    (root / 'config').write_text(
        f'NBNS_ENABLED={nbns}\nSMBD_DEBUG_LOGGING={smb_debug}\n'
        f'MDNS_DEBUG_LOGGING={mdns_debug}\nMDNS_ADVERTISE_AFP={afp}\n')
    assert run('false', TC_TEST_PLAN_MODE='nat').returncode == 0
    payload = state['payloads'][0]
    # A stale NBNS_ENABLED line in the config must not bring the field back.
    assert 'nbns_enabled' not in payload
    assert payload['debug_logging'] == bool(smb_debug or mdns_debug)
    assert payload['advertise_afp'] == bool(afp)
    assert 'plan_error' not in payload
    assert not {'mdns_daemon', 'mdns_registrant_status', 'telemetry', 'acp_ok'} & payload.keys()
    fields = {key: payload[key] for key in ('debug_logging', 'advertise_afp')}
    assert len(json.dumps(fields, separators=(',', ':'))) <= 45


@pytest.mark.parametrize('changes,reason', [
    ({'TC_TEST_ACP_MODE': 'empty', 'TC_TEST_ACP_KEY': 'usbF'}, 'usbF'),
    ({'TC_TEST_ACP_MODE': 'value', 'TC_TEST_ACP_KEY': 'laIP', 'TC_TEST_ACP_VALUE': 'invalid'}, 'laIP'),
    ({'TC_TEST_IFLIST': 'failed'}, 'iflist'),
    ({'TC_TEST_IFLIST': 'truncated'}, 'iflist-truncated'),
])
def test_plan_error_is_short_and_omitted_after_recovery(cycle, changes, reason):
    run, state, *_ = cycle
    assert run('false', TC_TEST_PLAN_MODE='nat', **changes).returncode == 0
    assert state['payloads'][-1]['plan_error'] == reason
    assert run('false', TC_TEST_PLAN_MODE='nat').returncode == 0
    assert 'plan_error' not in state['payloads'][-1]


def test_unreadable_and_invalid_config_are_not_reported_as_false(cycle, rig):
    run, state, *_ = cycle
    root, *_ = rig
    (root / 'config').unlink()
    assert run('false', TC_TEST_PLAN_MODE='bridge').returncode == 0
    assert all(state['payloads'][-1][key] is None for key in ('debug_logging', 'advertise_afp'))
    (root / 'config').write_text('NBNS_ENABLED=invalid\nMDNS_DEBUG_LOGGING=bad\nSMBD_DEBUG_LOGGING=1\n')
    assert run('false', TC_TEST_PLAN_MODE='bridge').returncode == 0
    assert 'nbns_enabled' not in state['payloads'][-1]
    assert state['payloads'][-1]['debug_logging'] is True
    assert state['payloads'][-1]['advertise_afp'] is False
