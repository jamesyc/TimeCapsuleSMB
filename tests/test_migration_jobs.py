"""A metadata migration left running by an interrupted deploy: finding it on
the device, reading how far it got, and deploy waiting for it."""
from __future__ import annotations

import subprocess
import threading
import unittest
from unittest import mock

from timecapsulesmb.app.events import AppClient, ClientDisconnected
from timecapsulesmb.device.migration_jobs import (
    JOBS_PS_COMMAND,
    MigrationActivity,
    MigrationProgress,
    MigrationProgressPoller,
    RunningMigration,
    parse_migration_log,
    probe_migration_activity,
    running_migrations,
)
from timecapsulesmb.services import deploy
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import (
    PREVIOUS_MIGRATION_STALL_SECONDS,
    DeployDeviceError,
    wait_for_previous_migration,
)
from timecapsulesmb.transport.errors import SshError
from timecapsulesmb.transport.ssh import SshConnection

LOG = "/Volumes/dk2/.samba4/logs/xattr-migration-copy.log"
COPY_ROW = f"  412 S     1:02.50 tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate --stall-seconds 300 --log {LOG} multi copy"


def migration(pid: int = 412, phase: str = "copy", cpu_time: str = "1:02.50", log: str | None = LOG) -> RunningMigration:
    return RunningMigration(pid, phase, log, cpu_time)


class RunningMigrationsTests(unittest.TestCase):
    def test_reads_phase_and_log_of_each_native_migrator(self) -> None:
        rows = "\n".join((
            COPY_ROW,
            "  413 R  0:00.10 xattr-hfs-migra /mnt/Memory/tc-xattr-hfs-migrate --log /x/cleanup.log multi cleanup",
            "  414 S  0:00.01 tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate --stall-seconds 300 --log /x/r.log multi retire",
            "  415 S  0:00.01 tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate --stall-seconds 300 --log /x/c.log inspect-root /Volumes/dk2",
            "  416 S  0:00.01 tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate --stall-seconds 300 inspect",
            "  417 S  0:00.01 tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate multi",
        ))
        self.assertEqual(running_migrations(rows), (
            RunningMigration(412, "copy", LOG, "1:02.50"),
            RunningMigration(413, "cleanup", "/x/cleanup.log", "0:00.10"),
            RunningMigration(414, "retire", "/x/r.log", "0:00.01"),
            RunningMigration(415, "inspect-root", "/x/c.log", "0:00.01"),
            RunningMigration(416, "inspect", None, "0:00.01"),
            RunningMigration(417, "unknown", None, "0:00.01"),
        ))

    def test_finds_shell_migrations_of_older_releases_by_argv_only(self) -> None:
        rows = "\n".join((
            "  20 S 0:00.02 sh /bin/sh /mnt/Flash/migrate.sh",
            "  21 S 0:00.02 sh /mnt/Flash/xattr-migrate-wrapper.sh",
            # The SSH wrapper that started it mentions the script but is not it.
            "  22 S 0:00.00 sh sh -c /mnt/Flash/migrate.sh",
        ))
        self.assertEqual(running_migrations(rows), (
            RunningMigration(20, "legacy", None, "0:00.02"),
            RunningMigration(21, "legacy", None, "0:00.02"),
        ))

    def test_netbsd4_rows_without_readable_arguments_still_count(self) -> None:
        # NetBSD 4's ps puts the command name in parentheses when it cannot
        # read argv, and the kernel's ucomm keeps 16 characters.
        self.assertEqual(running_migrations(" 1504 S    0:00.01 tc-xattr-hfs-mig (tc-xattr-hfs-mig)\n"),
                         (RunningMigration(1504, "unknown", None, "0:00.01"),))

    def test_skips_zombies_other_processes_and_malformed_rows(self) -> None:
        rows = "\n".join((
            "  30 Z 0:00.00 tc-xattr-hfs-mi",
            "  31 S 0:00.00 afpserver /sbin/afpserver",
            "  32 S 0:00.00 service service: role=job storage",
            "PID STAT TIME COMMAND",
            "",
            "  33 S",
        ))
        self.assertEqual(running_migrations(rows), ())


