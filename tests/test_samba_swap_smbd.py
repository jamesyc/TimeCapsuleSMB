"""tests/samba/swap_smbd.py: running a built smbd without deploying, and restoring."""
from __future__ import annotations

import hashlib
import subprocess
import unittest
from unittest import mock

from tests.device_ps import ps_row
from tests.samba import device_processes
from tests.samba.links_device import Device, Remote, payload_disk
from tests.samba.swap_smbd import RAM_SMBD, Swapper, restore_commands, smbd_parent, swap_commands

# device_processes.COMMAND output: only service processes carry a command line.
PS = "\n".join([
    ps_row(276, "service", "service: role=manager", pgid=2),
    ps_row(301, "smbd", ppid=276),
    ps_row(305, "smbd", ppid=301, pgid=301, stat="D"),
    ps_row(400, "service", "service: role=discovery", ppid=276),
    ps_row(371, "mDNSResponder"),
]) + "\n"

# The runtime's smb.conf on a device with an internal disk (dk2, holding the
# payload) and a USB disk, each shared.
TWO_SHARES = """[global]
    log file = /Volumes/dk2/.samba4/logs/log.smbd
    max log size = 128

[4TB]
    path = /Volumes/dk3/ShareRoot
    xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb

[AirPort Disk]
    path = /Volumes/dk2/ShareRoot
    xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb
"""
ENV = {"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "x"}


class ParseTest(unittest.TestCase):
    def test_the_payload_disk_is_where_smbd_logs(self) -> None:
        self.assertEqual(payload_disk(TWO_SHARES), "/Volumes/dk2")
        with self.assertRaises(RuntimeError):
            payload_disk("[global]\n    log file = /mnt/Memory/log.smbd\n")

    def test_the_smbd_the_manager_runs(self) -> None:
        self.assertEqual(smbd_parent(PS), 301)

    def test_no_manager_or_no_smbd(self) -> None:
        with self.assertRaises(RuntimeError):
            smbd_parent(ps_row(1, "init", ppid=0, stat="Is"))
        with self.assertRaises(RuntimeError):
            smbd_parent(ps_row(276, "service", "service: role=manager", pgid=2))

    def test_commands(self) -> None:
        self.assertIn(f"ln -s /Volumes/dk2/.tc-swap-smbd/smbd {RAM_SMBD}", swap_commands("/Volumes/dk2"))
        restore = restore_commands("/Volumes/dk2")
        self.assertIn(f"cp /Volumes/dk2/.samba4/smbd {RAM_SMBD}", restore)
        self.assertIn("rm -rf /Volumes/dk2/.tc-swap-smbd", restore)
        # Restore does nothing when there is no deployed smbd to put back.
        self.assertTrue(restore.startswith("test -f /Volumes/dk2/.samba4/smbd &&"))


class FakeDevice:
    """A device whose manager restarts smbd with a new pid after a kill."""

    def __init__(self, running: bytes) -> None:
        self.running = running
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.pid = 301
        self.link = False

    def sh(self, command: str) -> str:
        self.commands.append(command)
        if command == device_processes.COMMAND:
            return f"{ps_row(276, 'service', 'service: role=manager', pgid=2)}\n{ps_row(self.pid, 'smbd', ppid=276)}\n"
        if command.startswith("kill "):
            assert command == f"kill {self.pid}"
            self.pid += 10
            if self.link:
                self.running = self.files["/Volumes/dk2/.tc-swap-smbd/smbd"]
            return ""
        if command.startswith("ls -l"):
            return f"lrwxr-xr-x 1 root {RAM_SMBD} -> x\n" if self.link else f"-rwxr-xr-x 1 root {RAM_SMBD}\n"
        if "ln -s" in command:
            self.link = True
        if command.startswith("test -f") and "cp " in command:
            self.link = False
            self.running = b"deployed"
        return ""

    def payload_disk(self) -> str:
        return "/Volumes/dk2"

    def sh_bytes(self, command: str) -> bytes:
        assert command == f"cat {RAM_SMBD}"
        return self.running

    def put(self, path: str, data: bytes) -> None:
        self.files[path] = data


class SwapperTest(unittest.TestCase):
    def test_swap_runs_the_new_binary_and_restore_puts_the_deployed_one_back(self) -> None:
        device = FakeDevice(b"deployed")
        swapper = Swapper(device, sleep=lambda _: None)
        self.assertEqual(swapper.swap(b"new smbd"), hashlib.sha256(b"new smbd").hexdigest())
        self.assertEqual(device.files["/Volumes/dk2/.tc-swap-smbd/smbd"], b"new smbd")
        self.assertIn("kill 301", device.commands)
        self.assertTrue(swapper.status().startswith("swapped: "))
        self.assertEqual(swapper.restore(), hashlib.sha256(b"deployed").hexdigest())
        self.assertTrue(swapper.status().startswith("deployed: "))

    def test_a_swap_that_does_not_take_effect_is_an_error(self) -> None:
        device = FakeDevice(b"deployed")
        device.sh_bytes = lambda command: b"something else"
        with self.assertRaises(RuntimeError):
            Swapper(device, sleep=lambda _: None).swap(b"new smbd")

    def test_a_device_with_several_shares_swaps_on_its_payload_disk(self) -> None:
        conf = subprocess.CompletedProcess([], 0, stdout=TWO_SHARES)
        with mock.patch("tests.samba.links_device.run_ssh", return_value=conf):
            with self.assertRaisesRegex(RuntimeError, "choose a share"):
                Device(ENV)
            self.assertEqual(Swapper(Remote(ENV)).disk, "/Volumes/dk2")

    def test_smbd_that_never_comes_back(self) -> None:
        device = FakeDevice(b"deployed")
        original = device.sh

        def no_restart(command: str) -> str:
            if command.startswith("kill "):
                return ""  # the pid stays: no new smbd
            return original(command)

        device.sh = no_restart
        with self.assertRaises(RuntimeError):
            Swapper(device, sleep=lambda _: None).restart()


if __name__ == "__main__":
    unittest.main()
