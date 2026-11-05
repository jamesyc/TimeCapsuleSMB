"""Build smbd, the metadata migrator and the regression drivers on the NetBSD VM.

    .venv/bin/python -m tests.samba.vm_build --out DIR [--lanes 6,4le,4be] [--install]

What AGENTS.md ("NetBSD Builds") describes by hand: sync this checkout's
files (tracked, and new ones git does not ignore, so an uncommitted new
patch is built too; not bin/) to the VM checkout with rsync -c; move aside patch
files there that this checkout does not have (the checkout is shared, and
patch_copy_overlay copies the whole overlay directory) and put them back
afterwards; install the lanes' VM packages once, then build each lane from
a clean tree (bin/ and .lock-wscript removed, since waf distclean does
nothing there) with the regression drivers, as root through su, detached,
the lanes at once as build/samba4xall.sh runs them (a lane whose download
fails is not built, and a failed lane's outputs are not copied); wait for
it, reporting each lane's result and ccache hits; fetch the
stripped outputs into DIR as smbd.<lane>, migrate.<lane> and <driver>.<lane>. With
--install, copy smbd and the migrator into bin/ and update
artifact-manifest.json. The VM rows of the lock file are held throughout, and
every step goes over one SSH connection, so a run normally logs in to the VM once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
VM = "james@192.168.64.4"
VM_REPO = "TimeCapsuleSMB"
PATCHES = "build/patches/samba4x"
# All calls share one connection (Vm adds its ControlPath): the first logs in
# and stays behind as the master, so a run logs in once rather than at every
# poll and copy, each a chance for the VM to refuse the password. ssh ends the
# master ControlPersist seconds after the last call and removes its socket.
# ServerAlive ends a master whose VM stopped answering, so a call exits 255
# (and is retried on a new master) instead of waiting on a dead link. One
# password prompt makes a refused password exit 255 too: on a second prompt
# sshpass would give up with its own 5.
SSH_OPTS = ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
            "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=15",
            "-o", "NumberOfPasswordPrompts=1", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120"]

LANES = {
    "6": dict(download="downloadsamba4x.sh", build="samba4x.sh", tree="/root/tc-samba4x-netbsd7/samba",
              stage="/root/tc-netbsd7", logs="/root/tc-earmv4-netbsd7", smbd="bin/samba4/smbd", migrator="bin/xattr-migrate/xattr-hfs-migrate"),
    "4le": dict(download="downloadsamba4xoldle.sh", build="samba4xoldle.sh", tree="/root/tc-samba4x-netbsd4le/samba",
                stage="/root/tc-netbsd4le", logs="/root/tc-earmv4-netbsd4", smbd="bin/samba4-netbsd4le/smbd",
                migrator="bin/xattr-migrate-netbsd4le/xattr-hfs-migrate"),
    "4be": dict(download="downloadsamba4xoldbe.sh", build="samba4xoldbe.sh", tree="/root/tc-samba4x-netbsd4be/samba",
                stage="/root/tc-netbsd4be", logs="/root/tc-armeb-netbsd4", smbd="bin/samba4-netbsd4be/smbd",
                migrator="bin/xattr-migrate-netbsd4be/xattr-hfs-migrate"),
}


def drivers() -> tuple[str, ...]:
    from tests.samba.run import TARGETS
    return TARGETS


def checkout_files(root: Path) -> list[str]:
    """The checkout's files to build from: tracked ones and new ones git does
    not ignore (a new patch before its commit), without bin/ outputs or
    tracked files deleted from the working tree."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], capture_output=True, check=True).stdout
    names = sorted({f.decode() for f in listed.split(b"\0") if f})
    return [n for n in names if not n.startswith("bin/") and (root / n).exists()]


