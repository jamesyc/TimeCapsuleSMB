from __future__ import annotations

from unittest import mock

import pytest

# Native children close every descriptor up to the soft limit, which macOS can
# set above a million: about 0.1 s per child. `make test` runs pytest under
# `ulimit -n 256`; give a direct pytest run the same limit.
OPEN_FILE_LIMIT = 256


def pytest_configure(config: pytest.Config) -> None:
    import os
    import resource

    # Set this before urllib builds its default opener. Proxy tests can opt in.
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft > OPEN_FILE_LIMIT:
        resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILE_LIMIT, hard))


@pytest.fixture(autouse=True)
def block_unmocked_telemetry_posts(monkeypatch: pytest.MonkeyPatch):
    post_mock = mock.Mock(side_effect=AssertionError("tests must not send telemetry"))
    monkeypatch.setattr("timecapsulesmb.telemetry.http_post_json", post_mock)
    yield
    post_mock.assert_not_called()


@pytest.fixture(autouse=True)
def hide_host_crypto_policy(monkeypatch: pytest.MonkeyPatch):
    # On Fedora and RHEL the host's OpenSSL refuses SHA-1 signatures, which
    # gives every ssh child an OpenSSL config and doctor another check.
    # Tests see neither the host's distribution nor its policy unless they
    # pass their own paths.
    import os
    from pathlib import Path

    missing = Path(os.devnull) / "missing"
    monkeypatch.setattr("timecapsulesmb.transport.openssl_sha1.OS_RELEASE_PATH", missing)
    monkeypatch.setattr("timecapsulesmb.transport.openssl_sha1.CRYPTO_POLICY_OPENSSL_PATH", missing)
    yield


