"""Doctor's SMB checks through Apple's client (checks/smb_macos.py and its
macOS paths in checks/smb.py). NetFS, smbutil, umount and diskutil are faked,
so these run on every host; a real device run is the end-to-end check."""
from __future__ import annotations

import errno
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from timecapsulesmb.checks import smb, smb_macos
from timecapsulesmb.checks.doctor_steps import (
    _smb_listing_looks_like_local_route_failure,
    _smb_login_rejected,
)
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.smb import SmbClientTarget, authenticated_smb_listing_retryable
from tests.fixtures.smb_clients import apple_client_here

HEADER = "Share                                           Type    Comments\n-------------------------------\n"


def smbutil_row(name: str, kind: str, comment: str = "") -> str:
    """One row exactly as smbutil prints it: printf("%-48s%-8s%s"), padded by bytes."""
    return (name.encode().ljust(48) + kind.encode().ljust(8) + comment.encode()).decode()


NETBSD6_VIEW = (
    HEADER
    + smbutil_row("4TB", "Disk") + "\n"
    + smbutil_row("AirPort Disk", "Disk") + "\n"
    + smbutil_row("IPC$", "Pipe", "IPC Service (James's AirPort Time Capsule)") + "\n"
    + "\n3 shares listed\n"
)
AUTH_ERROR = "smbutil: server rejected the authentication: Authentication error\n"


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class SmbUrlTests(unittest.TestCase):
    def test_hosts_and_ports(self) -> None:
        cases = (
            ("10.0.0.2", None, "10.0.0.2"),
            ("10.0.0.2", 445, "10.0.0.2"),
            ("timecapsule.local", 445, "timecapsule.local"),
            # Apple's listing fails for an IP address with another port;
            # doctor's loopback tunnel works when named localhost.
            ("127.0.0.1", 2445, "localhost:2445"),
            ("::1", 2445, "localhost:2445"),
            ("127.0.0.1", 445, "127.0.0.1"),
            ("fe80::1%en0", 445, "[fe80::1%25en0]"),
            ("2001:db8::2", None, "[2001:db8::2]"),
            ("timecapsule.local", 8445, "timecapsule.local:8445"),
        )
        for host, port, expected in cases:
            with self.subTest(host=host, port=port):
                self.assertEqual(smb_macos.smb_url_host(host, port), expected)

    def test_share_names_are_escaped(self) -> None:
        self.assertEqual(smb_macos.smb_url("10.0.0.2", "AirPort Disk", 445), "smb://10.0.0.2/AirPort%20Disk")
        self.assertEqual(smb_macos.smb_url("10.0.0.2", "a/b", None), "smb://10.0.0.2/a%2Fb")


class SmbutilParseTests(unittest.TestCase):
    def parse(self, *rows: str) -> list[str] | None:
        return smb_macos.parse_smbutil_disk_shares(HEADER + "".join(row + "\n" for row in rows))

    def test_disk_shares_from_a_device_listing(self) -> None:
        self.assertEqual(smb_macos.parse_smbutil_disk_shares(NETBSD6_VIEW), ["4TB", "AirPort Disk"])

    def test_names_are_padded_by_bytes_not_characters(self) -> None:
        # A Mac names a disk with a curly apostrophe (3 bytes in UTF-8).
        for name in ("Jiaying’s Time Capsule", "Données", "バックアップ"):
            with self.subTest(name=name):
                self.assertEqual(self.parse(smbutil_row(name, "Disk"), smbutil_row("IPC$", "Pipe", "IPC")), [name])

    def test_a_name_of_48_bytes_or_more_runs_into_the_type(self) -> None:
        for name in ("A" * 48, "B" * 60, "Long name ending in Disk " + "x" * 30, "é" * 30):
            with self.subTest(name=name):
                row = smbutil_row(name, "Disk")
                self.assertNotIn(" Disk", row[len(name):len(name) + 1])
                self.assertEqual(self.parse(row, smbutil_row("IPC$", "Pipe", "IPC Service (Disk)")), [name])

    def test_a_comment_naming_a_type_is_not_a_row(self) -> None:
        self.assertEqual(self.parse(smbutil_row("Data", "Disk", "Pipe Disk Printer"), smbutil_row("IPC$", "Pipe")), ["Data"])

    def test_no_table_means_the_listing_failed(self) -> None:
        self.assertIsNone(smb_macos.parse_smbutil_disk_shares(""))
        self.assertIsNone(smb_macos.parse_smbutil_disk_shares(HEADER))
        self.assertIsNone(smb_macos.parse_smbutil_disk_shares(AUTH_ERROR))

    def test_only_ipc_is_a_listing_with_no_disk_shares(self) -> None:
        self.assertEqual(self.parse(smbutil_row("IPC$", "Pipe", "IPC Service")), [])


