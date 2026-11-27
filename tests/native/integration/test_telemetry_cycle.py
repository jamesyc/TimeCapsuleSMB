from __future__ import annotations
import json
from hashlib import sha512
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import signal
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from tests.loopback import accept_loop, held_refusing_ports
from tests.native.build import ROOT, compile_program, compile_service, header_define
from tests.processes import kill_acp_fixture_groups, kill_group

# Hang guards only. Under ASan on shared hosted macOS runners, process startup
# alone can take seconds; tests with real deadlines assert elapsed time
# themselves.
HANG_TIMEOUT = 20
# The rig's first spacing between extra resource heartbeats, in seconds.
HEALTH_SPACING = 3


def telemetry_command(binary, *args):
    return [str(binary), 'telemetry', *args]


def first_exec(binary, *args):
    # macOS checks each newly written binary on its first exec, one binary at
    # a time host-wide: ~0.2 s alone, seconds when parallel workers queue up.
    # Pay it here, not inside a deadline a test measures.
    subprocess.run([str(binary), *args], capture_output=True, env={}, timeout=HANG_TIMEOUT)

@pytest.fixture(scope='module')
def probe_ports():
    # Probe ports nothing listens on, held for the module so a connect is
    # refused and no other worker takes them (tests/loopback.py).
    with held_refusing_ports(2) as ports:
        yield ports


