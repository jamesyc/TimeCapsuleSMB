"""tests/samba/links_device.py: taking a test mount off."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from tests.samba import links_device


class Umount:
    """umount that fails with the given outcomes, then unmounts."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.mounted = True

    def ismount(self, _path) -> bool:
        return self.mounted

    def __call__(self, command, **_kwargs):
        self.calls += 1
        assert command[0] == "umount"
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            self.mounted = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", outcome)


@pytest.fixture
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(links_device.time, "sleep", slept.append)
    return slept


def run(fake, monkeypatch, **kwargs) -> bool:
    monkeypatch.setattr(links_device.os.path, "ismount", fake.ismount)
    with mock.patch.object(links_device.subprocess, "run", fake):
        return links_device.unmount(Path("/tmp/mnt"), **kwargs)


def test_a_mount_comes_off_at_once(monkeypatch, waits, capsys):
    fake = Umount()
    assert run(fake, monkeypatch)
    assert fake.calls == 1 and waits == []
    assert capsys.readouterr().out == ""


def test_a_folder_that_is_not_a_mount_has_nothing_to_unmount(monkeypatch, waits):
    fake = Umount()
    fake.mounted = False
    assert run(fake, monkeypatch)
    assert fake.calls == 0


def test_a_busy_mount_is_tried_again(monkeypatch, waits, capsys):
    fake = Umount("umount: Resource busy", subprocess.TimeoutExpired("umount", 120))
    assert run(fake, monkeypatch, tries=5, wait=2.0)
    assert fake.calls == 3 and waits == [2.0, 2.0]
    assert capsys.readouterr().out == ""


def test_a_mount_another_process_took_off_counts_as_off(monkeypatch, waits):
    fake = Umount("umount: not currently mounted")
    original = fake.__call__

    def off_after_the_failure(command, **kwargs):
        result = original(command, **kwargs)
        fake.mounted = False
        return result

    monkeypatch.setattr(links_device.os.path, "ismount", fake.ismount)
    with mock.patch.object(links_device.subprocess, "run", off_after_the_failure):
        assert links_device.unmount(Path("/tmp/mnt"))
    assert fake.calls == 1


def test_a_mount_that_stays_is_reported_and_not_raised(monkeypatch, waits, capsys):
    fake = Umount(*["umount: Resource busy"] * 3)
    assert not run(fake, monkeypatch, tries=3)
    assert fake.calls == 3 and len(waits) == 2
    out = capsys.readouterr().out
    assert "WARNING /tmp/mnt is still mounted: umount: Resource busy" in out


def test_a_timeout_on_the_last_try_is_reported(monkeypatch, waits, capsys):
    fake = Umount(subprocess.TimeoutExpired("umount", 120))
    assert not run(fake, monkeypatch, tries=1)
    assert "umount timed out" in capsys.readouterr().out