class MigrationLogTests(unittest.TestCase):
    def test_reads_the_latest_progress_line(self) -> None:
        log = "\n".join((
            "phase=copy sources=1 stall_seconds=300",
            "started_at=2026-10-05T00:12:10Z",
            "volume phase=copy uuid=ba18a59d-a088-5f29-bc7f-765a5cfb727c root=/Volumes/dk2 sources=1 start",
            "progress phase=copy volume=ba18a59d-a088-5f29-bc7f-765a5cfb727c entries=4000 matched=900 total=5000",
            "progress phase=copy volume=ba18a59d-a088-5f29-bc7f-765a5cfb727c entries=8000 matched=1800 total=5000",
        ))
        self.assertEqual(parse_migration_log(log), MigrationProgress("copy", "ba18a59d-a088-5f29-bc7f-765a5cfb727c", 8000))

    def test_falls_back_to_scan_progress_of_older_migrators(self) -> None:
        log = "\n".join((
            "phase=cleanup sources=2 stall_seconds=300",
            "volume phase=cleanup uuid=u1 root=/Volumes/dk2 sources=2 start",
            "scan progress entries=10000 path=/Volumes/dk2/Photos",
            "scan progress entries=20000 path=/Volumes/dk2/Music",
        ))
        self.assertEqual(parse_migration_log(log), MigrationProgress("cleanup", "u1", 20000))

    def test_a_new_volume_resets_the_counters(self) -> None:
        log = "\n".join((
            "progress phase=copy volume=u1 entries=9000 matched=10 total=20",
            "volume uuid=u1 complete entries=9000",
            "volume phase=copy uuid=u2 root=/Volumes/dk3 sources=1 start",
        ))
        self.assertEqual(parse_migration_log(log), MigrationProgress("copy", "u2", None))

    def test_ignores_a_line_cut_by_the_tail_and_an_empty_log(self) -> None:
        self.assertEqual(parse_migration_log("ss entries=999 matched=9 total=9\nscan progress entries=12 path=/x"),
                         MigrationProgress(None, None, 12))
        self.assertEqual(parse_migration_log(""), MigrationProgress())


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