@pytest.fixture(scope='module')
def rig(tmp_path_factory, probe_ports):
    root = tmp_path_factory.mktemp('telemetry-integration')
    key = ECC.construct(curve='Ed25519', seed=bytes(range(32)))
    signer = eddsa.new(key, 'rfc8032')
    fixture = compile_program(root / 'debug', Path(__file__).with_name('debug_fixture.c'))
    # Its own file: test_acp_exec_failure_aborts_without_posting takes away its
    # execute permission.
    acp = compile_program(root / 'acp', Path(__file__).with_name('acp_fixture.c'), shared=False)
    first_exec(acp, '-q', 'syAP')
    state = {'mode': 'false', 'calls': [], 'payloads': [], 'raw': [], 'hold': threading.Event(), 'signature_started': threading.Event()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def answer(self, body, status=200):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_POST(self):
            # Read the mode before the call is recorded: a test that changes it
            # once it sees this call changes only the next one.
            mode = state['mode']
            raw = self.rfile.read(int(self.headers['Content-Length']))
            payload = json.loads(raw)
            state['payloads'].append(payload)
            state['raw'].append(raw)
            # Last, so a test that sees this call also sees its payload.
            state['calls'].append(('POST', self.path))
            # A test's step to take while the device waits for this answer.
            if state.get('before_answer'): state['before_answer']()
            if mode == 'http_error': return self.answer(b'{"DEBUG":true}', 500)
            if mode == 'http_error_once':
                # The server's own switch: a daemon retrying within a second
                # must not race the test thread.
                state['mode'] = 'false'
                return self.answer(b'{"DEBUG":true}', 500)
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
    closed_ports = probe_ports
    config = root / 'test_config.h'
    config.write_text('\n'.join([
        f'#define HEARTBEAT_ENDPOINT "{base}/v1/router-heartbeats"',
        f'#define TC_DEBUG_BASE_URL "{base}/downloads/bin/debug"',
        '#define TC_DEBUG_QUERY ""',
        '#define TC_CLEANUP_INTERVAL_SECONDS 1',
        '#define TC_HEARTBEAT_RETRY_SECONDS 1',
        '#define TC_HEALTH_SAMPLE_SECONDS 1',
        f'#define TC_EXTRA_SPACING_SECONDS {HEALTH_SPACING}',
        # The host's monotonic clock counts from its own boot, days ago: arm
        # only on the boot heartbeat, as a device does in its first minutes.
        '#define TC_ARM_AFTER_SECONDS 2000000000L',
        # Daemons send their boot heartbeat at once unless a test holds it.
        '#ifndef TC_BOOT_HOLD_SECONDS', '#define TC_BOOT_HOLD_SECONDS 0', '#endif',
        # The loopback probes must not depend on the host's own sshd or SMB
        # server: ports nothing listens on.
        '#ifndef TC_HEALTH_SSH_PORT', f'#define TC_HEALTH_SSH_PORT {closed_ports[0]}', '#endif',
        '#ifndef TC_HEALTH_SMB_PORT', f'#define TC_HEALTH_SMB_PORT {closed_ports[1]}', '#endif',
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
    state['calls'].clear(); state['payloads'].clear(); state['raw'].clear(); state['mode'] = 'false'
    state['hold'].clear(); state['signature_started'].clear()
    env = {**os.environ, 'TC_TEST_MARKER': str(tmp_path / 'marker')}
    # Prevent user curl/proxy configuration from affecting the local test server.
    for name in list(env):
        if name.lower().endswith('_proxy'): env.pop(name)
    def run(mode, **changes):
        state['mode'] = mode
        result = subprocess.run(telemetry_command(binary, '--once', 'manual'), env={**env, **changes}, capture_output=True, text=True, timeout=HANG_TIMEOUT)
        outcome = f'exit {result.returncode}: {result.stderr}'
        if mode == 'bad_executable' and sys.platform == 'darwin' and (root / 'work/debug').exists():
            # macOS checks a new file at exec. When that exec fails, the child's
            # descriptors, and so its workspace lock, can outlive the reap by a
            # few milliseconds; the cycle then sees a busy workspace and leaves
            # the files, as it would for a surviving worker. The device kernel
            # has no such check. The next cleanup must still remove them.
            def cleaned():
                return subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True,
                                      timeout=HANG_TIMEOUT).returncode == 0
            wait_until(cleaned, seconds=5)
        assert not (root / 'work/debug').exists(), outcome
        assert not (root / 'work/debug.sig').exists(), outcome
        assert (root / 'work/keep.txt').read_text() == 'unrelated runtime file'
        return result
    return run, state, tmp_path / 'marker', binary, env

@pytest.mark.parametrize('mode', ['false', 'legacy'])
def test_no_debug_means_post_only(cycle, mode):
    run, state, marker, *_ = cycle
    result = run(mode)
    assert result.returncode == 0, result.stderr
    assert state['calls'] == [('POST', '/v1/router-heartbeats')], result.stderr
    assert not marker.exists()
    payload = state['payloads'][0]
    assert payload['target_lane'] == '6' and len(payload['debug_nonce']) == 32
    assert payload['deploy_release_tag'] == 'test-release'
    assert payload['reason'] == 'manual' and payload['router_id'] == 'C86TEST0SN01'
    assert payload['mac'] == '02:00:00:00:00:01'

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
        wait_until(debug_started(marker))
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


@pytest.mark.parametrize('mode,expected', [('false', 0), ('true', 0), ('unsigned', 0),
                                         ('malformed', 0), ('http_error', 1), ('oversized', 1)])
def test_report_posts_without_executing_debug(cycle, mode, expected):
    _, state, marker, binary, env = cycle
    state['mode'] = mode
    result = subprocess.run(telemetry_command(binary, '--report', 'bufstall:resolved:5'),
                            env=env, capture_output=True, timeout=HANG_TIMEOUT)
    assert result.returncode == expected, result.stderr
    assert state['calls'] == [('POST', '/v1/router-heartbeats')]
    assert state['payloads'][0]['reason'] == 'bufstall:resolved:5'
    assert not marker.exists()


def test_a_reports_token_gives_every_attempt_one_heartbeat_id(cycle):
    # The manager starts a new --report process for each try of one report
    # and passes the same token: the tries share an id (the server keeps one
    # row), even when a buffer-stall retry's reason has grown with merged
    # episodes; a new token is a new heartbeat, and with no token each
    # process makes its own.
    _, state, _, binary, env = cycle
    state['mode'] = 'http_error'
    def report(*args):
        result = subprocess.run(telemetry_command(binary, '--report', *args), env=env, capture_output=True,
                                timeout=HANG_TIMEOUT)
        assert result.returncode == 1, result.stderr
        return state['payloads'][-1]['heartbeat_id']
    first = report('bufstall:resolved:3', '0123456789abcdef')
    assert report('bufstall:resolved:3', '0123456789abcdef') == first
    assert report('bufstall:capped:41', '0123456789abcdef') == first
    assert report('bufstall:resolved:3', 'fedcba9876543210') != first
    assert report('stuck:smbd:biowait:400') != report('stuck:smbd:biowait:400')


def test_report_and_its_cancellation_do_not_touch_a_running_signed_job(cycle, rig, tmp_path):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    state['mode'] = 'true'
    finish = tmp_path / 'finish'
    job = subprocess.Popen(telemetry_command(binary, '--once'),
                           env={**env, 'TC_TEST_FINISH': str(finish)},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    report = None
    try:
        wait_until(debug_started(marker))
        before = marker.read_bytes()
        debug = (root / 'work/debug').read_bytes()
        # Even cleanup must not run while another process owns these files.
        signature = root / 'work/debug.sig'
        signature.write_bytes(b'owned by the running job')
        result = subprocess.run(telemetry_command(binary, '--report'), env=env,
                                capture_output=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 0, result.stderr
        assert state['calls'] == [('POST', '/v1/router-heartbeats'), ('GET', '/downloads/bin/debug6'),
                                  ('GET', '/downloads/bin/debug6.sig'), ('POST', '/v1/router-heartbeats')]
        calls = tmp_path / 'report-acp-calls'
        report = subprocess.Popen(telemetry_command(binary, '--report'), env={**env,
            'TC_TEST_ACP_MODE': 'ignore_term', 'TC_TEST_ACP_CALLS': str(calls)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait_until(lambda: calls.exists() and calls.stat().st_size > 0)
        report.terminate()
        report.communicate(timeout=HANG_TIMEOUT)
        assert report.returncode != 0
        assert_collectors_stopped(calls)
        assert job.poll() is None and marker.read_bytes() == before
        assert (root / 'work/debug').read_bytes() == debug
        assert signature.read_bytes() == b'owned by the running job'
        assert len(state['calls']) == 4
    finally:
        if report and report.poll() is None:
            report.terminate()
            report.communicate(timeout=HANG_TIMEOUT)
        finish.touch()
        job.communicate(timeout=HANG_TIMEOUT)


def test_print_payload_has_no_network(cycle):
    _, state, _, binary, env = cycle
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'manual'), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert json.loads(result.stdout)['reason'] == 'manual'
    assert state['calls'] == []


@pytest.mark.parametrize('args,expected', [(['--once'], 0), (['--daemon'], 0), (['--report'], 1)])
@pytest.mark.parametrize('setting', ['TELEMETRY=false\n', "TELEMETRY='false'\n", 'TELEMETRY="false"\n', '  TELEMETRY = false  \r\n'])
def test_device_opt_out_exits_without_network_or_debug_files(cycle, rig, args, expected, setting):
    _, state, marker, binary, env = cycle
    root, _, _ = rig
    (root / 'config').write_text(setting)
    state['mode'] = 'true'
    result = subprocess.run(telemetry_command(binary, *args), env=env, capture_output=True, timeout=HANG_TIMEOUT)
    assert result.returncode == expected, result.stderr
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


@pytest.fixture(scope='module')
def held_collector(rig):
    # A boot hold that outlasts any test.
    return collector_with(rig[0], 'service-held', '-DTC_BOOT_HOLD_SECONDS=1000000')


def test_a_held_boot_heartbeat_blocks_nothing_else(cycle, rig, held_collector, tmp_path):
    # The hold is the daemon's own: it reads no acp and posts nothing, while
    # the same binary's --report (the manager's) delivers at once, and the
    # daemon still leaves promptly on opt-out or SIGTERM.
    _, state, _, binary, env = cycle
    root, _, _ = rig
    held_calls, report_calls = tmp_path / 'held-acp-calls', tmp_path / 'report-acp-calls'
    held = {**env, 'TC_TEST_ACP_CALLS': str(held_calls)}
    stale = root / 'work/debug'

    def in_loop():
        # Housekeeping runs through the hold; once it has removed a stale
        # download the daemon is in its loop, signal handlers set.
        return not stale.exists() and process.poll() is None

    stale.write_bytes(b'stale')
    process = subprocess.Popen(telemetry_command(held_collector, '--daemon'), env=held, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        wait_until(in_loop)
        result = subprocess.run(telemetry_command(held_collector, '--report', 'bufstall:resolved:5'),
                                env={**env, 'TC_TEST_ACP_CALLS': str(report_calls)}, capture_output=True,
                                timeout=HANG_TIMEOUT)
        assert result.returncode == 0, result.stderr
        assert [p['reason'] for p in state['payloads']] == ['bufstall:resolved:5'] and recorded_collectors(report_calls)
        assert process.poll() is None and not recorded_collectors(held_calls)
        (root / 'config').write_text('TELEMETRY=false\n')
        assert process.wait(timeout=HANG_TIMEOUT) == 0
    finally:
        if process.poll() is None: process.terminate()
        process.communicate(timeout=HANG_TIMEOUT)
    (root / 'config').write_text("TC_DEPLOY_RELEASE_TAG='test-release'\n")
    stale.write_bytes(b'stale')
    process = subprocess.Popen(telemetry_command(held_collector, '--daemon'), env=held, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        wait_until(in_loop)
        process.terminate()
        # The loop sleeps a second at a time; SIGTERM ends the sleep.
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None: process.kill()
        process.communicate(timeout=HANG_TIMEOUT)
    assert len(state['calls']) == 1 and not recorded_collectors(held_calls)


@pytest.fixture(scope='module')
def probe_listener(rig):
    # An sshd stand-in on a held port (refused on ::1, tests/loopback.py):
    # it records each connection's arrival and answers with a banner. Two
    # collectors aim their SSH probe at it: one whose boot hold outlasts any
    # test, one held a second.
    arrivals = []

    def banner(conn):
        arrivals.append(time.monotonic())
        with conn:
            try:
                conn.sendall(b'SSH-2.0-OpenSSH_test\r\n')
                conn.settimeout(HANG_TIMEOUT)
                while conn.recv(4096):
                    pass
            except OSError:
                pass
    with held_refusing_ports(1) as [port]:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(8)
        stop = accept_loop(listener, banner)
        define = f'-DTC_HEALTH_SSH_PORT={port}'
        try:
            held = collector_with(rig[0], 'service-held-probe', '-DTC_BOOT_HOLD_SECONDS=1000000', define)
            short = collector_with(rig[0], 'service-hold1-probe', '-DTC_BOOT_HOLD_SECONDS=1', define)
            yield held, short, arrivals
        finally:
            stop()


def test_the_first_probe_round_waits_for_the_boot_hold(cycle, rig, probe_listener, tmp_path):
    # At daemon start smbd is often not listening yet, so the first round
    # waits out the hold the boot heartbeat waits. The loop probes before
    # it cleans up, so once housekeeping has run the daemon would already
    # have probed had it probed at start.
    _, state, _, _, env = cycle
    root, _, _ = rig
    held, short, arrivals = probe_listener
    env, table = health_fixtures(tmp_path, env)
    table(40)  # a roomy table, so a round may run
    stale = root / 'work/debug'
    stale.write_bytes(b'stale')
    arrivals.clear()
    process = subprocess.Popen(telemetry_command(held, '--daemon'), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        wait_until(lambda: not stale.exists() or process.poll() is not None)
        assert process.poll() is None and arrivals == [] and state['payloads'] == []
    finally:
        process.terminate()
        process.communicate(timeout=HANG_TIMEOUT)
    # Held a second: the round runs when the hold ends, just before the
    # boot heartbeat, which carries its results.
    started = time.monotonic()
    process = subprocess.Popen(telemetry_command(short, '--daemon'), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        wait_until(lambda: state['payloads'] or process.poll() is not None)
        assert process.poll() is None
        [boot] = state['payloads']
        assert boot['reason'] == 'boot' and len(arrivals) == 1 and arrivals[0] >= started
        probes = boot['health']['probes']
        assert probes['ssh4']['last'] == 'ok' and probes['smb4']['last'] == 'absent'
    finally:
        process.terminate()
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


def debug_started(marker):
    # The debug fixture creates its marker, then writes "executed\n<pid>\n";
    # the file exists before that record is in it.
    return lambda: marker.exists() and re.fullmatch(r'executed\n\d+\n', marker.read_text()) is not None


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
        wait_until(debug_started(marker))
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
        kill_group(process.pid)
        process.communicate(timeout=HANG_TIMEOUT)


def test_debug_crash_is_reaped_and_cleaned(cycle):
    run, _, marker, *_ = cycle
    assert run('true', TC_TEST_CRASH='1').returncode == 1
    assert marker.read_text().startswith('executed\n')


def serial_reads(calls):
    # One per collection: sySN is its first key.
    return [line for line in recorded_collectors(calls) if line.split()[0] == 'sySN' and line.split()[2] == 'parent']


def test_daemon_retries_an_undelivered_boot_heartbeat(cycle, acp_calls):
    # Right after boot DNS is often not ready. An undelivered heartbeat must
    # retry soon, still labelled boot, and stop once one is delivered. acp
    # answered every key, so the retry sends the same facts without forking
    # acp again.
    _, state, _, binary, env = cycle
    state['mode'] = 'http_error_once'
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env={**env, 'TC_TEST_ACP_CALLS': str(acp_calls)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 2)
        assert [p['reason'] for p in state['payloads']] == ['boot', 'boot']
        assert len(serial_reads(acp_calls)) == 1
        assert state['payloads'][0]['router_id'] == state['payloads'][1]['router_id'] == 'C86TEST0SN01'
        # One heartbeat, one id: the server keeps the latest attempt, which
        # may replace a first one it stored without the answer getting back.
        assert state['payloads'][0]['heartbeat_id'] == state['payloads'][1]['heartbeat_id']
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_daemon_retry_reads_acp_again_after_an_incomplete_collection(cycle, acp_calls):
    # The boot collection loses waMA (output too long for acp to accept) and
    # the POST fails: the retry collects again, and carries the address.
    _, state, _, binary, env = cycle
    state['mode'] = 'http_error_once'
    process = subprocess.Popen(telemetry_command(binary, '--daemon'),
                               env={**env, 'TC_TEST_ACP_CALLS': str(acp_calls), 'TC_TEST_ACP_KEY': 'waMA',
                                    'TC_TEST_ACP_MODE': 'countdown', 'TC_TEST_ACP_VALUE': 'x' * 4096,
                                    'TC_TEST_ACP_UNTIL': '1', 'TC_TEST_ACP_LATER': '80:EA:96:E6:58:68'},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 2)
        assert [p['reason'] for p in state['payloads']] == ['boot', 'boot']
        assert len(serial_reads(acp_calls)) == 2 and len(wama_reads(acp_calls)) == 2
        assert state['payloads'][1]['mac'] == REAL_MAC
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def health_fixtures(tmp_path, env):
    procs = tmp_path / 'procs'
    procs.mkdir()
    counters = tmp_path / 'counters'
    counters.write_text('maxproc 90\n')

    def table(rows):
        text = ''.join(f'{1000 + i} 1 {1000 + i} 3 0x80 0 0 nanoslp sleep sleep 150\n' for i in range(rows))
        (procs / '.table.new').write_text(text)
        (procs / '.table.new').rename(procs / 'table')
    return {**env, 'TC_TEST_PROCS': str(procs), 'TC_TEST_HEALTH': str(counters)}, table


def test_daemon_sends_one_extra_heartbeat_when_the_process_table_nears_full(cycle, tmp_path):
    # The outage usually ends with an unplug that loses the daemon's peaks,
    # so the daemon reports the first crossing while it can, and only once
    # while the table stays high.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    table(40)
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        assert state['payloads'][0]['reason'] == 'boot'
        assert state['payloads'][0]['health']['procs']['n'] == 40
        assert state['payloads'][0]['health']['triggers'] == []
        table(85)
        wait_until(lambda: len(state['calls']) == 2)
        extra = state['payloads'][1]
        assert extra['reason'] == 'resource_high'
        assert extra['heartbeat_id'] != state['payloads'][0]['heartbeat_id']
        assert extra['health']['triggers'] == ['procs']
        assert extra['health']['procs_first_high']['n'] == 85
        assert extra['health']['maxproc'] == 90
        assert extra['router_id'] == state['payloads'][0]['router_id']
        assert extra['delivery'] == {'failed': 0, 'since': None, 'classes': {
            'local_resource': 0, 'acp_unavailable': 0, 'network': 0, 'server': 0}}
        # Still high, then the file table crosses 90 %: the next heartbeat is
        # for files alone. A second procs crossing, whenever it came, would
        # be a heartbeat of its own or ride in this one's triggers or news,
        # so load cannot make this pass wrongly.
        (tmp_path / 'counters').write_text('maxproc 90\nmaxfiles 1000\nfiles 950\n')
        wait_until(lambda: len(state['calls']) == 3)
        files = state['payloads'][2]
        assert files['reason'] == 'resource_high' and files['health']['triggers'] == ['files']
        assert 'procs' not in files['health']['news']
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_a_failed_extra_heartbeat_retries_with_its_triggers_and_id(cycle, tmp_path):
    # The server answered 500: nothing was stored, so the retry must carry
    # the same events (they are cleared only once one is delivered), under
    # the same id, with the failure on record.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    table(40)
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        state['mode'] = 'http_error_once'
        table(85)
        wait_until(lambda: len(state['calls']) == 3)
        first, retry = state['payloads'][1:3]
        assert first['reason'] == retry['reason'] == 'resource_high'
        assert first['health']['triggers'] == retry['health']['triggers'] == ['procs']
        assert first['heartbeat_id'] == retry['heartbeat_id'] != state['payloads'][0]['heartbeat_id']
        assert retry['delivery']['failed'] == 1 and retry['delivery']['classes']['server'] == 1
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_a_held_workspace_keeps_a_failed_heartbeats_id_events_and_record(cycle, rig, tmp_path):
    # An attempt that finds the workspace lock held (busy) builds no payload
    # and is not counted: the attempt after the lock is free is the same
    # heartbeat as the failed one before it, with its id, events and record.
    import fcntl
    _, state, _, binary, env = cycle
    root, _, _ = rig
    env, table = health_fixtures(tmp_path, env)
    table(40)
    held, fd = threading.Event(), os.open(root / 'work', os.O_RDONLY)

    def hold_once_free():
        # Blocks until the daemon's cycle lets go, then holds the lock, so
        # every retry after the failed attempt finds it busy.
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.set()

    def start_holding():
        state['before_answer'] = None
        threading.Thread(target=hold_once_free, daemon=True).start()
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        # The server starts the holder while it handles the failing POST,
        # with the daemon's lock still held: no race with the retry.
        state['before_answer'] = start_holding
        state['mode'] = 'http_error_once'
        table(85)
        wait_until(held.is_set)
        # Retries come a second apart in this build (TC_HEARTBEAT_RETRY_SECONDS
        # and TC_CLEANUP_INTERVAL_SECONDS are 1): five seconds of busy ones.
        time.sleep(5)
        assert len(state['calls']) == 2
        os.close(fd)
        fd = -1
        wait_until(lambda: len(state['calls']) == 3)
        first, retry = state['payloads'][1:3]
        assert first['reason'] == retry['reason'] == 'resource_high'
        assert first['heartbeat_id'] == retry['heartbeat_id'] != state['payloads'][0]['heartbeat_id']
        assert first['health']['triggers'] == retry['health']['triggers'] == ['procs']
        assert retry['delivery']['failed'] == 1 and retry['delivery']['classes']['server'] == 1
    finally:
        if fd >= 0: os.close(fd)
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_a_full_table_in_the_kernel_messages_alone_asks_for_a_heartbeat(cycle, tmp_path):
    # Every sample finds the process table roomy, but the kernel logged that
    # it filled between two samples (a burst shorter than one): that line
    # alone asks for an extra heartbeat. Lines already in the buffer at the
    # daemon's first read only mark at_start.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    kmsg = tmp_path / 'kmsg'

    def write_kmsg(text):
        (tmp_path / '.kmsg.new').write_text(text)
        (tmp_path / '.kmsg.new').rename(kmsg)
    old = 'boot\nfile: table is full\n' + 'wl0: deauth reason 2\n' * 50
    write_kmsg(old)
    env = {**env, 'TC_TEST_KMSG': str(kmsg)}
    table(40)
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        boot = state['payloads'][0]['health']
        assert state['payloads'][0]['reason'] == 'boot' and boot['triggers'] == [] and boot['news'] == []
        assert boot['kmsg']['file_full'] == {'first': None, 'last': None, 'n': 0, 'at_start': True}
        assert boot['kmsg']['proc_full'] is None
        write_kmsg(old + 'proc: table is full\n' + 'wl0: deauth reason 2\n' * 3)
        wait_until(lambda: len(state['calls']) == 2)
        extra = state['payloads'][1]
        assert extra['reason'] == 'resource_high' and extra['health']['triggers'] == ['kmsg']
        proc_full = extra['health']['kmsg']['proc_full']
        assert proc_full['n'] == 1 and not proc_full['at_start'] and proc_full['first'] == proc_full['last']
        assert extra['health']['kmsg']['file_full']['n'] == 0
        assert extra['health']['procs']['n'] == 40 and extra['health']['procs_high_events'] == 0
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_extra_heartbeat_is_delivered_with_no_fork_once_the_table_is_full(cycle, tmp_path):
    # The case issue 429 needs: after the boot heartbeat, fork() fails for
    # everything (as with kern.maxproc reached). The extra heartbeat still
    # goes out over the native HTTP client, with the serial and network plan
    # this process read at boot, and it does not even try to fork an acp
    # child.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    calls = tmp_path / 'acp-calls'
    fork_fail = tmp_path / 'fork-fail'
    env = {**env, 'TC_TEST_ACP_CALLS': str(calls), 'TC_TEST_FORK_FAIL': str(fork_fail)}
    table(40)
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        boot = state['payloads'][0]
        assert boot['router_id'] == 'C86TEST0SN01' and 'plan_age_sec' not in boot
        reads_at_boot = calls.read_text()
        fork_fail.touch()
        table(85)
        wait_until(lambda: len(state['calls']) == 2)
        extra = state['payloads'][1]
        assert extra['reason'] == 'resource_high'
        assert extra['heartbeat_id'] != state['payloads'][0]['heartbeat_id']
        assert extra['router_id'] == 'C86TEST0SN01' and extra['device_model'] == 'TimeCapsule8,119'
        assert extra['plan_error'] == boot['plan_error'] and extra['plan_age_sec'] >= 0
        assert extra['health']['fork_failures'] == {'eagain': 0, 'enomem': 0, 'other': 0}
        assert 'fork' not in extra['health']['news'] and 'procs' in extra['health']['news']
        assert calls.read_text() == reads_at_boot
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores RLIMIT_NPROC")
def test_payload_survives_a_kernel_that_refuses_every_fork(cycle):
    # RLIMIT_NPROC 0 makes the real kernel refuse every fork() with EAGAIN,
    # as kern.maxproc does on a device. No acp reading works, so the router
    # is named by its hardware address, and the failures are counted.
    import resource
    _, state, _, binary, env = cycle
    # A sanitizer build's leak check forks at exit, which this limit refuses.
    env = {**env, 'ASAN_OPTIONS': 'detect_leaks=0'}
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'probe'), env=env, capture_output=True,
                            text=True, timeout=HANG_TIMEOUT,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NPROC, (0, 0)))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['router_id'] == 'mac-02:00:00:00:00:01'
    assert payload['device_model'] == ''
    # One acp collection, which stops at its first failed fork.
    assert payload['health']['fork_failures']['eagain'] == 1
    assert 'fork' in payload['health']['news'] and payload['health']['triggers'] == []
    assert state['calls'] == []


def test_no_probe_round_within_two_of_the_process_limit(cycle, tmp_path):
    # At 88 of 90 the probe's own sshd or smbd child could take a slot a
    # client needs: the round is skipped and counted. At 85 it still runs.
    # Without a table it is skipped too.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    table(88)
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'probe'), env=env, capture_output=True,
                            text=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    health = json.loads(result.stdout)['health']
    assert health['probe_rounds_skipped'] == 1
    assert all(probe['last'] == 'none' for probe in health['probes'].values())
    table(85)
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'probe'), env=env, capture_output=True,
                            text=True, timeout=HANG_TIMEOUT)
    health = json.loads(result.stdout)['health']
    assert health['probe_rounds_skipped'] == 0 and health['probes']['ssh4']['last'] == 'absent'
    # A table that could not be read says nothing about room: no round.
    unread = {k: v for k, v in env.items() if k != 'TC_TEST_PROCS'}
    result = subprocess.run(telemetry_command(binary, '--print-payload', 'probe'), env=unread, capture_output=True,
                            text=True, timeout=HANG_TIMEOUT)
    health = json.loads(result.stdout)['health']
    assert health['procs'] is None and health['probe_rounds_skipped'] == 1
    assert all(probe['last'] == 'none' for probe in health['probes'].values())


def test_a_table_high_from_boot_is_reported_once_the_boot_heartbeat_is_in(cycle, tmp_path):
    # Boot runs many short-lived processes against the stock kern.maxproc, so
    # nothing counts before the boot heartbeat is delivered: with the server
    # failing it keeps retrying as "boot", and no extra heartbeat jumps ahead.
    # Once it is in, a table still high for two samples is reported.
    _, state, _, binary, env = cycle
    env, table = health_fixtures(tmp_path, env)
    table(85)
    state['mode'] = 'http_error'
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: len(state['calls']) == 2)
        state['mode'] = 'false'
        wait_until(lambda: any(p['reason'] == 'resource_high' for p in state['payloads']))
        reasons = [p['reason'] for p in state['payloads']]
        first_extra = reasons.index('resource_high')
        assert set(reasons[:first_extra]) == {'boot'}
        boot = state['payloads'][first_extra - 1]
        assert boot['health']['triggers'] == [] and boot['health']['procs']['n'] == 85
        # The delivered boot heartbeat says how many tries failed before it.
        assert boot['delivery']['failed'] >= 2 and boot['delivery']['classes']['server'] >= 2
        extra = state['payloads'][first_extra]
        assert extra['health']['triggers'] == ['procs'] and 'procs' in extra['health']['news']
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


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


def collector_with(root, name, *defines):
    binary = compile_service(root / name,
                             flags=['-include', str(root / 'test_config.h'), *defines, '-I', str(ROOT / 'build/native')],
                             exclude=['iflist.c'], extra_sources=[ROOT / 'tests/native/integration/iflist_fixture.c'])
    first_exec(binary)
    return binary


def collector_with_acp_deadline(root, name, seconds):
    return collector_with(root, name, f'-DTC_ACP_TIMEOUT_SECONDS={seconds}')


@pytest.fixture(scope='module')
def short_collector(rig):
    # Only deadline tests need a shortened clock. Ordinary HTTP/JSON tests
    # use the production allowance so fixture startup isn't the assertion.
    return collector_with_acp_deadline(rig[0], 'service-short-timeout', 1)


@pytest.fixture(scope='module')
def paced_collector(rig):
    # A reply that must arrive inside the deadline gets 1.5 s of it to spare
    # for the fake acp's start on a loaded runner.
    return collector_with_acp_deadline(rig[0], 'service-paced-timeout', 2)


@pytest.fixture
def acp_calls(tmp_path):
    calls = tmp_path / 'acp-calls'
    yield calls
    # A failed assertion or outer subprocess timeout must not strand a fixture
    # that deliberately ignores TERM or leaves a descendant holding stdout.
    kill_acp_fixture_groups(calls)


def recorded_collectors(calls):
    # The fake ACP records itself before it can fork anything. On a loaded host
    # the short deadline can kill a child before it execs and records; that
    # child ran no fixture code, so nothing of it can be left running.
    return calls.read_text().splitlines() if calls.exists() else []


def assert_collectors_stopped(calls):
    for line in recorded_collectors(calls):
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
def test_acp_failure_reaps_children_releases_lock_and_still_reports(cycle, short_collector, acp_calls, mode):
    # A broken or hung syAP read is stopped and reaped, and the heartbeat
    # still goes under the serial with the model number empty: the router ID
    # does not depend on it.
    _, state, marker, binary, env = cycle
    calls = acp_calls
    state['mode'] = 'false'
    started = time.monotonic()
    result = subprocess.run(telemetry_command(short_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': mode,
                            'TC_TEST_ACP_CALLS': str(calls)}, capture_output=True, text=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 15 # Below the 30 s collection budget, with scheduling margin.
    assert len(state['calls']) == 1
    assert state['payloads'][0]['router_id'] == 'C86TEST0SN01' and state['payloads'][0]['device_syap'] == ''
    assert 'acp: syAP' in result.stderr
    if mode in ('drip', 'drip_after_line'): assert 'timed out' in result.stderr
    # Only the deadline may stop the child before the fixture records itself.
    if not recorded_collectors(calls): assert 'acp: syAP timed out' in result.stderr
    assert not marker.exists()
    assert_collectors_stopped(calls)
    assert subprocess.run(telemetry_command(binary, '--cleanup'), env=env, capture_output=True, timeout=HANG_TIMEOUT).returncode == 0
    # A fresh invocation can collect/post after the owner exits.
    assert subprocess.run(telemetry_command(binary, '--once'), env=env, capture_output=True, timeout=HANG_TIMEOUT).returncode == 0
    assert len(state['calls']) == 2 and state['payloads'][1]['device_syap'] == '119'


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
    # The oversized name is refused, never truncated into the payload; the
    # heartbeat still goes, with the name empty.
    run, state, marker, *_ = cycle
    result = run('false', TC_TEST_ACP_MODE='value', TC_TEST_ACP_KEY='syNm', TC_TEST_ACP_VALUE='x' * 256)
    assert result.returncode == 0 and 'oversized output' in result.stderr
    assert state['payloads'][0]['device_name'] == '' and not marker.exists()


def test_acp_exec_does_not_inherit_workspace_or_pipe_descriptors(cycle):
    run, state, *_ = cycle
    assert run('false', TC_TEST_ACP_MODE='inspect_fds').returncode == 0
    # A descriptor leak makes the fixture fail; missing optional ACP fields
    # must not disguise that failure as an otherwise successful heartbeat.
    assert state['payloads'][0]['device_syap'] == '119'


def test_acp_exec_failure_reports_under_the_hardware_address(cycle, rig):
    # A broken acp binary is exactly what a heartbeat should report: with no
    # serial, the hardware address names the router, and every acp field is
    # empty.
    _, state, _, binary, env = cycle
    root, _, _ = rig
    acp = root / 'acp'
    permissions = acp.stat().st_mode & 0o777
    try:
        acp.chmod(0o600)
        result = subprocess.run(telemetry_command(binary, '--once'), env=env, capture_output=True, text=True, timeout=HANG_TIMEOUT)
        assert result.returncode == 0 and 'exec' in result.stderr
        payload = state['payloads'][0]
        assert payload['router_id'] == 'mac-02:00:00:00:00:01'
        assert payload['device_model'] == payload['device_name'] == payload['device_syap'] == ''
    finally:
        acp.chmod(permissions)


# One collection per heartbeat: the identity keys, then the plan's.
COLLECTION = ['sySN', 'syAP', 'syAM', 'raNA', 'raDS', 'waNM', 'usbF', 'laIP', 'waIP', 'waLL', 'gnRo', 'syNm', 'waMA']


@pytest.mark.parametrize('key', ['sySN', 'syAP', 'syAM', 'raNA', 'syNm'])
def test_acp_timeout_stops_the_collection_and_posts_what_was_read(cycle, short_collector, acp_calls, key):
    # Nothing is asked after a timeout. A new process sends what it read:
    # the serial (or, if that hung, the hardware address), and empty fields
    # for the key that hung and every key after it.
    _, state, _, _, env = cycle
    calls = acp_calls
    result = subprocess.run(telemetry_command(short_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': 'hang',
                            'TC_TEST_ACP_KEY': key, 'TC_TEST_ACP_CALLS': str(calls)},
                            capture_output=True, text=True, timeout=HANG_TIMEOUT)
    assert result.returncode == 0 and f'acp: {key} timed out' in result.stderr
    payload = state['payloads'][0]
    read = COLLECTION[:COLLECTION.index(key)]
    assert payload['router_id'] == ('C86TEST0SN01' if 'sySN' in read else 'mac-02:00:00:00:00:01')
    assert payload['device_syap'] == ('119' if 'syAP' in read else '')
    assert payload['device_model'] == ('TimeCapsule8,119' if 'syAM' in read else '')
    assert payload['device_name'] == ('Test Capsule' if 'syNm' in read else '')
    assert 'plan_error' in payload
    # Collection stopped at key. The deadline may kill key's child before the
    # fixture records it, leaving the previous key as the last record.
    recorded = [line.split()[0] for line in calls.read_text().splitlines()]
    assert recorded in (COLLECTION[:COLLECTION.index(key) + 1], COLLECTION[:COLLECTION.index(key)])
    assert_collectors_stopped(calls)


@pytest.mark.parametrize('args', [['--daemon'], ['--once'], ['--report'], ['--print-payload']])
@pytest.mark.parametrize('mode,stop_signal', [('ignore_term', signal.SIGTERM), ('descendant', signal.SIGTERM),
                                             ('closed_hang', signal.SIGINT)])
def test_stop_during_acp_collection_is_prompt_even_with_long_timeout(cycle, production_collector, acp_calls, args, mode, stop_signal):
    _, state, _, _, env = cycle
    calls = acp_calls
    process = subprocess.Popen(telemetry_command(production_collector, *args), env={**env,
        'TC_TEST_ACP_MODE': mode, 'TC_TEST_ACP_CALLS': str(calls)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # The mode applies to syAP alone: wait for its child, not sySN's
        # before it, which answers and exits by itself, so a stop that
        # lands during it cancels the collection with nothing to abort.
        wait_until(lambda: calls.exists() and any(line.split()[:1] == ['syAP'] for line in calls.read_text().splitlines()))
        if mode == 'descendant': wait_until(lambda: ' descendant' in calls.read_text())
        started = time.monotonic()
        process.send_signal(stop_signal)
        out, err = process.communicate(timeout=HANG_TIMEOUT)
        assert process.returncode == 1 and b'cancelled' in err
        assert time.monotonic() - started < 15 # Below the production 20 s per-key timeout.
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
    assert payload['router_id'] == 'C86TEST0SN01'


def test_production_telemetry_uses_the_default_acp_deadline(cycle, production_collector):
    # The slow fixture answers after 1.5 s, beyond short_collector's 1 s
    # deadline, so a production build that took a shortened timeout fails
    # here. test_device.c pins the default at exactly 20 s on a moved clock;
    # the short_collector tests cover telemetry's handling of a timeout.
    _, state, _, _, env = cycle
    result = subprocess.run(telemetry_command(production_collector, '--once'), env={**env, 'TC_TEST_ACP_MODE': 'slow'},
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert len(state['calls']) == 1


def test_each_acp_command_gets_its_own_deadline(cycle, paced_collector, acp_calls):
    _, state, _, _, env = cycle
    # The fake paces its replies, sleeping less when a start is slow: the 13
    # replies span 5.2 s, past paced_collector's 2 s per-key deadline, while
    # each lasts only 0.4 s or its own startup. test_device.c pins the exact
    # per-key and shared deadlines.
    pace_ms = 400
    result = subprocess.run(telemetry_command(paced_collector, '--once', 'manual'),
                            env={**env, 'TC_TEST_ACP_MODE': 'slow_each', 'TC_TEST_ACP_KEY': '*',
                                 'TC_TEST_ACP_CALLS': str(acp_calls), 'TC_TEST_ACP_PACE_MS': str(pace_ms)},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    # One collection under one 30 s budget: the identity keys and the
    # device plan's, one child at a time, each with its own deadline, each
    # key once.
    calls = [line.split() for line in acp_calls.read_text().splitlines()]
    # The fourth call can start only after the three identity replies.
    assert float(calls[3][3]) - float(calls[0][3]) >= 3 * pace_ms / 1000
    assert [call[0] for call in calls] == COLLECTION
    assert len(state['calls']) == 1


def test_router_id_falls_back_to_the_hardware_address_without_a_hash(cycle):
    # A device whose ACP never answers sySN still reports, under its own
    # hardware address; with neither, nothing is sent at all.
    run, state, *_ = cycle
    assert run('false', TC_TEST_ACP_NO_SERIAL='1').returncode == 0
    payload = state['payloads'][0]
    assert payload['router_id'] == 'mac-02:00:00:00:00:01' and payload['mac'] == '02:00:00:00:00:01'
    result = run('false', TC_TEST_ACP_NO_SERIAL='1', TC_TEST_IFLIST='failed', TC_TEST_ACP_KEY='waMA',
                 TC_TEST_ACP_MODE='empty')
    assert result.returncode == 1 and 'no router serial or hardware address' in result.stderr
    assert len(state['calls']) == 1


PLACEHOLDER = '00:01:02:03:04:05'
REAL_MAC = '80:ea:96:e6:58:68'


def wama_reads(calls):
    return [line for line in recorded_collectors(calls) if line.split()[0] == 'waMA' and line.split()[2] == 'parent']


def test_mac_comes_from_wama_over_the_interface_table(cycle, tmp_path):
    # Apple's own answer names the device, whatever the lowest interface holds.
    run, state, *_ = cycle
    table = tmp_path / 'mac'
    table.write_text('e8:8d:28:58:f1:5c\n')
    assert run('false', TC_TEST_IFLIST='mac_file', TC_TEST_IFLIST_MAC=str(table), TC_TEST_ACP_KEY='waMA',
               TC_TEST_ACP_MODE='value', TC_TEST_ACP_VALUE='80:EA:96:E6:58:68').returncode == 0
    assert state['payloads'][0]['mac'] == REAL_MAC


def test_a_placeholder_is_never_sent_and_never_names_the_router(cycle, tmp_path):
    # Every unit of a model boots with the same placeholder: with no serial
    # and no waMA, a placeholder table leaves nothing to name the router by.
    run, state, *_ = cycle
    table = tmp_path / 'mac'
    table.write_text(PLACEHOLDER + '\n')
    placeholder = {'TC_TEST_IFLIST': 'mac_file', 'TC_TEST_IFLIST_MAC': str(table), 'TC_TEST_ACP_KEY': 'waMA',
                   'TC_TEST_ACP_MODE': 'value', 'TC_TEST_ACP_VALUE': PLACEHOLDER}
    assert run('false', **placeholder).returncode == 0
    assert state['payloads'][0]['router_id'] == 'C86TEST0SN01' and state['payloads'][0]['mac'] is None
    result = run('false', TC_TEST_ACP_NO_SERIAL='1', **placeholder)
    assert result.returncode == 1 and 'no router serial or hardware address' in result.stderr
    assert len(state['calls']) == 1


def test_daemon_without_a_serial_retries_until_wama_is_real(cycle, acp_calls, tmp_path):
    # No serial, and waMA and the table hold the placeholder: nothing names
    # the router, so the boot heartbeat fails acp_unavailable, and each retry
    # collects again, waMA included, until it answers the real address.
    _, state, _, binary, env = cycle
    iflist = tmp_path / 'mac'
    iflist.write_text(PLACEHOLDER + '\n')
    env = {**env, 'TC_TEST_ACP_CALLS': str(acp_calls), 'TC_TEST_ACP_NO_SERIAL': '1', 'TC_TEST_ACP_KEY': 'waMA',
           'TC_TEST_ACP_MODE': 'countdown', 'TC_TEST_ACP_VALUE': PLACEHOLDER, 'TC_TEST_ACP_UNTIL': '2',
           'TC_TEST_ACP_LATER': '80:EA:96:E6:58:68', 'TC_TEST_IFLIST': 'mac_file', 'TC_TEST_IFLIST_MAC': str(iflist)}
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        wait_until(lambda: len(state['calls']) == 1)
        boot = state['payloads'][0]
        assert boot['reason'] == 'boot' and boot['router_id'] == f'mac-{REAL_MAC}' and boot['mac'] == REAL_MAC
        assert boot['delivery']['failed'] == 2 and boot['delivery']['classes']['acp_unavailable'] == 2
        assert len(wama_reads(acp_calls)) == 3
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=HANG_TIMEOUT)
    assert f'hardware address {REAL_MAC} from ACP waMA' in stderr


def test_a_device_short_of_processes_with_no_identity_stays_on_the_local_schedule(cycle, acp_calls, tmp_path):
    # No serial and a placeholder table, and every fork refused from the
    # start: no acp read works, so nothing names the router. Facts that name
    # no router are never kept, so every retry tries acp again and fails for
    # want of processes (local_resource), never acp_unavailable on kept
    # facts, which would grow the network back-off. Once forks work, the
    # retry reads acp and goes out under waMA.
    _, state, _, binary, env = cycle
    iflist = tmp_path / 'mac'
    iflist.write_text(PLACEHOLDER + '\n')
    fork_fail = tmp_path / 'fork-fail'
    fork_fail.touch()
    log = tmp_path / 'stderr'
    env = {**env, 'TC_TEST_ACP_CALLS': str(acp_calls), 'TC_TEST_ACP_NO_SERIAL': '1', 'TC_TEST_ACP_KEY': 'waMA',
           'TC_TEST_ACP_MODE': 'value', 'TC_TEST_ACP_VALUE': '80:EA:96:E6:58:68', 'TC_TEST_IFLIST': 'mac_file',
           'TC_TEST_IFLIST_MAC': str(iflist), 'TC_TEST_FORK_FAIL': str(fork_fail)}
    with open(log, 'w') as stderr:
        process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE,
                                   stderr=stderr)
    try:
        wait_until(lambda: log.read_text().count('no router serial or hardware address') >= 3)
        assert state['calls'] == [] and recorded_collectors(acp_calls) == []
        fork_fail.unlink()
        wait_until(lambda: len(state['calls']) == 1)
        boot = state['payloads'][0]
        assert boot['reason'] == 'boot' and boot['router_id'] == f'mac-{REAL_MAC}'
        classes = boot['delivery']['classes']
        assert boot['delivery']['failed'] >= 3 and classes['local_resource'] == boot['delivery']['failed']
        assert classes['acp_unavailable'] == 0
        assert wama_reads(acp_calls)
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


def test_daemon_without_a_serial_waits_for_a_real_table_address(cycle, acp_calls, tmp_path):
    # No serial and no waMA: the boot heartbeat retries while the table holds
    # the placeholder, and goes under the real address once boot sets it.
    _, state, _, binary, env = cycle
    iflist = tmp_path / 'mac'
    iflist.write_text(PLACEHOLDER + '\n')
    env = {**env, 'TC_TEST_ACP_CALLS': str(acp_calls), 'TC_TEST_ACP_NO_SERIAL': '1', 'TC_TEST_ACP_KEY': 'waMA',
           'TC_TEST_ACP_MODE': 'empty', 'TC_TEST_IFLIST': 'mac_file', 'TC_TEST_IFLIST_MAC': str(iflist)}
    process = subprocess.Popen(telemetry_command(binary, '--daemon'), env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        # A third attempt has begun, so two have been refused for want of a
        # router ID (the third may still read the real address below).
        wait_until(lambda: sum(line.split()[0] == 'sySN' for line in recorded_collectors(acp_calls)) >= 3)
        assert state['calls'] == []
        iflist.write_text(REAL_MAC + '\n')
        wait_until(lambda: len(state['calls']) == 1)
        boot = state['payloads'][0]
        assert boot['reason'] == 'boot' and boot['router_id'] == f'mac-{REAL_MAC}' and boot['mac'] == REAL_MAC
        assert boot['delivery']['failed'] >= 2 and boot['delivery']['classes']['acp_unavailable'] == boot['delivery']['failed']
    finally:
        process.terminate(); process.communicate(timeout=HANG_TIMEOUT)


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
    assert payload['router_id'] == 'C86TEST0SN01' and payload['deploy_release_tag'] == 'test-release'
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
    ({'TC_TEST_IFLIST': 'sockaddr'}, 'iflist-sockaddr'),
    ({'TC_TEST_IFLIST': 'links'}, 'iflist-links'),
    ({'TC_TEST_IFLIST': 'addrs'}, 'iflist-addrs'),
])
def test_plan_error_is_short_and_omitted_after_recovery(cycle, changes, reason):
    run, state, *_ = cycle
    assert run('false', TC_TEST_PLAN_MODE='nat', **changes).returncode == 0
    payload = state['payloads'][-1]
    assert payload['plan_error'] == reason
    if reason.startswith('iflist-'):
        # The kernel's totals say how far past the table this device is.
        assert (payload['iflist_links'], payload['iflist_addrs']) == (40, 12)
    else:
        assert 'iflist_links' not in payload and 'iflist_addrs' not in payload
    assert run('false', TC_TEST_PLAN_MODE='nat').returncode == 0
    assert 'plan_error' not in state['payloads'][-1]
    assert 'iflist_links' not in state['payloads'][-1]


def test_heartbeat_reports_only_links_with_a_role_or_service_address(cycle):
    """Thirty bridged-extender interfaces own no address: the plan still
    validates and the heartbeat carries bridge0 alone, not lo0 (loopback is
    no service address) or the extenders, which the server would discard."""
    run, state, *_ = cycle
    assert run('false', TC_TEST_PLAN_MODE='bridge', TC_TEST_IFLIST='extenders').returncode == 0
    payload = state['payloads'][-1]
    assert 'plan_error' not in payload
    assert payload['links'] == [{'name': 'bridge0', 'role': 'isolated', 'families': ['ipv4']}]


def test_largest_possible_heartbeat_still_fits(cycle, rig):
    """Every field the device can make long, at the limit of its escape
    buffer in payload.c, at once: model and name (255 bytes that escape to
    510), the release tag likewise, plan_error with ten-digit kernel totals,
    and TC_MAX_LINKS links whose 15-byte names escape byte for byte. Sizes are
    the bytes that went over the wire. An oversized payload sends nothing."""
    run, state, *_ = cycle
    root, *_ = rig
    (root / 'config').write_text("TC_DEPLOY_RELEASE_TAG='" + '\\' * 255 + "'\n")
    result = run('false', TC_TEST_PLAN_MODE='bridge', TC_TEST_IFLIST='worst', TC_TEST_ACP_MODE='value',
                 TC_TEST_ACP_KEY='syAM,syNm', TC_TEST_ACP_VALUE='"' * 255)
    assert result.returncode == 0, result.stderr
    payload, raw = state['payloads'][-1], state['raw'][-1]
    assert payload['device_model'] == payload['device_name'] == '"' * 255
    assert payload['deploy_release_tag'] == '\\' * 255
    assert payload['plan_error'] == 'iflist-links'
    assert payload['iflist_links'] == payload['iflist_addrs'] == 4294967295
    assert [link['name'] for link in payload['links']] == ['\x01' * 15] * 32
    assert all(link['families'] == ['ipv4'] for link in payload['links'])
    # The old 4096-byte buffer could not have held it; the new one has room.
    assert 4096 < len(raw) < 16384 // 2
    # Everything before the delivery record and health stays within the
    # budget payload.c checks at compile time.
    base_max = header_define('telemetry/telemetry.h', 'TC_PAYLOAD_BASE_MAX')
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode()
    assert raw_bytes.index(b',"delivery":') + 2 <= base_max


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
