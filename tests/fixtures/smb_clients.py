"""This computer's SMB client as checks.smb.local_smb_client describes it, for
tests that patch that one lookup."""
from __future__ import annotations

from unittest import mock

from timecapsulesmb.checks.smb import LocalSmbClient

SMBCLIENT_PATH = "/usr/bin/smbclient"


def smbclient_here(*, path: str | None = SMBCLIENT_PATH, problem: str | None = None) -> LocalSmbClient:
    """smbclient at `path` (None: missing); `problem` is what its self-test reports."""
    return LocalSmbClient(
        kind="smbclient", label="smbclient", tool="smbclient", path=path,
        unreachable="NT_STATUS_HOST_UNREACHABLE", self_test=mock.Mock(return_value=problem),
    )


def apple_client_here(*, path: str | None = "/usr/bin/smbutil", problem: str | None = None) -> LocalSmbClient:
    """Apple's client, with smbutil at `path` (None: missing); `problem` is
    what loading NetFS reports."""
    return LocalSmbClient(
        kind="macos", label="Apple's SMB client", tool="smbutil", path=path,
        unreachable="No route to host", self_test=mock.Mock(return_value=problem),
    )
