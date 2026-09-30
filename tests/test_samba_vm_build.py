"""tests/samba/vm_build.py: the root build job, the patch-directory set-aside and the manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.samba import vm_build
from tests.samba.vm_build import LANES, aside_files, checkout_files, job_script, update_manifest


class JobScriptTest(unittest.TestCase):
    def test_builds_each_lane_clean_and_copies_every_output(self) -> None:
        text = job_script(["6", "4be"], "/tmp/o", "/tmp/l", ("tc_a_test", "tc_b_test"))
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("mkdir /tmp/l ||", text)
        for lane in ("6", "4be"):
            spec = LANES[lane]
            self.assertIn(f"rm -rf {spec['tree']}/bin {spec['tree']}/.lock-wscript", text)
            self.assertIn(f"SAMBA4X_BUILD_REGRESSION_TESTS=1 ./build/{spec['build']}", text)
            self.assertIn(f"$O/smbd.{lane}", text)
            self.assertIn(f"$O/migrate.{lane}", text)
        self.assertNotIn("netbsd4le", text)
        self.assertIn("for d in tc_a_test tc_b_test;", text)
        self.assertTrue(text.rstrip().endswith("echo JOB_DONE"))
        # The lane order is kept, and each download comes before its build.
        self.assertLess(text.index("downloadsamba4x.sh"), text.index("samba4x.sh >"))
        self.assertLess(text.index("samba4x.sh >"), text.index("downloadsamba4xoldbe.sh"))


class JobScriptRunTest(unittest.TestCase):
    """Run the job script under sh with stand-in download and build scripts."""

    def lane(self, tmp: Path, name: str, download_rc: int, build_rc: int) -> dict:
        tree, stage = tmp / f"tree-{name}", tmp / f"stage-{name}"
        (tmp / "repo/build").mkdir(parents=True, exist_ok=True)
        (tmp / f"repo/build/dl-{name}.sh").write_text(f"#!/bin/sh\nexit {download_rc}\n")
        (tmp / f"repo/build/b-{name}.sh").write_text(
            "#!/bin/sh\n"
            f"touch {tmp}/built-{name}\n"
            f"mkdir -p {tree}/bin/default/source3/modules {stage}/sbin {stage}/bin\n"
            f"echo drv > {tree}/bin/default/source3/modules/tc_a_test.stripped\n"
            f"echo smbd-{name} > {stage}/sbin/smbd.stripped\n"
            f"echo mig > {stage}/bin/tc_xattr_hfs_migrate.stripped\n"
            f"exit {build_rc}\n")
        for script in (tmp / "repo/build").iterdir():
            script.chmod(0o755)
        # Outputs an earlier build left behind must not be copied for a failed lane.
        (stage / "sbin").mkdir(parents=True)
        (stage / "sbin/smbd.stripped").write_text("stale")
        return dict(download=f"dl-{name}.sh", build=f"b-{name}.sh", tree=str(tree), stage=str(stage))

    def test_a_failed_download_is_not_built_and_failed_lanes_copy_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            specs = {"6": self.lane(tmp, "6", 0, 0), "4le": self.lane(tmp, "4le", 0, 2),
                     "4be": self.lane(tmp, "4be", 1, 0)}
            out = tmp / "out"
            text = job_script(["6", "4le", "4be"], str(out), str(tmp / "lock"), ("tc_a_test",),
                              repo=str(tmp / "repo"), specs=specs)
            proc = subprocess.run(["sh", "-c", text], capture_output=True, text=True, timeout=60)
            log = proc.stdout
            self.assertIn("6 BUILD_RC=0 ", log)
            self.assertIn("4le BUILD_RC=2 ", log)
            self.assertIn("4be DOWNLOAD_FAILED", log)
            self.assertNotIn("4be BUILD_RC", log)
            self.assertTrue(log.rstrip().endswith("JOB_DONE"))
            self.assertFalse((tmp / "built-4be").exists())
            self.assertTrue((tmp / "built-4le").exists())
            self.assertEqual((out / "smbd.6").read_text(), "smbd-6\n")
            self.assertTrue((out / "tc_a_test.6").exists())
            self.assertEqual(sorted(f.name for f in out.iterdir() if not f.name.endswith(".log")),
                             ["migrate.6", "smbd.6", "tc_a_test.6"])
            self.assertFalse((tmp / "lock").exists())


class CheckoutFilesTest(unittest.TestCase):
    def test_tracked_and_new_files_without_ignored_outputs_or_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            for name in ("build/patches/samba4x/series", "bin/samba4/smbd", "gone.txt", "src/a.py"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x")
            (root / ".gitignore").write_text("*.pyc\n")
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
            (root / "gone.txt").unlink()
            (root / "build/patches/samba4x/0070-new.patch").write_text("new")
            (root / "src/a.pyc").write_text("ignored")
            self.assertEqual(checkout_files(root), [".gitignore", "build/patches/samba4x/0070-new.patch",
                                                    "build/patches/samba4x/series", "src/a.py"])


class AsideTest(unittest.TestCase):
    def test_only_files_this_checkout_lacks(self) -> None:
        local = ["build/patches/samba4x/series", "build/patches/samba4x/0002-a.patch"]
        remote = local + ["build/patches/samba4x/0065-theirs.patch", "build/patches/samba4x/0003-old.patch"]
        self.assertEqual(aside_files(local, remote),
                         ["build/patches/samba4x/0003-old.patch", "build/patches/samba4x/0065-theirs.patch"])
        self.assertEqual(aside_files(local, local), [])


def repo(tmp: Path) -> Path:
    root = tmp / "repo"
    entries = []
    for lane in LANES:
        for kind in ("smbd", "migrator"):
            path = root / LANES[lane][kind]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old " + lane.encode() + kind.encode())
            entries.append({"path": LANES[lane][kind], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    entries.append({"path": "bin/service/service", "sha256": "unchanged"})
    manifest = root / "src/timecapsulesmb/assets/artifact-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"artifacts": entries}, indent=2) + "\n")
    return root


class ManifestTest(unittest.TestCase):
    def test_updates_only_the_installed_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(Path(tmp))
            (root / LANES["6"]["smbd"]).write_bytes(b"new smbd")
            changed = update_manifest(root, ["6", "4le"])
            self.assertEqual(changed, [LANES["6"]["smbd"]])
            data = json.loads((root / "src/timecapsulesmb/assets/artifact-manifest.json").read_text())
            by_path = {e["path"]: e["sha256"] for e in data["artifacts"]}
            self.assertEqual(by_path[LANES["6"]["smbd"]], hashlib.sha256(b"new smbd").hexdigest())
            self.assertEqual(by_path["bin/service/service"], "unchanged")


class FakeVm:
    def __init__(self, remote_patches: list[str], log: str, out_files: dict[str, bytes]) -> None:
        self.remote_patches, self.log, self.out_files = remote_patches, log, out_files
        self.commands: list[str] = []
        self.synced = False
        self.sleep = lambda _: None

    def ssh(self, command: str) -> str:
        self.commands.append(command)
        if command.endswith(f"find {vm_build.PATCHES} -type f"):
            return "\n".join(self.remote_patches)
        if "job.log" in command:
            return self.log
        return ""

    def put(self, local: Path, remote: str) -> None:
        self.commands.append(f"put {remote}")

    def get(self, remote: str, local: Path) -> None:
        local.write_bytes(self.out_files.get(remote.rsplit("/", 1)[1], b"x"))

    def root(self, command: str) -> None:
        self.commands.append(f"root {command}")

    def sync(self, root: Path) -> None:
        self.synced = True


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.local = ["build/patches/samba4x/series"]
        patcher = mock.patch.object(vm_build.subprocess, "run", side_effect=self.git)
        patcher.start()
        self.addCleanup(patcher.stop)
        drivers = mock.patch.object(vm_build, "drivers", return_value=("tc_a_test",))
        drivers.start()
        self.addCleanup(drivers.stop)

    def git(self, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=("\0".join(self.local) + "\0").encode())

    def repo_with_local(self, tmp: Path) -> Path:
        root = repo(tmp)
        for name in self.local:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("x")
        return root

    def test_builds_fetches_installs_and_puts_other_files_back(self) -> None:
        log = "6 BUILD_RC=0 secs=200\nJOB_DONE\n"
        vm = FakeVm(self.local + ["build/patches/samba4x/0065-theirs.patch"], log, {"smbd.6": b"new smbd"})
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo_with_local(Path(tmp))
            hashes = vm_build.build(root, ["6"], Path(tmp) / "out", "pw", "tc-x", install=True, vm=vm,
                                    log=lambda *_: None)
            self.assertEqual(hashes["smbd.6"], hashlib.sha256(b"new smbd").hexdigest())
            self.assertEqual((root / LANES["6"]["smbd"]).read_bytes(), b"new smbd")
        self.assertTrue(vm.synced)
        moved = [c for c in vm.commands if "0065-theirs.patch" in c]
        self.assertEqual(len(moved), 1)
        self.assertLess(vm.commands.index(moved[0]), vm.commands.index("put /tmp/tc-x-job.sh"))
        self.assertIn("mv $f ~/TimeCapsuleSMB/$f", vm.commands[-1])  # put back last

    def test_a_failed_lane_raises_and_still_puts_files_back(self) -> None:
        vm = FakeVm(self.local + ["build/patches/samba4x/0065-theirs.patch"], "6 BUILD_RC=1 secs=9\nJOB_DONE\n", {})
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo_with_local(Path(tmp))
            with self.assertRaises(RuntimeError):
                vm_build.build(root, ["6"], Path(tmp) / "out", "pw", "tc-x", install=True, vm=vm,
                               log=lambda *_: None)
            self.assertEqual((root / LANES["6"]["smbd"]).read_bytes(), b"old 6smbd")
        self.assertIn("mv $f ~/TimeCapsuleSMB/$f", vm.commands[-1])


class ExpectTest(unittest.TestCase):
    script = vm_build.EXPECT.format(vm="james@192.0.2.1")

    def test_pattern_lists_span_lines(self) -> None:
        # On one line, expect takes a braced list as a single pattern: every
        # step then waits out its timeout and the exit status is lost.
        for line in self.script.splitlines():
            if line.startswith("expect {"):
                self.assertEqual(line, "expect {")

    def test_the_status_pattern_is_a_braced_regex(self) -> None:
        # In a braced list "\\[" stays a backslash and the regex looks for a literal "[".
        self.assertIn("-re {__RC=([0-9]+)}", self.script)
        self.assertIn('send -- "$env(VMCMD); echo __RC=\\$?\\r"', self.script)

    @unittest.skipUnless(shutil.which("expect"), "expect is not installed")
    def test_expect_reports_the_command_status(self) -> None:
        # Run the script's logic against a local shell instead of the VM.
        local = self.script.replace(
            "spawn sshpass -e ssh -tt -o PubkeyAuthentication=no -o PreferredAuthentications=password "
            "-o StrictHostKeyChecking=no james@192.0.2.1",
            "spawn sh -c {PS1='ja$ '; export PS1; exec sh -i}")
        # A nested interactive shell with a root-style prompt stands in for su.
        local = local.replace('send "su\\r"\nexpect "assword"\nsend -- "$env(VMPASS)\\r"\n',
                              "send \"PS1='ja# ' sh -i\\r\"\n")
        with tempfile.NamedTemporaryFile("w", suffix=".exp", delete=False) as handle:
            handle.write(local)
        try:
            for command, status in (("true", 0), ("false", 1), ("exit 3 | true; (exit 7)", 7)):
                proc = subprocess.run(["expect", "-f", handle.name], env={"VMCMD": command, "VMPASS": "x",
                                                                           "PATH": "/usr/bin:/bin"},
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, status, (command, proc.stdout, proc.stderr))
        finally:
            Path(handle.name).unlink()


if __name__ == "__main__":
    unittest.main()
