from __future__ import annotations

from unittest import mock

import pytest

# Native children close every descriptor up to the soft limit, which macOS can
# set above a million: about 0.1 s per child. `make test` runs pytest under
# `ulimit -n 256`; give a direct pytest run the same limit.
OPEN_FILE_LIMIT = 256


def pytest_configure(config: pytest.Config) -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft > OPEN_FILE_LIMIT:
        resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILE_LIMIT, hard))


@pytest.fixture(autouse=True)
def block_unmocked_telemetry_posts(monkeypatch: pytest.MonkeyPatch):
    urlopen_mock = mock.Mock(side_effect=AssertionError("tests must not send telemetry"))
    monkeypatch.setattr("timecapsulesmb.telemetry.urllib.request.urlopen", urlopen_mock)
    yield
    urlopen_mock.assert_not_called()


@pytest.fixture(autouse=True)
def block_real_keep_awake(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    # Device commands hold off macOS sleep with a real caffeinate child.
    # Only tests marked real_keep_awake may start one.
    if request.node.get_closest_marker("real_keep_awake") is None:
        monkeypatch.setattr("timecapsulesmb.core.keep_awake._start_caffeinate", lambda: None)
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

    def guarded(host: str, *, timeout: float):
        if acp.socket.create_connection is real_create_connection:
            raise AssertionError(f"tests must not open a real ACP connection to {host}")
        return open_connection(host, timeout=timeout)

    monkeypatch.setattr(acp, "_open_connection", guarded)
    yield


@pytest.fixture(autouse=True)
def device_password_matches(monkeypatch: pytest.MonkeyPatch):
    # Commands that reboot compare the password with the device's syPW over
    # SSH. Tests answer "match" (exit 0); a test that needs another answer
    # patches timecapsulesmb.device.probe.run_ssh_input itself.
    import subprocess

    from timecapsulesmb.device import probe

    monkeypatch.setattr(
        probe, "run_ssh_input",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["ssh"], 0, b"", b""),
    )
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
