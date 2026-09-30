"""tests/samba/swap_smbd.py: running a built smbd without deploying, and restoring."""
from __future__ import annotations

import hashlib
import unittest

from tests.samba.swap_smbd import RAM_SMBD, Swapper, disk_of, restore_commands, smbd_parent, swap_commands

PS = """  PID  PPID ARGS
  276     1 service: role=manager
  301   276 /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf
  305   301 /mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf
  400   276 service: role=discovery
"""


class ParseTest(unittest.TestCase):
    def test_disk_of_a_share_root(self) -> None:
        self.assertEqual(disk_of("/Volumes/dk2/ShareRoot"), "/Volumes/dk2")
        with self.assertRaises(ValueError):
            disk_of("/mnt/Memory/x")

    def test_the_smbd_the_manager_runs(self) -> None:
        self.assertEqual(smbd_parent(PS), 301)

    def test_no_manager_or_no_smbd(self) -> None:
        with self.assertRaises(RuntimeError):
            smbd_parent("  1 0 init\n")
        with self.assertRaises(RuntimeError):
            smbd_parent("  276 1 service: role=manager\n")

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
        self.root = "/Volumes/dk2/ShareRoot"
        self.running = running
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.pid = 301
        self.link = False

    def sh(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("ps "):
            return f"276 1 service: role=manager\n{self.pid} 276 {RAM_SMBD} -F\n"
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
