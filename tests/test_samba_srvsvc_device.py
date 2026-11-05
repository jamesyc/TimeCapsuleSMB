"""tests/samba/srvsvc_device.py: the DCE/RPC it speaks, and how it bounds a wait."""
from __future__ import annotations

import struct
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from tests.samba import srvsvc_device as srvsvc


def ndr_string(text: str) -> bytes:
    data = (text + "\0").encode("utf-16-le")
    count = len(data) // 2
    return struct.pack("<III", count, 0, count) + data + b"\0" * (-len(data) % 4)


def answer(shares, *, status=0, level=1, ptype=srvsvc.RPC_RESPONSE, flags=3) -> bytes:
    """A NetShareEnumAll response with these (name, comment) pairs."""
    stub = struct.pack("<IIII", level, 1, 0x20000, len(shares))
    if not shares:
        stub += struct.pack("<I", 0)
    else:
        stub += struct.pack("<II", 0x20004, len(shares))
        for index in range(len(shares)):
            stub += struct.pack("<III", 0x20008 + index, 0, 0x30000 + index)
        for name, comment in shares:
            stub += ndr_string(name) + ndr_string(comment)
    # Total entries, the resume handle's pointer and value, the status.
    stub += struct.pack("<IIII", len(shares), 0x20010, 0, status)
    return struct.pack("<BBBB4sHHI", 5, 0, ptype, flags, b"\x10\0\0\0", 24 + len(stub), 0, 2) \
        + struct.pack("<IHH", len(stub), 0, 0) + stub


def test_bind_names_srvsvc_over_ndr():
    pdu = srvsvc.bind_pdu()
    assert pdu[2] == srvsvc.RPC_BIND
    assert struct.unpack_from("<H", pdu, 8)[0] == len(pdu)
    assert pdu[:4] == bytes((5, 0, srvsvc.RPC_BIND, 3))
    # max_xmit, max_recv, assoc, one context, then the context's two syntaxes.
    assert uuid.UUID(bytes_le=pdu[32:48]) == srvsvc.SRVSVC_UUID
    assert struct.unpack_from("<HH", pdu, 48) == (3, 0)
    assert uuid.UUID(bytes_le=pdu[52:68]) == srvsvc.NDR_UUID
    assert struct.unpack_from("<I", pdu, 68)[0] == 2


@pytest.mark.parametrize("server", ["host", "10.0.0.1"])
def test_enum_request_is_level_one_with_all_results(server):
    pdu = srvsvc.enum_all_pdu(server)
    assert struct.unpack_from("<H", pdu, 8)[0] == len(pdu)
    hint, _context, opnum = struct.unpack_from("<IHH", pdu, 16)
    stub = pdu[24:]
    assert hint == len(stub) and opnum == srvsvc.NET_SHARE_ENUM_ALL
    assert not len(stub) % 4
    name = ndr_string(rf"\\{server}")
    assert stub[4:4 + len(name)] == name
    rest = struct.unpack("<IIIIIIII", stub[4 + len(name):])
    assert rest[:2] == (1, 1)  # level, union switch
    assert rest[3:5] == (0, 0)  # an empty container
    assert rest[5] == 0xFFFFFFFF  # no limit on the answer
    assert rest[7] == 0  # from the first share


def test_answer_lists_names_not_comments():
    shares = [("AirPort Disk", "a comment"), ("IPC$", "IPC Service (Samba 4.25.0)"), ("x", "")]
    assert srvsvc.parse_enum_all(answer(shares)) == ["AirPort Disk", "IPC$", "x"]


def test_answer_keeps_a_non_ascii_name():
    assert srvsvc.parse_enum_all(answer([("irMac ディスク", "")])) == ["irMac ディスク"]


def test_empty_answer_has_no_share_array():
    assert srvsvc.parse_enum_all(answer([])) == []


@pytest.mark.parametrize("pdu, message", [
    (answer([("a", "")], status=5), "failed with 0x5"),
    (answer([("a", "")], level=2), "level 2"),
    (answer([("a", "")], ptype=3), "PDU type 3"),
    (answer([("a", "")], flags=1), "another fragment"),
])
def test_other_answers_are_refused(pdu, message):
    with pytest.raises(RuntimeError, match=message):
        srvsvc.parse_enum_all(pdu)


class Device:
    """Answers the commands a suite sends, and records them."""

    def __init__(self, output=""):
        self.output = output
        self.commands = []

    def sh(self, command, check=True):
        self.commands.append((command, check))
        return self.output

    def put(self, path, data):
        self.commands.append((path, data))


class Connection:
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    def disconnect(self):
        self.closed = True
        if self.error:
            raise self.error


def test_a_worker_that_answered_is_left_to_exit():
    device, conn = Device(), Connection()
    srvsvc._finish(device, conn, 7, True)
    assert device.commands == [] and conn.closed


def test_a_worker_that_stopped_answering_is_killed():
    device, conn = Device(), Connection(OSError("reset"))
    srvsvc._finish(device, conn, 7, False)
    assert device.commands == [("kill -9 7", False)] and conn.closed