class ProbeTests(unittest.TestCase):
    connection = SshConnection("root@10.0.0.2", "pw", "")

    def test_no_migration_costs_one_ps_call(self) -> None:
        with mock.patch("timecapsulesmb.device.migration_jobs.run_ssh", return_value=completed("  1 S 0:00.00 init /sbin/init\n")) as ssh:
            activity = probe_migration_activity(self.connection)
        self.assertEqual(activity, MigrationActivity(()))
        self.assertEqual(ssh.call_args.args[1], JOBS_PS_COMMAND)

    def test_reads_size_time_and_position_of_the_migration_log(self) -> None:
        listing = f"-rw-r--r--  1 0  0  48213 Oct  5 00:20:15 2026 {LOG}\n"
        tail = "s=1 start\nprogress phase=copy volume=u1 entries=500 matched=40 total=90\n"
        with mock.patch("timecapsulesmb.device.migration_jobs.run_ssh",
                        side_effect=[completed(COPY_ROW + "\n"), completed(listing + tail)]) as ssh:
            activity = probe_migration_activity(self.connection)
        self.assertEqual(activity.migrations, (migration(),))
        self.assertEqual((activity.log_size, activity.log_mtime), (48213, "Oct 5 00:20:15 2026"))
        self.assertEqual(activity.progress, MigrationProgress("copy", "u1", 500))
        self.assertIn(f"/usr/bin/tail -c 2048 {LOG}", ssh.call_args.args[1])
        self.assertFalse(ssh.call_args.kwargs["check"])

    def test_a_missing_log_leaves_only_the_process(self) -> None:
        with mock.patch("timecapsulesmb.device.migration_jobs.run_ssh",
                        side_effect=[completed(COPY_ROW + "\n"), completed("", returncode=1)]):
            activity = probe_migration_activity(self.connection)
        self.assertEqual(activity, MigrationActivity((migration(),)))

    def test_a_failed_ps_is_an_error_not_an_idle_device(self) -> None:
        with mock.patch("timecapsulesmb.device.migration_jobs.run_ssh", side_effect=SshError("ps failed")):
            with self.assertRaises(SshError):
                probe_migration_activity(self.connection)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class WaitForPreviousMigrationTests(unittest.TestCase):
    connection = SshConnection("root@10.0.0.2", "pw", "")

    def setUp(self) -> None:
        self.stages: list[str] = []
        self.messages: list[object] = []
        self.measurements: list[tuple[str, dict[str, object]]] = []
        self.callbacks = OperationCallbacks(
            set_stage=self.stages.append,
            log=self.messages.append,
            log_summary=self.messages.append,
            record_execution_measurement=lambda kind, **fields: self.measurements.append((kind, fields)),
        )
        self.clock = FakeClock()

    def wait(self, probe, callbacks: OperationCallbacks | None = None) -> None:
        wait_for_previous_migration(self.connection, callbacks=callbacks or self.callbacks, probe=probe,
                                    sleep=self.clock.sleep, monotonic=self.clock.monotonic)

    @staticmethod
    def running(size: int, cpu: str = "0:01.00", phase: str = "copy") -> MigrationActivity:
        return MigrationActivity((migration(phase=phase, cpu_time=cpu),), size, "Oct 5 00:20:15 2026",
                                 MigrationProgress(phase=phase))

    def test_an_idle_device_adds_no_stage_and_no_wait(self) -> None:
        probe = mock.Mock(return_value=MigrationActivity(()))
        self.wait(probe)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual((self.stages, self.messages, self.measurements), ([], [], []))
        self.assertEqual(self.clock.now, 1000.0)

    def test_waits_while_the_migration_progresses_then_continues(self) -> None:
        # Twenty minutes of a growing log, as on a large legacy database.
        sequence = [self.running(size) for size in range(1, 241)] + [MigrationActivity(())]
        probe = mock.Mock(side_effect=sequence)
        self.wait(probe)
        self.assertEqual(self.stages, ["wait_for_previous_migration"])
        self.assertEqual([getattr(m, "key", m) for m in self.messages], ["migration.waiting_for_previous"])
        self.assertEqual(self.measurements, [("previous_migration_wait",
                                              {"waited_sec": 1200.0, "phase": "copy", "outcome": "finished"})])

    def test_cpu_time_counts_as_progress_for_a_migrator_without_a_log(self) -> None:
        legacy = [MigrationActivity((migration(phase="legacy", log=None, cpu_time=f"0:{second:02d}.00"),))
                  for second in range(1, 100)]
        probe = mock.Mock(side_effect=[*legacy, MigrationActivity(())])
        self.wait(probe)
        self.assertEqual(self.measurements[-1][1]["outcome"], "finished")
        self.assertEqual(self.measurements[-1][1]["phase"], "legacy")

    def test_a_migration_without_progress_fails_once_its_own_guard_would_have_fired(self) -> None:
        probe = mock.Mock(return_value=self.running(500))
        with self.assertRaises(DeployDeviceError) as raised:
            self.wait(probe)
        self.assertEqual(raised.exception.code, "previous_migration_stalled")
        self.assertIn("stopped making progress", str(raised.exception))
        self.assertGreater(self.clock.now - 1000.0, PREVIOUS_MIGRATION_STALL_SECONDS)
        self.assertLessEqual(self.clock.now - 1000.0, PREVIOUS_MIGRATION_STALL_SECONDS + deploy.PREVIOUS_MIGRATION_POLL_SECONDS)
        self.assertEqual(self.measurements[-1][1]["outcome"], "stalled")

    def test_progress_resets_the_stall_clock(self) -> None:
        # 300 s frozen, one change, then 300 s frozen again: never 360 s without progress.
        frozen = [self.running(1)] * 60 + [self.running(2)] * 60
        probe = mock.Mock(side_effect=[*frozen, MigrationActivity(())])
        self.wait(probe)
        self.assertEqual(self.measurements[-1][1]["outcome"], "finished")

    def test_a_transient_probe_failure_is_retried(self) -> None:
        probe = mock.Mock(side_effect=[self.running(1), SshError("timeout"), SshError("timeout"), MigrationActivity(())])
        self.wait(probe)
        self.assertEqual(self.measurements[-1][1]["outcome"], "finished")

    def test_persistent_probe_failures_stop_the_deploy(self) -> None:
        probe = mock.Mock(side_effect=[self.running(1), SshError("a"), SshError("b"), SshError("c")])
        with self.assertRaises(SshError):
            self.wait(probe)

    def test_a_lost_app_ends_the_wait(self) -> None:
        client = AppClient()
        callbacks = OperationCallbacks(checkpoint=client.stop_if_disconnected)
        def probe(_connection):
            client.disconnect()
            return self.running(len(self.stages) + int(self.clock.now))
        with self.assertRaises(ClientDisconnected):
            self.wait(probe, callbacks)