def job_script(lanes: list[str], out: str, lock: str, driver_names: tuple[str, ...],
               repo: str = f"/home/james/{VM_REPO}", specs: dict | None = None) -> str:
    """The root build job: the lanes' VM packages installed once, then a clean
    build of each lane, the lanes at once (as build/samba4xall.sh runs them),
    outputs copied to out. A lane whose download fails is not built (its tree
    may be pristine Samba by then), and only a lane that built copies its
    outputs. Each lane at most once: two builds of one lane would share its
    tree."""
    if len(set(lanes)) != len(lanes):
        raise ValueError(f"a lane is named twice: {lanes}")
    specs = specs or LANES
    lines = ["#!/bin/sh",
             "# Written by tests/samba/vm_build.py: clean Samba lane builds with the regression drivers.",
             f"mkdir {lock} || {{ echo 'job already running'; exit 1; }}",
             f"O={out}; rm -rf $O; mkdir -p $O; chmod 755 $O",
             f"if ! cd {repo}; then echo 'CHECKOUT_MISSING {repo}'; rmdir {lock}; echo JOB_DONE; exit 1; fi",
             # One pkgin run before the lanes start, not one per lane at once.
             "if ! ./build/_samba4x_host_packages.sh > $O/host-packages.log 2>&1; then",
             f"  echo HOST_PACKAGES_FAILED; cat $O/host-packages.log; rmdir {lock}; echo JOB_DONE; exit 1",
             "fi"]
    for lane in lanes:
        spec = specs[lane]
        drivers_copy = " ".join(driver_names)
        lines += [
            "(",
            f"cd {repo} || exit 1",
            "start=$(date +%s)",
            f"if ./build/{spec['download']} > $O/download.{lane}.log 2>&1; then",
            f"  rm -rf {spec['tree']}/bin {spec['tree']}/.lock-wscript",
            f"  SAMBA4X_BUILD_REGRESSION_TESTS=1 ./build/{spec['build']} > $O/build.{lane}.log 2>&1; rc=$?",
            f"  echo \"{lane} BUILD_RC=$rc secs=$(( $(date +%s) - start ))\"",
            # The lane's own ccache summary (build/_samba4x.sh), if it had one.
            f"  sed -n 's/^ccache /{lane} ccache /p' $O/build.{lane}.log",
            "  if [ $rc -eq 0 ]; then",
            f"    cp {spec['stage']}/sbin/smbd.stripped $O/smbd.{lane}",
            f"    cp {spec['stage']}/bin/tc_xattr_hfs_migrate.stripped $O/migrate.{lane}",
            f"    for d in {drivers_copy}; do cp {spec['tree']}/bin/default/source3/modules/$d.stripped $O/$d.{lane} "
            f"2>/dev/null || echo \"{lane} no driver $d\"; done",
            "  fi",
            "else",
            f"  echo '{lane} DOWNLOAD_FAILED'",
            "fi",
            ") &",
        ]
    lines += ["wait", "chmod a+r $O/* 2>/dev/null", f"rmdir {lock}", "echo JOB_DONE"]
    return "\n".join(lines) + "\n"


# expect script: su to root on the VM and run $VMCMD there, exiting with its
# status. It exits 3 (password refused) or 4 (timeout, or the connection ended)
# before the shell prompt, when nothing has run, and Vm.root() retries those.
# A braced pattern list must span lines: on one line expect takes the whole
# list as a single pattern.
EXPECT = r'''set timeout 60
log_user 0
spawn sshpass -e ssh -tt {opts} {vm}
expect {{
  "$ " {{}}
  "denied" {{ exit 3 }}
  timeout {{ exit 4 }}
  eof {{ exit 4 }}
}}
send "su\r"
expect "assword"
send -- "$env(VMPASS)\r"
expect {{
  "# " {{}}
  timeout {{ exit 5 }}
}}
send -- "$env(VMCMD); echo __RC=\$?\r"
expect {{
  -re {{__RC=([0-9]+)}} {{ set rc $expect_out(1,string) }}
  timeout {{ exit 6 }}
}}
send "exit\r"
expect "$ "
send "exit\r"
expect eof
exit $rc
'''


def aside_files(local: list[str], remote: list[str]) -> list[str]:
    """Patch-directory files on the VM this checkout does not have."""
    return sorted(set(remote) - set(local))