def test_a_failed_lock_listing_is_not_a_file_without_locks():
    device = Device("f pid 12 write 0+1\n")
    assert srvsvc._kernel_locks(device, "/Volumes/dk2/a b") == ["f pid 12 write 0+1"]
    assert device.commands[0][1] is True  # sh() raises when the tool fails


def test_smb_conf_is_replaced_in_one_step():
    device = Device()
    srvsvc._replace_conf(device, b"[global]\n")
    assert device.commands == [(srvsvc.SMB_CONF_NEW, b"[global]\n"),
                               (f"mv {srvsvc.SMB_CONF_NEW} {srvsvc.SMB_CONF}", True)]


class Results:
    def __init__(self):
        self.failed = []

    def check(self, name, fn):
        try:
            ok = fn()
        except Exception:
            ok = False
        if not ok:
            self.failed.append(name)


def test_answers_says_whether_its_check_passed():
    results = Results()
    assert srvsvc._answers(results, "fast", 5, lambda: None) is True
    assert results.failed == []


def test_answers_counts_a_connection_that_died_as_no_answer():
    results = Results()

    def fails():
        raise OSError("closed")

    assert srvsvc._answers(results, "dead", 5, fails) is False
    assert results.failed == ["dead"]


NETSTAT = """Active Internet connections
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  192.168.1.5.50000      192.168.1.2.445        ESTABLISHED
tcp4       0      0  192.168.1.5.50001      192.168.1.2.445        CLOSE_WAIT
tcp4       0      0  192.168.1.5.50002      192.168.1.3.445        ESTABLISHED
tcp4       0      0  192.168.1.5.50003      192.168.1.2.4450       ESTABLISHED
tcp4       0      0  192.168.1.5.50004      10.192.168.1.2.445     ESTABLISHED
"""


def test_mac_ports_keeps_the_device_s_established_connections(monkeypatch):
    monkeypatch.setattr(srvsvc.socket, "gethostbyname", lambda host: "192.168.1.2")
    monkeypatch.setattr(srvsvc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, NETSTAT, ""))
    assert srvsvc._mac_ports("capsule.local") == {50000}


class Share:
    host = "192.168.1.2"
    share = "AirPort Disk"


MOUNTS = """/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
//root@192.168.1.3/AirPort%20Disk on /Volumes/Other (smbfs, nodev, nosuid, mounted by me)
//root@192.168.1.2/Backups on /Volumes/Backups (smbfs, nodev, nosuid, mounted by me)
//root@192.168.1.2/AirPort%20Disk on /Volumes/AirPort Disk (smbfs, nodev, nosuid, mounted by me)
"""


def different_hosts(monkeypatch):
    monkeypatch.setattr(srvsvc.socket, "gethostbyname", lambda host: host)


def test_existing_mount_finds_this_share_on_this_device(monkeypatch):
    different_hosts(monkeypatch)
    assert srvsvc._existing_mount(Share, MOUNTS) == Path("/Volumes/AirPort Disk")


def test_existing_mount_ignores_other_shares_and_devices(monkeypatch):
    different_hosts(monkeypatch)
    assert srvsvc._existing_mount(Share, MOUNTS.replace("192.168.1.2/AirPort", "192.168.1.4/AirPort")) is None


def test_existing_mount_matches_a_name_for_the_same_address(monkeypatch):
    monkeypatch.setattr(srvsvc.socket, "gethostbyname", lambda host: "192.168.1.2")
    mounts = "//root@capsule.local/AirPort%20Disk on /mnt/d (smbfs, nodev)\n"
    assert srvsvc._existing_mount(Share, mounts) == Path("/mnt/d")


def test_existing_mount_survives_a_name_that_does_not_resolve(monkeypatch):
    def fails(host):
        raise OSError("no such host")

    monkeypatch.setattr(srvsvc.socket, "gethostbyname", fails)
    assert srvsvc._existing_mount(Share, "//root@gone.local/AirPort%20Disk on /mnt/d (smbfs)\n") is None


def test_existing_mount_compares_share_names_normalized(monkeypatch):
    different_hosts(monkeypatch)

    class Nfd(Share):
        share = "irMac \u30c6\u3099\u30a3\u30b9\u30af"

    mounts = "//root@192.168.1.2/irMac%20%E3%83%87%E3%82%A3%E3%82%B9%E3%82%AF on /mnt/d (smbfs)\n"
    assert srvsvc._existing_mount(Nfd, mounts) == Path("/mnt/d")


def test_within_says_when_work_returned():
    assert srvsvc._within(5, lambda: None) is True


def test_within_passes_on_a_failure():
    def fails():
        raise OSError("gone")

    with pytest.raises(OSError, match="gone"):
        srvsvc._within(5, fails)


def test_within_gives_up_on_work_that_never_returns():
    release = threading.Event()
    try:
        assert srvsvc._within(0.05, release.wait) is False
    finally:
        release.set()