class MigrationProgressPollerTests(unittest.TestCase):
    connection = SshConnection("root@10.0.0.2", "pw", "")

    def poll(self, readings: list[object]) -> list[MigrationProgress]:
        reported: list[MigrationProgress] = []
        done = threading.Event()
        remaining = list(readings)

        def read(_connection, log):
            self.assertEqual(log, LOG)
            if not remaining:
                done.set()
                return None, None, MigrationProgress()
            reading = remaining.pop(0)
            if isinstance(reading, Exception):
                raise reading
            return 1, "t", reading

        with MigrationProgressPoller(self.connection, LOG, reported.append, interval=0.001, read=read):
            self.assertTrue(done.wait(5))
        return reported

    def test_reports_each_new_position_once(self) -> None:
        first = MigrationProgress("copy", "u1", 10000)
        second = MigrationProgress("copy", "u1", 20000)
        self.assertEqual(self.poll([first, first, second]), [first, second])

    def test_skips_logs_without_a_counter_and_failed_reads(self) -> None:
        later = MigrationProgress("copy", "u1", 10)
        self.assertEqual(self.poll([MigrationProgress("copy"), SshError("busy"), later]), [later])

    def test_reports_nothing_after_the_phase_ends(self) -> None:
        reported: list[MigrationProgress] = []
        reading = threading.Event()
        release = threading.Event()

        def read(_connection, _log):
            reading.set()
            release.wait(5)
            return 1, "t", MigrationProgress("copy", "u1", 1)

        with MigrationProgressPoller(self.connection, LOG, reported.append, interval=0.001, read=read):
            self.assertTrue(reading.wait(5))
        release.set()
        threading.Event().wait(0.05)
        self.assertEqual(reported, [])


class WaitProgressTests(unittest.TestCase):
    def test_the_wait_shows_the_earlier_migrations_position(self) -> None:
        progress: list[tuple[str, dict[str, object]]] = []
        callbacks = OperationCallbacks(report_progress=lambda stage, **fields: progress.append((stage, fields)))
        clock = FakeClock()
        sequence = [
            MigrationActivity((migration(),), 1, "t", MigrationProgress("copy", "u1", 100)),
            MigrationActivity((migration(),), 2, "t", MigrationProgress("copy", "u1", 100)),
            MigrationActivity((migration(),), 3, "t", MigrationProgress("copy", "u1", 200)),
            MigrationActivity(()),
        ]
        wait_for_previous_migration(SshConnection("h", "pw", ""), callbacks=callbacks, probe=mock.Mock(side_effect=sequence),
                                    sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(progress, [
            ("wait_for_previous_migration", {"entries": 100}),
            ("wait_for_previous_migration", {"entries": 200}),
        ])


class ProgressEventTests(unittest.TestCase):
    def test_app_context_sends_a_progress_event_for_the_stage(self) -> None:
        from timecapsulesmb.app.context import AppOperationContext
        from timecapsulesmb.app.events import EventSink

        events: list[dict[str, object]] = []
        context = AppOperationContext("deploy", EventSink(lambda event: events.append(event.to_jsonable()), request_id="r"))
        context.to_operation_callbacks().progress("migrate_xattrs_copy", entries=4000)
        self.assertEqual(events, [{
            "schema_version": 1, "type": "progress", "operation": "deploy", "request_id": "r",
            "stage": "migrate_xattrs_copy", "entries": 4000,
        }])

    def test_cli_prints_progress_at_most_every_half_minute(self) -> None:
        import io
        from contextlib import redirect_stdout
        from timecapsulesmb.cli.context import CommandContext

        context = CommandContext(mock.Mock(), "deploy", "deploy_started", "deploy_finished")
        out = io.StringIO()
        times = iter([0.0, 10.0, 31.0, 40.0])
        with redirect_stdout(out):
            for entries in (1000, 2000, 3000, 4000):
                context.print_progress("migrate_xattrs_copy", entries=entries, now=lambda: next(times))
        self.assertEqual(out.getvalue().splitlines(), ["  1,000 files checked", "  3,000 files checked"])

    def test_cli_prints_the_first_progress_of_a_new_stage_at_once(self) -> None:
        import io
        from contextlib import redirect_stdout
        from timecapsulesmb.cli.context import CommandContext

        context = CommandContext(mock.Mock(), "deploy", "deploy_started", "deploy_finished")
        out = io.StringIO()
        reports = (
            ("migrate_xattrs_copy", 1000, 0.0),
            ("migrate_xattrs_cleanup", 50, 5.0),
            ("migrate_xattrs_cleanup", 60, 10.0),
            # Back to a stage seen before: a stage change still prints.
            ("migrate_xattrs_copy", 70, 12.0),
        )
        with redirect_stdout(out):
            for stage, entries, at in reports:
                context.print_progress(stage, entries=entries, now=lambda at=at: at)
        self.assertEqual(out.getvalue().splitlines(),
                         ["  1,000 files checked", "  50 files checked", "  70 files checked"])