def update_manifest(root: Path, lanes: list[str]) -> list[str]:
    """Set the manifest hashes of the installed smbd and migrators; return what changed."""
    path = root / "src/timecapsulesmb/assets/artifact-manifest.json"
    manifest = json.loads(path.read_text())
    wanted = {LANES[lane][kind] for lane in lanes for kind in ("smbd", "migrator")}
    changed = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("path") in wanted and "sha256" in node:
                digest = hashlib.sha256((root / node["path"]).read_bytes()).hexdigest()
                if digest != node["sha256"]:
                    node["sha256"] = digest
                    changed.append(node["path"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(manifest)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return changed


class Vm:
    def __init__(self, password: str, run=subprocess.run, sleep=time.sleep) -> None:
        self.env = dict(os.environ, SSHPASS=password, VMPASS=password)
        self.run, self.sleep = run, sleep
        # The master's socket is this object's own, with a random name: ssh
        # does not check who owns a socket it connects to, and /tmp is shared.
        # It is in /tmp because macOS caps a socket path at 104 bytes.
        self.control = f"/tmp/tc-vm-{os.getpid()}-{secrets.token_hex(4)}"
        self.opts = [*SSH_OPTS, "-o", f"ControlPath={self.control}"]

    def _retry(self, argv: list[str], copy: bool = False, **kwargs):
        # Password SSH to the VM sometimes refuses a good password; retry. ssh
        # reports that as 255. scp reports it as 1 like any failed copy, and
        # rsync with some nonzero code that depends on its version, so a copy,
        # which is safe to repeat, is retried after any failure.
        for attempt in range(4):
            proc = self.run(argv, env=self.env, capture_output=True, text=True, **kwargs)
            refused = proc.returncode != 0 if copy else proc.returncode == 255
            if not refused or attempt == 3:
                return proc
            self.sleep(4)
        return proc

    def ssh(self, command: str) -> str:
        proc = self._retry(["sshpass", "-e", "ssh", *self.opts, VM, command])
        if proc.returncode != 0:
            raise RuntimeError(f"VM command failed ({proc.returncode}): {command}\n{proc.stderr}")
        return proc.stdout

    def put(self, local: Path, remote: str) -> None:
        proc = self._retry(["sshpass", "-e", "scp", "-O", "-q", *self.opts, str(local), f"{VM}:{remote}"], copy=True)
        if proc.returncode != 0:
            raise RuntimeError(f"copy to the VM failed: {remote}")

    def get(self, remote: str, local: Path) -> None:
        proc = self._retry(["sshpass", "-e", "scp", "-O", "-q", *self.opts, f"{VM}:{remote}", str(local)], copy=True)
        if proc.returncode != 0:
            raise RuntimeError(f"copy from the VM failed: {remote}")

    def root(self, command: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".exp", delete=False) as script:
            script.write(EXPECT.format(vm=VM, opts=" ".join(self.opts)))
        try:
            for attempt in range(4):
                proc = self.run(["expect", "-f", script.name], env=dict(self.env, VMCMD=command),
                                capture_output=True, text=True)
                if proc.returncode not in (3, 4) or attempt == 3:
                    break
                self.sleep(4)
            if proc.returncode != 0:
                raise RuntimeError(f"root command failed ({proc.returncode}): {command}")
        finally:
            os.unlink(script.name)

    def sync(self, root: Path) -> None:
        listed = "\0".join(checkout_files(root))
        proc = self._retry(["rsync", "-ac", "--from0", "--files-from=-", "-e",
                            "sshpass -e ssh " + " ".join(self.opts), "./", f"{VM}:{VM_REPO}/"],
                           copy=True, cwd=root, input=listed)
        if proc.returncode != 0:
            raise RuntimeError(f"rsync to the VM failed ({proc.returncode})\n{proc.stderr}".rstrip())


def aside_command(repo: str, stash: str, files: list[str]) -> str:
    """Move files out of the VM checkout into stash. Vm.ssh retries a dropped
    connection even if the command ran, so a second run must keep what the
    first moved: it never empties stash and skips files already gone."""
    names = " ".join(shlex.quote(name) for name in files)
    return (f"cd {repo} && mkdir -p {stash} && for f in {names}; do if [ -e \"$f\" ]; then "
            f"mkdir -p {stash}/$(dirname \"$f\") && mv \"$f\" {stash}/\"$f\" || exit 1; fi; done")


def restore_command(repo: str, stash: str) -> str:
    """Put the stash back, but never over a file in the checkout: one there
    is newer than a stash that a crashed run left."""
    return (f"cd {stash} && for f in $(find . -type f); do if [ ! -e {repo}/\"$f\" ]; then "
            f"mkdir -p {repo}/$(dirname \"$f\") && mv \"$f\" {repo}/\"$f\"; fi; done; cd / && rm -rf {stash}")


def build(root: Path, lanes: list[str], out: Path, password: str, tag: str, install: bool,
          vm: Vm | None = None, log=print) -> dict[str, str]:
    vm = vm or Vm(password)
    local = [f for f in checkout_files(root) if f.startswith(PATCHES + "/")]
    remote = vm.ssh(f"cd {VM_REPO} && find {PATCHES} -type f").split()
    aside = aside_files(local, remote)
    stash = f"/tmp/{tag}-aside"
    if aside:
        vm.ssh(aside_command(f"~/{VM_REPO}", stash, aside))
    try:
        vm.sync(root)
        vm_out, lock = f"/tmp/{tag}-out", f"/tmp/{tag}-lock"
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as job:
            job.write(job_script(lanes, vm_out, lock, drivers()))
        vm.put(Path(job.name), f"/tmp/{tag}-job.sh")
        os.unlink(job.name)
        vm.root(f"chmod 755 /tmp/{tag}-job.sh; rm -f /tmp/{tag}-job.log; "
                f"(nohup /tmp/{tag}-job.sh > /tmp/{tag}-job.log 2>&1 < /dev/null &)")
        start = time.monotonic()
        while "JOB_DONE" not in vm.ssh(f"cat /tmp/{tag}-job.log 2>/dev/null; true"):
            if time.monotonic() - start > 3600:
                raise RuntimeError("the VM build did not finish within an hour")
            vm.sleep(20)
        report = vm.ssh(f"cat /tmp/{tag}-job.log")
        log(report.strip())
        if "CHECKOUT_MISSING" in report:
            raise RuntimeError(f"the VM has no checkout at ~/{VM_REPO}; no lane was built")
        if "HOST_PACKAGES_FAILED" in report:
            raise RuntimeError("installing the lanes' VM packages (build/_samba4x_host_packages.sh) failed; "
                               "no lane was built")
        failed = [lane for lane in lanes if f"{lane} BUILD_RC=0 " not in report]
        if failed:
            logs = ", ".join(f"{LANES[lane]['logs']}/{{download,}}samba4x*.log" for lane in failed)
            raise RuntimeError(f"build failed for {failed}; the lane logs on the VM: {logs}")
        out.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for lane in lanes:
            for name in ("smbd", "migrate", *drivers()):
                vm.get(f"{vm_out}/{name}.{lane}", out / f"{name}.{lane}")
                hashes[f"{name}.{lane}"] = hashlib.sha256((out / f"{name}.{lane}").read_bytes()).hexdigest()
    finally:
        # The VM's /tmp is a 1 GB tmpfs that other agents share; every run's
        # outputs (all drivers, three lanes) once filled it. The lane logs the
        # scripts write stay under /root.
        try:
            vm.root(f"rm -rf /tmp/{tag}-out /tmp/{tag}-job.log /tmp/{tag}-job.sh")
        except RuntimeError as error:
            log(f"could not remove /tmp/{tag}-out on the VM: {error}")
        if aside:
            vm.ssh(restore_command(f"~/{VM_REPO}", stash))
    if install:
        for lane in lanes:
            (root / LANES[lane]["smbd"]).write_bytes((out / f"smbd.{lane}").read_bytes())
            (root / LANES[lane]["migrator"]).write_bytes((out / f"migrate.{lane}").read_bytes())
        for path in update_manifest(root, lanes):
            log(f"manifest: {path}")
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lanes", default="6,4le,4be")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--vm-env", default=str(ROOT / ".env.backup6"),
                        help="env file whose TC_PASSWORD is the VM password")
    args = parser.parse_args()
    from tests.samba.locks import VM_CHECKOUT, VM_LANES, Locks, default_holder
    from timecapsulesmb.core.config import parse_env_file

    lanes = args.lanes.split(",")
    unknown = [lane for lane in lanes if lane not in LANES]
    if len(set(lanes)) != len(lanes):
        parser.error(f"each lane may be named once: {args.lanes}")
    if unknown:
        parser.error(f"unknown lanes {unknown}")
    password = parse_env_file(Path(args.vm_env))["TC_PASSWORD"]
    holder = default_holder(ROOT)
    tag = "tc-" + hashlib.sha256(holder.encode()).hexdigest()[:8]
    with Locks([VM_CHECKOUT, VM_LANES], holder, f"vm_build {','.join(lanes)}"):
        hashes = build(ROOT, lanes, args.out, password, tag, args.install)
    for name, digest in sorted(hashes.items()):
        if name.startswith(("smbd.", "migrate.")):
            print(f"{digest[:16]}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
