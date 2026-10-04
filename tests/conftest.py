from __future__ import annotations

from unittest import mock

import pytest


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