@pytest.fixture(autouse=True)
def block_real_keep_awake(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    # Device commands hold off macOS sleep with a real caffeinate child.
    # Only tests marked real_keep_awake may start one.
    if request.node.get_closest_marker("real_keep_awake") is None:
        monkeypatch.setattr("timecapsulesmb.core.keep_awake._start_caffeinate", lambda: None)
    yield


@pytest.fixture(autouse=True)
def block_real_adisk_recheck(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    # A verified start makes this Mac re-check its cached Bonjour record for the
    # device over SSH and through libSystem. Tests skip that unless marked
    # real_adisk_recheck; recheck_cached_adisk's own tests pass it fakes.
    if request.node.get_closest_marker("real_adisk_recheck") is None:
        monkeypatch.setattr("timecapsulesmb.services.runtime_verification.recheck_cached_adisk",
                            lambda *_args, **_kwargs: None)
        monkeypatch.setattr("timecapsulesmb.discovery.native_dns_sd._reconfirm_record", lambda: None)
    yield


@pytest.fixture(autouse=True)
def block_real_acp_connections(monkeypatch: pytest.MonkeyPatch):
    # Every reboot now talks to the device's AirPort ACP over the network.
    # Tests use tests.reboot_support.FakeAcpDevice, or patch the socket the ACP
    # client opens; an unpatched call must fail instead of reaching a device.
    import socket

    from timecapsulesmb.integrations import acp

    real_create_connection = socket.create_connection
    open_connection = acp._open_connection

    def guarded(host: str, *, timeout: float, port: int = acp.ACP_PORT):
        if acp.socket.create_connection is real_create_connection:
            raise AssertionError(f"tests must not open a real ACP connection to {host}")
        return open_connection(host, timeout=timeout, port=port)

    monkeypatch.setattr(acp, "_open_connection", guarded)
    yield


@pytest.fixture(autouse=True)
def block_real_acpd_network_reads(monkeypatch: pytest.MonkeyPatch):
    # A network ACP read that goes unanswered makes the ACPd checks read the
    # listener again through a real SSH forward. A test that gets there patches
    # timecapsulesmb.device.acpd.acpd_answers_network with the answer it needs.
    from timecapsulesmb.device import acpd

    def unpatched(connection):
        raise AssertionError(f"tests must not open a real SSH forward to {connection.host}'s ACP")

    monkeypatch.setattr(acpd, "acpd_answers_network", unpatched)
    yield


@pytest.fixture(autouse=True)
def ssh_master_still_up(monkeypatch: pytest.MonkeyPatch):
    # A deploy that fails mid-install asks whether its shared SSH connection
    # survived; tests have no real one. They answer "up", so the failure keeps
    # its own message; a test of a lost connection patches it to False.
    monkeypatch.setattr("timecapsulesmb.services.deploy.ssh_master_alive", lambda _connection: True)
    yield


@pytest.fixture(autouse=True)
def device_password_matches(monkeypatch: pytest.MonkeyPatch):
    # Commands that reboot read the device over network ACP first, which checks
    # the admin password. Tests answer "match"; a test that needs another answer
    # patches timecapsulesmb.device.probe.read_airport_acp itself.
    from timecapsulesmb.device import probe

    from tests.reboot_support import acp_reading

    monkeypatch.setattr(probe, "read_airport_acp", lambda *_args, **_kwargs: acp_reading(True))
    yield


@pytest.fixture(autouse=True)
def apple_acpd_healthy(monkeypatch: pytest.MonkeyPatch):
    # Reboots, flash and doctor ask the device over SSH whether Apple's ACPd
    # answers syUT, has no stuck fork and ran the last restart requested, and
    # a reboot marks the restart on the device first (timecapsulesmb.device.acpd).
    # Tests answer "healthy" and "marked", and a silent listener's evidence
    # read finds nothing; a test that needs another answer patches that function.
    from timecapsulesmb.device import acpd

    monkeypatch.setattr(acpd, "acpd_answers", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(acpd, "read_processes", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(acpd, "restart_never_happened", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(acpd, "mark_restart", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(acpd, "network_silent_details", lambda *_args, **_kwargs: {})
    # A listing without ACPd is read again after a pause; tests do not wait.
    monkeypatch.setattr(acpd, "pause", lambda *_args, **_kwargs: None)
    yield


@pytest.fixture(autouse=True)
def acpd_runs_after_start(monkeypatch: pytest.MonkeyPatch):
    # Every wait for the runtime ends by reading whether ACPd still runs, for
    # telemetry (services.runtime_verification.acpd_after_start_measurement).
    # Tests answer "running"; a test that needs another answer passes
    # read_start_state or patches this function.
    from timecapsulesmb.device.probe import ManagerStartupAgeProbeResult

    monkeypatch.setattr(
        "timecapsulesmb.services.runtime_verification.probe_manager_startup_age_conn",
        lambda *_args, **_kwargs: ManagerStartupAgeProbeResult(None, "manager started 30s ago", 24, 54000,
                                                               acpd_running=True, acpd_pids=(190,)),
    )
    yield


@pytest.fixture(autouse=True)
def device_disks_healthy(monkeypatch: pytest.MonkeyPatch):
    # Install, uninstall and disk repair read the device's kernel log for disk
    # errors before they ask, and a runtime that does not start reads it to
    # say why (timecapsulesmb.device.disk_errors). Tests answer "no errors";
    # a test that needs some patches read_disk_errors_conn where it is used.
    # The preflight also reads the manager's title for a disk it could not
    # read: no verdicts.
    monkeypatch.setattr("timecapsulesmb.device.disk_errors.read_remote_command_conn", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("timecapsulesmb.device.disk_errors.manager_payload_problems", lambda *_args, **_kwargs: ())
    yield


@pytest.fixture(autouse=True)
def block_real_network_connections(monkeypatch: pytest.MonkeyPatch):
    # Tests use made-up device addresses such as 10.0.0.2 and capsule.local.
    # A real TCP connect to one, a UDP datagram to it (an NBNS query), or a
    # lookup of the name, waits seconds for a timeout and reaches whatever
    # answers on the user's network. Loopback servers, address literals,
    # multicast and UDP route lookups (a UDP connect sends nothing) stay
    # allowed. Code under test may swallow the error, so the attempt also
    # fails the test.
    import ipaddress
    import socket

    attempts: list[object] = []

    def remote(sock: socket.socket, address: object) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type != socket.SOCK_STREAM:
            return False
        host = str(address[0]) if isinstance(address, tuple) else str(address)
        try:
            return not ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return host != "localhost"

    def guard(real):
        def connect(sock: socket.socket, address: object):
            if remote(sock, address):
                attempts.append(address)
                raise AssertionError(f"tests must not open a real TCP connection to {address}")
            return real(sock, address)
        return connect

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name not in (None, "", "localhost"):
            try:
                ipaddress.ip_address(str(name).split("%", 1)[0])
            except ValueError:
                attempts.append(name)
                raise socket.gaierror(socket.EAI_NONAME, f"tests must not look up {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    real_sendto = socket.socket.sendto

    def sendto(sock: socket.socket, data, *args):
        address = args[-1]
        if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            try:
                ip = ipaddress.ip_address(str(address[0]).split("%", 1)[0])
            except ValueError:
                ip = None
            if ip is None or not (ip.is_loopback or ip.is_multicast):
                attempts.append(address)
                raise AssertionError(f"tests must not send a real datagram to {address}")
        return real_sendto(sock, data, *args)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "connect", guard(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(socket.socket.connect_ex))
    yield
    assert not attempts, f"tests must not reach the real network: {attempts}"
