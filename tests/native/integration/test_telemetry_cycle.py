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
from tests.native.build import ROOT, compile_native

@pytest.fixture(scope='module')
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp('telemetry-integration')
    key = ECC.construct(curve='Ed25519', seed=bytes(range(32)))
    signer = eddsa.new(key, 'rfc8032')
    fixture = root / 'debug'
    subprocess.run(['cc', str(Path(__file__).with_name('debug_fixture.c')), '-o', str(fixture)], check=True, timeout=30)
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
                    state['hold'].wait(timeout=5)

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
        f'#define TC_TELEMETRY_WORK_ROOT "{root}/work"',
        f'#define HEARTBEAT_FLASH_CONFIG_PATH "{root}/config"',
        '#define TC_HEARTBEAT_PUBLIC_KEY_BYTES ' + ','.join(str(v) for v in public),
    ]))
    (root / 'config').write_text("TC_DEPLOY_RELEASE_TAG='test-release'\n")
    binary = compile_native('telemetry', root / 'telemetry', flags=['-include', str(config)])
    yield root, binary, state
    server.shutdown(); server.server_close(); thread.join(timeout=5)

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
        result = subprocess.run([str(binary), '--once', 'manual'], env={**env, **changes}, capture_output=True, text=True, timeout=10)
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
    process = subprocess.Popen([str(binary), '--once', 'manual'], env={**env, 'TC_TEST_FINISH': str(finish)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        process.terminate()
        assert process.poll() is None
        blocked = subprocess.run([str(binary), '--once'], env=env, capture_output=True, timeout=5)
        assert blocked.returncode == 75
        finish.touch()
        assert process.wait(timeout=5) == 0
        assert len(state['calls']) == 3
    finally:
        finish.touch()
        process.communicate(timeout=5)


def test_print_payload_has_no_network(cycle):
    _, state, _, binary, env = cycle
    result = subprocess.run([str(binary), '--print-payload', 'manual'], env=env, capture_output=True, text=True, timeout=5)
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
    result = subprocess.run([str(binary), *args], env=env, capture_output=True, timeout=5)
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
    process = subprocess.Popen([str(binary), '--daemon'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        (root / 'config').write_text('TELEMETRY=false\n')
        assert process.wait(timeout=5) == 0
        assert len(state['calls']) == 1
    finally:
        if process.poll() is None: process.terminate()
        process.communicate(timeout=5)


def test_opt_out_keeps_local_cleanup_available(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    (root / 'config').write_text('TELEMETRY=false\n')
    (root / 'work/debug').write_text('stale')
    (root / 'work/debug.sig').write_text('stale')
    result = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']
    assert state['calls'] == []


def wait_until(predicate, seconds=5):
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
        result = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
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
    result = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
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
            result = subprocess.run([str(binary), *args], env=env, capture_output=True, text=True, timeout=5)
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
        result = subprocess.run([str(binary), '--once'], env={**env, 'TC_TEST_LEAVE_SIG_DIR': '1'},
                                capture_output=True, text=True, timeout=10)
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
    process = subprocess.Popen([str(binary), '--once'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert state['signature_started'].wait(timeout=5)
        assert (root / 'work/debug').exists()
        assert (root / 'work/debug').stat().st_mode & 0o111 == 0
        if kill: process.kill()
        else: process.terminate()
        assert process.wait(timeout=5) != 0
        if kill:
            assert (root / 'work/debug').exists()
            cleanup = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
            assert cleanup.returncode == 0, cleanup.stderr
        assert not marker.exists()
        assert not (root / 'work/debug').exists()
        assert not (root / 'work/debug.sig').exists()
    finally:
        state['hold'].set()
        if process.poll() is None: process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize('detach', [False, True])
def test_inherited_owner_survives_parent_death_and_cleanup_waits(cycle, rig, tmp_path, detach):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'true'
    finish = tmp_path / 'finish'
    child_env = {**env, 'TC_TEST_FINISH': str(finish)}
    if detach: child_env['TC_TEST_DETACH'] = '1'
    process = subprocess.Popen([str(binary), '--once'], env=child_env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        wait_until(lambda: marker.exists() and marker.read_text().startswith('executed\n'))
        if detach:
            assert process.wait(timeout=5) == 0
        else:
            process.kill(); process.wait(timeout=5)
        assert (root / 'work/debug').exists()
        assert not (root / 'work/debug.sig').exists()
        for args in (['--cleanup'], ['--once']):
            blocked = subprocess.run([str(binary), *args], env=env, capture_output=True, timeout=5)
            assert blocked.returncode == 75
        assert len(state['calls']) == 3
        finish.touch()
        def recovered():
            return subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5).returncode == 0
        wait_until(recovered)
        assert sorted(p.name for p in (root / 'work').iterdir()) == ['keep.txt']
    finally:
        finish.touch()
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.communicate(timeout=5)


def test_debug_crash_is_reaped_and_cleaned(cycle):
    run, _, marker, *_ = cycle
    assert run('true', TC_TEST_CRASH='1').returncode == 1
    assert marker.read_text().startswith('executed\n')


def test_daemon_housekeeping_cleans_between_heartbeats(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'false'
    process = subprocess.Popen([str(binary), '--daemon'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        process.terminate(); process.communicate(timeout=5)


def test_shared_ram_root_requires_sticky_permissions(cycle, rig):
    _, state, _, binary, env = cycle
    root, _, _ = rig
    work = root / 'work'
    original_mode = work.stat().st_mode & 0o7777
    (work / 'debug').write_text('stale')
    try:
        work.chmod(0o777)
        result = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
        assert result.returncode == 1 and (work / 'debug').exists()
        work.chmod(0o1777)
        result = subprocess.run([str(binary), '--cleanup'], env=env, capture_output=True, timeout=5)
        assert result.returncode == 0 and not (work / 'debug').exists()
        assert state['calls'] == []
    finally:
        work.chmod(original_mode)
        (work / 'debug').unlink(missing_ok=True)
