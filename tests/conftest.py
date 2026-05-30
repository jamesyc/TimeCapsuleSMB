from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def block_unmocked_telemetry_posts(monkeypatch: pytest.MonkeyPatch):
    urlopen_mock = mock.Mock(side_effect=AssertionError("tests must not send telemetry"))
    monkeypatch.setattr("timecapsulesmb.telemetry.urllib.request.urlopen", urlopen_mock)
    yield
    urlopen_mock.assert_not_called()
