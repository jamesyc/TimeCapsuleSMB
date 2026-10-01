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
