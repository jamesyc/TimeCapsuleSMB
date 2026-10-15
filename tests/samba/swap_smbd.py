"""Run a built smbd on a device without deploying it, and put the deployed one back.

    .venv/bin/python -m tests.samba.swap_smbd swap --env .env.backup6 bin/samba4/smbd
    .venv/bin/python -m tests.samba.swap_smbd restore --env .env.backup6
    .venv/bin/python -m tests.samba.swap_smbd status --env .env.backup6

The runtime runs smbd from /mnt/Memory/samba4/sbin/smbd, a copy of the
deployed /Volumes/dkN/.samba4/smbd (dkN from the running smb.conf, whatever
shares the device serves), and the manager restarts it about two seconds
after it exits. /mnt/Memory has too little room for a second 10 MB copy, so
swap copies the binary to that data disk (named smbd: the kernel's process
name comes from the file name, and doctor looks for "smbd"), points the RAM
path at it and stops the running smbd. restore copies the deployed
binary back to RAM and stops smbd again. Nothing is written to /mnt/Flash and
nothing reboots. The data disk can be unmounted while the swapped smbd runs
from it; this is for testing only, and a reboot also restores the deployed
smbd.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shlex
import sys
import time

from tests.samba import device_processes

RAM_SMBD = "/mnt/Memory/samba4/sbin/smbd"
SWAP_DIR = ".tc-swap-smbd"


def smbd_parent(ps_output: str) -> int:
    """The pid of the smbd the manager runs (its direct child), from
    device_processes.COMMAND output."""
    return device_processes.samba_parent(device_processes.parse(ps_output)).pid


def swap_commands(disk: str) -> str:
    target = f"{disk}/{SWAP_DIR}/smbd"
    return (f"chmod 755 {shlex.quote(target)} && rm -f {RAM_SMBD} && "
            f"ln -s {shlex.quote(target)} {RAM_SMBD}")


def restore_commands(disk: str) -> str:
    deployed = f"{disk}/.samba4/smbd"
    return (f"test -f {shlex.quote(deployed)} && rm -f {RAM_SMBD} && cp {shlex.quote(deployed)} {RAM_SMBD} && "
            f"chmod 755 {RAM_SMBD} && rm -rf {shlex.quote(disk + '/' + SWAP_DIR)}")


class Swapper:
    def __init__(self, device, sleep=time.sleep) -> None:
        self.device = device
        self.disk = device.payload_disk()
        self.sleep = sleep

    def running_hash(self) -> str:
        """sha256 of the file the RAM path resolves to (the device has no hash tools)."""
        return hashlib.sha256(self.device.sh_bytes(f"cat {RAM_SMBD}")).hexdigest()

    def restart(self) -> None:
        old = smbd_parent(self.device.sh(device_processes.COMMAND))
        self.device.sh(f"kill {old}")
        for _ in range(60):
            self.sleep(1)
            try:
                if smbd_parent(self.device.sh(device_processes.COMMAND)) != old:
                    return
            except RuntimeError:
                continue
        raise RuntimeError("smbd did not come back within 60 seconds")

    def swap(self, binary: bytes) -> str:
        self.device.sh(f"mkdir -p {shlex.quote(self.disk + '/' + SWAP_DIR)}")
        self.device.put(f"{self.disk}/{SWAP_DIR}/smbd", binary)
        self.device.sh(swap_commands(self.disk))
        self.restart()
        running = self.running_hash()
        if running != hashlib.sha256(binary).hexdigest():
            raise RuntimeError(f"the running smbd is {running[:16]}, not the swapped one")
        return running

    def restore(self) -> str:
        self.device.sh(restore_commands(self.disk))
        self.restart()
        return self.running_hash()

    def status(self) -> str:
        link = self.device.sh(f"ls -l {RAM_SMBD}").strip()
        return ("swapped: " if " -> " in link else "deployed: ") + self.running_hash()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("swap", "restore", "status"))
    parser.add_argument("binary", nargs="?", type=Path)
    parser.add_argument("--env", required=True)
    args = parser.parse_args()
    from tests.samba.links_device import Remote
    from timecapsulesmb.core.config import parse_env_file

    swapper = Swapper(Remote(parse_env_file(Path(args.env))))
    if args.action == "swap":
        if args.binary is None:
            parser.error("swap needs the smbd to run")
        print("running", swapper.swap(args.binary.read_bytes())[:16])
    elif args.action == "restore":
        print("running", swapper.restore()[:16])
    else:
        print(swapper.status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