class ViewTests(unittest.TestCase):
    def test_lists_in_its_own_session_with_the_password_in_the_url(self) -> None:
        # -f: smbutil would otherwise reuse any session to the same host name,
        # such as another localhost tunnel's or another user's.
        self.assertEqual(
            smb_macos.view_args("127.0.0.1", "admin", "p@ss word", port=2445),
            [smb_macos.SMBUTIL, "view", "-N", "-f", "//admin:p%40ss%20word@localhost:2445"],
        )
        self.assertEqual(smb_macos.view_args("10.0.0.2", "admin", "pw", port=445)[-1], "//admin:pw@10.0.0.2")

    def test_no_password_shows_the_command_without_it(self) -> None:
        self.assertEqual(smb_macos.view_args("10.0.0.2", "admin", None)[-1], "//admin:***@10.0.0.2")

    def test_share_names_are_read_as_utf8_whatever_the_locale(self) -> None:
        with mock.patch.object(smb_macos, "run_process", return_value=completed(NETBSD6_VIEW)) as spawn:
            smb_macos.run_view(["smbutil"], timeout=7)
        spawn.assert_called_once_with(
            ["smbutil"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7,
        )


class UnmountTests(unittest.TestCase):
    def test_umount_alone_when_it_works(self) -> None:
        run = mock.Mock(return_value=completed())
        with mock.patch.object(smb_macos, "run_local_capture", run):
            smb_macos.unmount(Path("/m"))
        run.assert_called_once_with([smb_macos.UMOUNT, "-f", "/m"], timeout=20)

    def test_diskutil_when_umount_fails(self) -> None:
        run = mock.Mock(side_effect=[completed(stderr="umount: /m: Resource busy", returncode=1), completed()])
        with mock.patch.object(smb_macos, "run_local_capture", run):
            smb_macos.unmount(Path("/m"))
        self.assertEqual(run.call_args.args[0], [smb_macos.DISKUTIL, "unmount", "force", "/m"])

    def test_a_share_that_stays_mounted_is_an_error(self) -> None:
        run = mock.Mock(side_effect=[
            completed(returncode=1),
            completed(stderr="Unmount failed for /m", returncode=1),
            completed("//admin@10.0.0.2/Data on /m (smbfs, nodev, nosuid, nobrowse, mounted by me)\n"),
        ])
        with mock.patch.object(smb_macos, "run_local_capture", run):
            with self.assertRaises(smb_macos.SmbUnmountError) as raised:
                smb_macos.unmount(Path("/m"))
        self.assertEqual(str(raised.exception), "/m is still mounted: /sbin/umount exited 1; Unmount failed for /m")
        self.assertEqual(run.call_args.args[0], [smb_macos.MOUNT, "-t", "smbfs"])

    def test_diskutil_when_umount_does_not_finish_or_cannot_run(self) -> None:
        for failure in (subprocess.TimeoutExpired(["umount"], 20), OSError(2, "No such file or directory")):
            with self.subTest(failure=type(failure).__name__):
                run = mock.Mock(side_effect=[failure, completed()])
                with mock.patch.object(smb_macos, "run_local_capture", run):
                    smb_macos.unmount(Path("/m"))
                self.assertEqual(run.call_args.args[0], [smb_macos.DISKUTIL, "unmount", "force", "/m"])

    def test_a_share_gone_from_the_mount_table_counts_as_unmounted(self) -> None:
        # umount that did not finish in time may still have unmounted it.
        run = mock.Mock(side_effect=[
            subprocess.TimeoutExpired(["umount"], 20),
            completed(stderr="Unmount failed for /m", returncode=1),
            completed("//admin@nas.local/Media on /Volumes/Media (smbfs, nodev, nosuid, mounted by me)\n"),
        ])
        with mock.patch.object(smb_macos, "run_local_capture", run):
            smb_macos.unmount(Path("/m"))
        self.assertEqual(run.call_count, 3)

    def test_an_unreadable_mount_table_counts_as_still_mounted(self) -> None:
        timeout = subprocess.TimeoutExpired(["x"], 20)
        run = mock.Mock(side_effect=[timeout, timeout, OSError(5, "Input/output error")])
        with mock.patch.object(smb_macos, "run_local_capture", run):
            with self.assertRaises(smb_macos.SmbUnmountError) as raised:
                smb_macos.unmount(Path("/m"))
        self.assertEqual(
            str(raised.exception),
            "/m is still mounted: /sbin/umount did not finish within 20 s; /usr/sbin/diskutil did not finish within 20 s",
        )

    def test_mount_points_with_spaces_and_other_shares(self) -> None:
        table = (
            "//admin@10.0.0.2/Data on /private/var/folders/x/T/tcapsule-doctor-smb-1-a (smbfs, nodev, nosuid, nobrowse)\n"
            "//me@nas.local/My%20Files on /Volumes/My Files (smbfs, nodev, nosuid, mounted by me)\n"
            "/dev/disk3s1 on / (apfs, sealed, local, read-only, journaled)\n"
        )
        with mock.patch.object(smb_macos, "run_local_capture", return_value=completed(table)) as run:
            mount_points = smb_macos.smbfs_mount_points()
        self.assertEqual(mount_points, [Path("/private/var/folders/x/T/tcapsule-doctor-smb-1-a"), Path("/Volumes/My Files")])
        run.assert_called_once_with([smb_macos.MOUNT, "-t", "smbfs"], timeout=10)


class StaleMountSweepTests(unittest.TestCase):
    def sweep(self, table: subprocess.CompletedProcess[str]) -> list[Path]:
        with mock.patch.object(smb_macos, "run_local_capture", return_value=table):
            return smb_macos.unmount_stale_doctor_mounts()

    def mount_lines(self, *paths: str) -> subprocess.CompletedProcess[str]:
        lines = [f"//admin@10.0.0.2/Data on {path} (smbfs, nodev, nosuid, nobrowse, mounted by me)" for path in paths]
        lines.append("//admin@nas.local/Media on /Volumes/Media (smbfs, nodev, nosuid, mounted by me)")
        return completed("\n".join(lines) + "\n")

    def test_only_mounts_of_doctors_that_exited_are_unmounted(self) -> None:
        live = os.getpid()
        dead = 999_999_999
        mounts = self.mount_lines(
            f"/private/var/folders/x/T/tcapsule-doctor-smb-{dead}-abc",
            f"/private/var/folders/x/T/tcapsule-doctor-smb-{live}-def",
        )
        with mock.patch.object(smb_macos, "unmount") as unmount:
            removed = self.sweep(mounts)
        unmount.assert_called_once_with(Path(f"/private/var/folders/x/T/tcapsule-doctor-smb-{dead}-abc"))
        self.assertEqual(removed, [Path(f"/private/var/folders/x/T/tcapsule-doctor-smb-{dead}-abc")])

    def test_another_users_doctor_counts_as_running(self) -> None:
        mounts = self.mount_lines("/private/var/folders/x/T/tcapsule-doctor-smb-1-abc")
        with mock.patch.object(smb_macos.os, "kill", side_effect=PermissionError), \
             mock.patch.object(smb_macos, "unmount") as unmount:
            self.assertEqual(self.sweep(mounts), [])
        unmount.assert_not_called()

    def test_a_failed_sweep_is_left_for_next_time(self) -> None:
        mounts = self.mount_lines("/private/var/folders/x/T/tcapsule-doctor-smb-999999999-abc")
        with mock.patch.object(smb_macos, "unmount", side_effect=smb_macos.SmbUnmountError("busy")):
            self.assertEqual(self.sweep(mounts), [])

    def test_empty_folders_of_doctors_that_exited_are_removed(self) -> None:
        # A doctor stopped mid-mount leaves its folder and no mount.
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            dead_empty = temp / "tcapsule-doctor-smb-999999999-a"
            dead_full = temp / "tcapsule-doctor-smb-999999998-b"
            live = temp / f"tcapsule-doctor-smb-{os.getpid()}-c"
            other = temp / "tcapsule-doctor-other"
            for folder in (dead_empty, dead_full, live, other):
                folder.mkdir()
            (dead_full / "file").write_text("x")
            with mock.patch.object(smb_macos.tempfile, "gettempdir", return_value=tmp):
                self.sweep(completed(""))
            self.assertFalse(dead_empty.exists())
            # rmdir only: a folder with anything in it, as a mounted one has, stays.
            self.assertTrue((dead_full / "file").exists())
            self.assertTrue(live.exists())
            self.assertTrue(other.exists())


class MountTests(unittest.TestCase):
    def test_returns_the_mounted_path(self) -> None:
        with mock.patch.object(smb_macos, "_netfs_mount", return_value=Path("/tmp/m")) as netfs:
            self.assertEqual(smb_macos._mount_within("smb://h/s", Path("/tmp/m"), "admin", "pw", 5), Path("/tmp/m"))
        netfs.assert_called_once_with("smb://h/s", Path("/tmp/m"), "admin", "pw")

    def test_netfs_errors_come_back_with_their_errno(self) -> None:
        with mock.patch.object(smb_macos, "_netfs_mount", side_effect=smb_macos.SmbMountError(80)):
            with self.assertRaises(smb_macos.SmbMountError) as raised:
                smb_macos._mount_within("smb://h/s", Path("/tmp/m"), "admin", "pw", 5)
        self.assertEqual(raised.exception.code, 80)
        # EAUTH, "Authentication error" on macOS; other hosts name errno 80 differently.
        self.assertEqual(str(raised.exception), f"{os.strerror(80)} (errno 80)")

    def test_a_mount_that_finishes_after_the_deadline_is_unmounted_and_its_folder_removed(self) -> None:
        release = threading.Event()
        unmounted = threading.Event()
        removed = threading.Event()
        real_rmdir = Path.rmdir

        def slow_mount(_url: str, mountpoint: Path, *_args: object) -> Path:
            release.wait(10)
            return mountpoint

        def rmdir(path: Path) -> None:
            real_rmdir(path)
            removed.set()

        with tempfile.TemporaryDirectory() as tmp:
            mountpoint = Path(tmp) / "m"
            mountpoint.mkdir()
            with mock.patch.object(smb_macos, "_netfs_mount", side_effect=slow_mount), \
                 mock.patch.object(smb_macos, "unmount", side_effect=lambda path: unmounted.set()) as unmount, \
                 mock.patch.object(Path, "rmdir", rmdir):
                with self.assertRaises(smb_macos.SmbMountTimeout):
                    smb_macos._mount_within("smb://h/s", mountpoint, "admin", "pw", 0.05)
                # Nothing is unmounted while the mount is still running...
                self.assertFalse(unmounted.is_set())
                release.set()
                # ...and the late mount is unmounted once it finishes, then its folder removed.
                self.assertTrue(unmounted.wait(10))
                self.assertTrue(removed.wait(10))
            unmount.assert_called_once_with(mountpoint)
            self.assertFalse(mountpoint.exists())

    def test_the_mount_folder_is_reached_without_a_symlink_and_names_this_doctor(self) -> None:
        # NetFS refuses a mount point reached through a symlink (ELOOP), and
        # macOS's temporary directory is under /var -> /private/var.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp).resolve() / "real"
            real.mkdir()
            link = Path(tmp).resolve() / "link"
            link.symlink_to(real, target_is_directory=True)
            made = link / f"tcapsule-doctor-smb-{os.getpid()}-x"
            made.mkdir()
            with mock.patch.object(smb_macos.tempfile, "mkdtemp", return_value=str(made)) as mkdtemp, \
                 mock.patch.object(smb_macos, "_mount_within", side_effect=lambda url, mountpoint, *a: mountpoint) as mount_within, \
                 mock.patch.object(smb_macos, "unmount"):
                with smb_macos.mounted_share("10.0.0.2", "Data", "admin", "pw"):
                    pass
            self.assertEqual(mkdtemp.call_args.kwargs["prefix"], f"tcapsule-doctor-smb-{os.getpid()}-")
            self.assertEqual(mount_within.call_args.args[1], real / made.name)

    def test_mounted_share_always_unmounts_and_removes_its_directory(self) -> None:
        seen: list[Path] = []

        def fake_mount(url: str, mountpoint: Path, *_args: object) -> Path:
            seen.append(mountpoint)
            return mountpoint

        with mock.patch.object(smb_macos, "_mount_within", side_effect=fake_mount) as mount_within, \
             mock.patch.object(smb_macos, "unmount") as unmount:
            with self.assertRaises(RuntimeError):
                with smb_macos.mounted_share("10.0.0.2", "Data", "admin", "pw", port=445, timeout=9):
                    raise RuntimeError("check failed")
        self.assertEqual(mount_within.call_args.args[0], "smb://10.0.0.2/Data")
        self.assertEqual(mount_within.call_args.args[4], 9)
        unmount.assert_called_once_with(seen[0])
        self.assertFalse(seen[0].exists())

    def test_a_failed_mount_still_removes_its_directory(self) -> None:
        seen: list[Path] = []

        def failing_mount(url: str, mountpoint: Path, *_args: object) -> Path:
            seen.append(mountpoint)
            raise smb_macos.SmbMountError(errno.ENOENT)

        with mock.patch.object(smb_macos, "_mount_within", side_effect=failing_mount), \
             mock.patch.object(smb_macos, "unmount") as unmount:
            with self.assertRaises(smb_macos.SmbMountError):
                with smb_macos.mounted_share("10.0.0.2", "Nope", "admin", "pw"):
                    pass
        unmount.assert_not_called()
        self.assertFalse(seen[0].exists())


class LocalSmbClientTests(unittest.TestCase):
    """checks.smb.local_smb_client, the one place that says which SMB client
    this computer runs, where it is, and whether it works."""

    @contextmanager
    def on(self, platform: str, *, smbutil: bool = True, smbclient: str | None = None,
           netfs_problem: str | None = None, smbclient_problem: str | None = None):
        netfs = mock.Mock(return_value=netfs_problem)
        self_test = mock.Mock(return_value=smbclient_problem)
        with mock.patch.object(smb.sys, "platform", platform), \
             mock.patch.object(smb.os, "access", side_effect=lambda path, mode: smbutil and path == smb_macos.SMBUTIL), \
             mock.patch.object(smb, "find_command", side_effect=lambda name: smbclient if name == "smbclient" else None), \
             mock.patch.object(smb_macos, "apple_smb_client_problem", netfs), \
             mock.patch.object(smb, "broken_smbclient_output", self_test):
            yield smb.local_smb_client(), netfs, self_test

    def test_a_mac_runs_apples_client_from_its_fixed_path(self) -> None:
        with self.on("darwin") as (client, netfs, _):
            self.assertEqual(
                (client.kind, client.label, client.tool, client.path, client.unreachable),
                ("macos", "Apple's SMB client", "smbutil", smb_macos.SMBUTIL, "No route to host"),
            )
            self.assertIsNone(client.unusable())
        netfs.assert_called_once_with()

    def test_a_mac_without_smbutil_is_missing_and_runs_no_self_test(self) -> None:
        with self.on("darwin", smbutil=False) as (client, netfs, _):
            self.assertIsNone(client.path)
            self.assertEqual(client.missing_message(), "missing local tool smbutil (/usr/bin/smbutil), which comes with macOS")
            self.assertIsNone(client.problem())
            self.assertEqual(client.unusable(), "is missing on this computer")
        netfs.assert_not_called()

    def test_a_mac_whose_netfs_does_not_load_does_not_work(self) -> None:
        with self.on("darwin", netfs_problem="NetFS.framework could not be loaded: gone") as (client, _, _):
            self.assertEqual(client.problem(), "NetFS.framework could not be loaded: gone")
            self.assertEqual(client.unusable(), "on this computer does not work")

    def test_elsewhere_smbclient_is_found_on_the_path_and_tested_there(self) -> None:
        with self.on("linux", smbclient="/opt/samba/bin/smbclient", smbclient_problem="BACKTRACE") as (client, netfs, self_test):
            self.assertEqual(
                (client.kind, client.label, client.tool, client.path, client.unreachable),
                ("smbclient", "smbclient", "smbclient", "/opt/samba/bin/smbclient", "NT_STATUS_HOST_UNREACHABLE"),
            )
            self.assertEqual(client.problem(), "BACKTRACE")
            self.assertEqual(client.unusable(), "on this computer does not work")
        # Doctor tests the smbclient it found, by its path (the result is cached per path).
        self.assertEqual({call.args for call in self_test.call_args_list}, {("/opt/samba/bin/smbclient",)})
        netfs.assert_not_called()

    def test_elsewhere_a_missing_smbclient_says_to_install_it(self) -> None:
        with self.on("linux") as (client, _, self_test):
            self.assertEqual(client.missing_message(), "missing local tool smbclient, please install smbclient on your computer")
            self.assertEqual(client.unusable(), "is missing on this computer")
        self_test.assert_not_called()

    def test_the_listing_runs_the_smbclient_the_lookup_found(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "Disk|Data|\nIPC|IPC$|IPC Service\n", "")
        with self.on("linux", smbclient="/opt/samba/bin/smbclient"), \
             mock.patch.object(smb, "run_local_capture", return_value=listing) as run:
            result = smb.check_authenticated_smb_listing("admin", "pw", "tc.local")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(run.call_args.args[0][0], "/opt/samba/bin/smbclient")

    def test_a_smbclient_that_cannot_start_is_a_failed_attempt_and_the_next_server_is_tried(self) -> None:
        listing = subprocess.CompletedProcess([], 0, "Disk|Data|\nIPC|IPC$|IPC Service\n", "")
        gone = FileNotFoundError(2, "No such file or directory")
        with self.on("linux", smbclient="/opt/samba/bin/smbclient"), \
             mock.patch.object(smb, "run_local_capture", side_effect=[gone, listing]):
            result = smb.check_authenticated_smb_listing("admin", "secret-pw", ["10.0.0.9", "10.0.0.2"])
        self.assertEqual(result.status, "PASS")
        first = result.details["attempts"][0]
        self.assertEqual(
            (first["outcome"], first["failure"]),
            ("error", "/opt/samba/bin/smbclient could not run: [Errno 2] No such file or directory"),
        )
        self.assertIn("'admin%***'", first["command"])
        self.assertNotIn("secret-pw", repr(result.details))

    def test_a_smbclient_that_cannot_start_fails_the_file_step_instead_of_stopping_doctor(self) -> None:
        with self.on("linux", smbclient="/opt/samba/bin/smbclient"), \
             mock.patch.object(smb, "run_local_capture", side_effect=FileNotFoundError(2, "No such file or directory")):
            results = smb.check_authenticated_smb_file_ops_detailed("admin", "pw", "tc.local", "Data")
        self.assertEqual(
            [(r.status, r.message) for r in results],
            [("FAIL", "SMB directory create failed: /opt/samba/bin/smbclient could not run: [Errno 2] No such file or directory")],
        )

    def test_without_a_client_neither_check_runs_anything(self) -> None:
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                with self.on(platform, smbutil=False), \
                     mock.patch.object(smb, "run_local_capture") as run, \
                     mock.patch.object(smb_macos, "run_view") as run_view, \
                     mock.patch.object(smb_macos, "mounted_share") as mounted_share:
                    listing = smb.check_authenticated_smb_listing("admin", "pw", "tc.local")
                    file_ops = smb.check_authenticated_smb_file_ops_detailed("admin", "pw", "tc.local", "Data")
                self.assertEqual(listing.status, "FAIL")
                self.assertTrue(listing.message.startswith("missing local tool "))
                self.assertEqual([r.status for r in file_ops], ["WARN"])
                self.assertTrue(file_ops[0].message.startswith("SMB file-ops verification skipped: missing local tool "))
                run.assert_not_called()
                run_view.assert_not_called()
                mounted_share.assert_not_called()

    def test_required_local_tools_name_smbutil_on_a_mac(self) -> None:
        with self.on("darwin"), mock.patch("timecapsulesmb.checks.local_tools.require_local_ssh"):
            messages = [result.message for result in check_required_local_tools()]
        self.assertIn("local tool smbutil is installed", messages)
        self.assertFalse([message for message in messages if "smbclient" in message])


class AppleListingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(smb, "local_smb_client", return_value=apple_client_here())
        patcher.start()
        self.addCleanup(patcher.stop)

    def listing(self, server, spawn, *, port=445):
        with mock.patch.object(smb_macos, "run_process", spawn):
            return smb.check_authenticated_smb_listing("admin", "secret-pw", server, port=port)

    @staticmethod
    def view(stdout: str = NETBSD6_VIEW, stderr: str = "", returncode: int = 0):
        return completed(stdout, stderr, returncode)

    def test_lists_the_connect_address(self) -> None:
        spawn = mock.Mock(return_value=self.view())
        result = self.listing([SmbClientTarget("tc.local", "10.0.0.2")], spawn)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.message, "authenticated SMB listing works for admin@tc.local via 10.0.0.2")
        self.assertEqual(result.details["disk_shares"], ["4TB", "AirPort Disk"])
        # The connect address, as smbclient's -I was.
        self.assertEqual(spawn.call_args.args[0], [smb_macos.SMBUTIL, "view", "-N", "-f", "//admin:secret-pw@10.0.0.2"])
        self.assertEqual(spawn.call_args.kwargs["timeout"], 20)
        self.assertEqual(result.details["attempts"][0]["command"], f"{smb_macos.SMBUTIL} view -N -f '//admin:***@10.0.0.2'")
        self.assertNotIn("secret-pw", repr(result.details))

    def test_the_tunnel_lists_through_localhost(self) -> None:
        spawn = mock.Mock(return_value=self.view())
        self.listing("127.0.0.1", spawn, port=2445)
        self.assertEqual(spawn.call_args.args[0][-1], "//admin:secret-pw@localhost:2445")

    def test_a_timeout_records_the_command_without_the_password(self) -> None:
        result = self.listing("10.0.0.2", mock.Mock(side_effect=subprocess.TimeoutExpired(["smbutil"], 20)))
        attempt = result.details["attempts"][0]
        self.assertEqual((attempt["outcome"], attempt["command"]), ("timeout", f"{smb_macos.SMBUTIL} view -N -f '//admin:***@10.0.0.2'"))
        self.assertNotIn("secret-pw", repr(result.details))

    def test_a_rejected_password_is_reported_as_one(self) -> None:
        result = self.listing("10.0.0.2", mock.Mock(return_value=self.view(stdout="", stderr=AUTH_ERROR, returncode=77)))
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["attempts"][0]["failure"], AUTH_ERROR.strip())
        self.assertTrue(_smb_login_rejected(result))
        self.assertFalse(authenticated_smb_listing_retryable(result))

    def test_a_refused_route_reads_as_missing_local_network_permission(self) -> None:
        refused = "smbutil: server connection failed: No route to host\n"
        result = self.listing("10.0.0.2", mock.Mock(return_value=self.view(stdout="", stderr=refused, returncode=68)))
        self.assertTrue(_smb_listing_looks_like_local_route_failure(result))
        self.assertFalse(_smb_login_rejected(result))

    def test_timeouts_and_refused_connections_are_retryable(self) -> None:
        refused = self.view(stdout="", stderr="smbutil: server connection failed: Connection refused\n", returncode=68)
        for outcome in (subprocess.TimeoutExpired(["smbutil"], 20), refused):
            with self.subTest(outcome=outcome):
                spawn = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
                result = self.listing("10.0.0.2", spawn)
                self.assertEqual(result.status, "FAIL")
                self.assertTrue(authenticated_smb_listing_retryable(result))

    def test_smbutil_that_cannot_run_is_a_failed_attempt(self) -> None:
        result = self.listing("10.0.0.2", mock.Mock(side_effect=OSError(errno.ENOEXEC, "Exec format error")))
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.details["attempts"][0]["failure"], f"{smb_macos.SMBUTIL} could not run: [Errno 8] Exec format error",
        )

    def test_the_next_server_is_tried_after_a_failure(self) -> None:
        refused = self.view(stdout="", stderr="smbutil: server connection failed: Connection refused\n", returncode=68)
        result = self.listing(["10.0.0.9", "10.0.0.2"], mock.Mock(side_effect=[refused, self.view()]))
        self.assertEqual(result.status, "PASS")
        self.assertEqual([attempt["outcome"] for attempt in result.details["attempts"]], ["error", "pass"])
        self.assertEqual(result.details["server"], "10.0.0.2")


class AppleFileOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(smb, "local_smb_client", return_value=apple_client_here())
        patcher.start()
        self.addCleanup(patcher.stop)
        sweep = mock.patch.object(smb_macos, "unmount_stale_doctor_mounts", return_value=[])
        self.sweep = sweep.start()
        self.addCleanup(sweep.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share_root = Path(tmp.name)
        self.mount_calls: list[tuple] = []

    @contextmanager
    def fake_mounted_share(self, host, share, username, password, *, port=None, timeout=20):
        self.mount_calls.append((host, share, port))
        yield self.share_root

    def file_ops(self, mounted_share=None, **kwargs) -> list[CheckResult]:
        with mock.patch.object(smb_macos, "mounted_share", mounted_share or self.fake_mounted_share):
            return smb.check_authenticated_smb_file_ops_detailed("admin", "pw", "127.0.0.1", "Data", port=2445, **kwargs)

    def test_every_step_passes_with_smbclients_messages_and_leaves_nothing(self) -> None:
        results = self.file_ops()
        target = "admin@127.0.0.1/Data"
        self.assertEqual([(r.status, r.message) for r in results], [("PASS", f"{step} for {target}") for step in (
            "SMB directory create works",
            "SMB file create works",
            "SMB file overwrite/edit works",
            "SMB file read works",
            "SMB file rename works",
            "SMB file copy works",
            "SMB file delete works",
            "SMB directory ls list works",
            "SMB directory delete works",
            "SMB final cleanup check passed",
        )])
        self.assertEqual(self.mount_calls, [("127.0.0.1", "Data", 2445)])
        self.assertEqual(list(self.share_root.iterdir()), [])
        # Mounts an earlier doctor left behind are swept first.
        self.sweep.assert_called_once_with()

    def test_the_connect_address_is_mounted_when_given(self) -> None:
        with mock.patch.object(smb_macos, "mounted_share", self.fake_mounted_share):
            smb.check_authenticated_smb_file_ops_detailed("admin", "pw", "tc.local", "Data", ip_address="10.0.0.2", port=445)
        self.assertEqual(self.mount_calls, [("10.0.0.2", "Data", 445)])

    def test_a_failing_step_stops_there_and_cleans_up(self) -> None:
        with mock.patch.object(Path, "rename", side_effect=PermissionError(13, "Permission denied")):
            results = self.file_ops()
        self.assertEqual([r.status for r in results], ["PASS"] * 4 + ["FAIL"])
        self.assertTrue(results[-1].message.startswith("SMB file rename failed: "))
        self.assertEqual(list(self.share_root.iterdir()), [])

    def test_contents_that_do_not_match_fail_the_read(self) -> None:
        with mock.patch.object(smb, "_read_uncached", return_value=b"stale"):
            results = self.file_ops()
        self.assertEqual(results[-1].message, "SMB file read failed: downloaded contents did not match overwritten contents")
        self.assertEqual(list(self.share_root.iterdir()), [])

    def raising(self, error: BaseException, *, after_body: bool = False):
        @contextmanager
        def mounted_share(*_args, **_kwargs):
            if not after_body:
                raise error
            yield self.share_root
            raise error
        return mounted_share

    def test_mount_failures_timeouts_and_setup_errors(self) -> None:
        target = "admin@127.0.0.1/Data"
        cases = (
            (smb_macos.SmbMountError(80), f"SMB mount failed for {target}: {os.strerror(80)} (errno 80)"),
            (smb_macos.SmbMountTimeout("slow"), f"SMB mount timed out for {target}"),
            # mkdtemp in a full or unwritable temporary directory.
            (OSError(errno.ENOSPC, "No space left on device"), f"SMB mount failed for {target}: [Errno 28] No space left on device"),
        )
        for error, message in cases:
            with self.subTest(error=error):
                results = self.file_ops(mounted_share=self.raising(error))
                self.assertEqual([(r.status, r.message) for r in results], [("FAIL", message)])

    def test_contents_that_are_not_text_fail_the_read_instead_of_stopping_doctor(self) -> None:
        with mock.patch.object(smb, "_read_uncached", return_value=b"line1\n\xff\xfe"):
            results = self.file_ops()
        self.assertEqual(results[-1].message, "SMB file read failed: downloaded contents did not match overwritten contents")
        self.assertEqual(list(self.share_root.iterdir()), [])

    def test_the_copy_is_byte_for_byte(self) -> None:
        self.file_ops(mounted_share=self.keeping_copy())
        self.assertEqual(self.copied, [b"line1\nline2\nline3\nline4-updated\n"])

    def keeping_copy(self):
        self.copied: list[bytes] = []
        real_write = Path.write_bytes

        def write_bytes(path: Path, data: bytes) -> int:
            if path.name == ".sample-copy.txt":
                self.copied.append(data)
            return real_write(path, data)

        @contextmanager
        def mounted_share(*_args, **_kwargs):
            with mock.patch.object(Path, "write_bytes", write_bytes):
                yield self.share_root
        return mounted_share

    def test_an_unmount_that_does_not_finish_warns_after_the_steps(self) -> None:
        # The checks passed; umount and diskutil not finishing must not fail them.
        def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            if cmd[0] == smb_macos.MOUNT:
                return completed(f"//admin@127.0.0.1:2445/Data on {self.share_root} (smbfs, nodev, nosuid, nobrowse)\n")
            raise subprocess.TimeoutExpired(cmd, timeout)

        with mock.patch.object(smb_macos, "_mount_within", return_value=self.share_root), \
             mock.patch.object(smb_macos, "run_local_capture", side_effect=run):
            results = smb.check_authenticated_smb_file_ops_detailed("admin", "pw", "127.0.0.1", "Data", port=2445)
        self.assertEqual([r.status for r in results], ["PASS"] * 10 + ["WARN"])
        self.assertIn(f"{self.share_root} is still mounted: /sbin/umount did not finish within 20 s", results[-1].message)

    def test_a_share_left_mounted_is_reported_after_the_steps(self) -> None:
        results = self.file_ops(mounted_share=self.raising(smb_macos.SmbUnmountError("/m is still mounted: busy"), after_body=True))
        self.assertEqual([r.status for r in results], ["PASS"] * 10 + ["WARN"])
        self.assertEqual(
            results[-1].message,
            "SMB share stayed mounted after the checks for admin@127.0.0.1/Data: /m is still mounted: busy",
        )


if __name__ == "__main__":
    unittest.main()
