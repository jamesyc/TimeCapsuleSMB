"""Find a metadata migration still running on the device, and how far it got.

A deploy runs the migrator over SSH, but the migrator is not tied to that
session: when the app or the Mac goes away mid-migration, it keeps going on
the device. The next deploy waits for it and doctor reports it, instead of
both treating the device as idle.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import shlex
import threading

from timecapsulesmb.transport.ssh import SshConnection, run_ssh


# One row per process: pid, state, CPU time, kernel ucomm, then argv.
JOBS_PS_COMMAND = "/bin/ps axww -o pid= -o stat= -o time= -o ucomm= -o command="
# The kernel truncates ucomm: tc-xattr-hfs-migrate shows as tc-xattr-hfs-mi.
MIGRATOR_UCOMMS = ("tc-xattr-hfs-mi", "xattr-hfs-migra")
# Shell migrations of releases before the native migrator.
LEGACY_MIGRATION_SCRIPTS = ("/mnt/Flash/migrate.sh", "/mnt/Flash/xattr-migrate-wrapper.sh")
# Options of the native migrator that take a value.
_VALUE_OPTIONS = {"--stall-seconds", "--log"}
LOG_TAIL_BYTES = 2048
PROBE_TIMEOUT_SECONDS = 30
# The migrator writes a progress line every 5 s (TC_PROGRESS_INTERVAL_SECONDS).
PROGRESS_POLL_SECONDS = 5
_FIELD = re.compile(r"(\w+)=(\S+)")


@dataclass(frozen=True)
class RunningMigration:
    pid: int
    # copy, cleanup or retire for `multi`; inspect / inspect-root; legacy for a
    # shell migration; unknown when argv names none.
    phase: str
    log_path: str | None
    cpu_time: str


@dataclass(frozen=True)
class MigrationProgress:
    """The latest position the migrator wrote to its log."""

    phase: str | None = None
    volume_uuid: str | None = None
    entries: int | None = None


@dataclass(frozen=True)
class MigrationActivity:
    migrations: tuple[RunningMigration, ...]
    log_size: int | None = None
    log_mtime: str | None = None
    progress: MigrationProgress = MigrationProgress()

    @property
    def phase(self) -> str | None:
        if not self.migrations:
            return None
        return self.progress.phase or self.migrations[0].phase

    def signature(self) -> tuple[object, ...]:
        """Changes whenever a migrator does work: its log grows or it uses CPU."""
        return (
            self.log_size,
            self.log_mtime,
            tuple((migration.pid, migration.cpu_time) for migration in self.migrations),
        )


def running_migrations(ps_output: str) -> tuple[RunningMigration, ...]:
    found = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].isdigit() or fields[1].startswith("Z"):
            continue
        pid, _state, cpu_time, ucomm, argv = int(fields[0]), fields[1], fields[2], fields[3], fields[4:]
        if ucomm.startswith(MIGRATOR_UCOMMS):
            found.append(RunningMigration(pid, _native_phase(argv), _option(argv, "--log"), cpu_time))
        elif ucomm == "sh":
            # Match argv, not text inside an SSH `sh -c` wrapper.
            if argv[:1] in (["/bin/sh"], ["sh"]):
                argv = argv[1:]
            if argv[:1] and argv[0] in LEGACY_MIGRATION_SCRIPTS:
                found.append(RunningMigration(pid, "legacy", None, cpu_time))
    return tuple(found)


def _option(argv: list[str], name: str) -> str | None:
    for index, word in enumerate(argv[:-1]):
        if word == name:
            return argv[index + 1]
    return None


def _native_phase(argv: list[str]) -> str:
    positional = []
    index = 1
    while index < len(argv):
        word = argv[index]
        index += 2 if word in _VALUE_OPTIONS else 1
        if not word.startswith("--"):
            positional.append(word)
    if positional[:1] == ["multi"]:
        return positional[1] if len(positional) > 1 else "unknown"
    return positional[0] if positional else "unknown"


def parse_migration_log(text: str) -> MigrationProgress:
    """Read the latest phase, volume and counters from the end of a migration log.

    The deploy writes `phase=copy sources=N ...` first; the migrator then logs
    `volume phase=copy uuid=... start`, `scan progress entries=N path=...` every
    10,000 entries, and `progress phase=... volume=... entries=N ...` every few
    seconds. The tail may start mid-line; that line is skipped. Only the file
    count is shown: the record counts it also logs cannot reach their total
    when rows belong to other volumes or deleted files.
    """
    phase = volume = None
    entries = None
    for line in text.splitlines():
        fields = dict(_FIELD.findall(line))
        if line.startswith("progress "):
            phase = fields.get("phase", phase)
            volume = fields.get("volume", volume)
            entries = _int(fields.get("entries"), entries)
        elif line.startswith("scan progress "):
            entries = _int(fields.get("entries"), entries)
        elif line.startswith("volume ") and line.endswith(" start"):
            phase = fields.get("phase", phase)
            volume = fields.get("uuid", volume)
            entries = None
        elif line.startswith("phase=") and "sources" in fields:
            phase = fields["phase"]
    return MigrationProgress(phase, volume, entries)


def _int(value: str | None, default: int | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else default


def read_migration_log(connection: SshConnection, log: str) -> tuple[int | None, str | None, MigrationProgress]:
    """The log's size and modification time, and the position its end records."""
    quoted = shlex.quote(log)
    output = run_ssh(
        connection,
        f"/bin/ls -lnT {quoted} 2>/dev/null && /usr/bin/tail -c {LOG_TAIL_BYTES} {quoted}",
        check=False,
        timeout=PROBE_TIMEOUT_SECONDS,
    ).stdout
    listing, _newline, tail = output.partition("\n")
    fields = listing.split()
    # -rw-r--r--  1 0  0  12345 Oct  5 00:20:15 2026 /Volumes/dk2/...
    if len(fields) < 10 or not fields[4].isdigit():
        return None, None, MigrationProgress()
    return int(fields[4]), " ".join(fields[5:9]), parse_migration_log(tail)


def probe_migration_activity(connection: SshConnection) -> MigrationActivity:
    """The migrations running now, and their log's size, time and position."""
    migrations = running_migrations(run_ssh(connection, JOBS_PS_COMMAND, timeout=PROBE_TIMEOUT_SECONDS).stdout)
    log = next((migration.log_path for migration in migrations if migration.log_path), None)
    if log is None:
        return MigrationActivity(migrations)
    size, mtime, progress = read_migration_log(connection, log)
    return MigrationActivity(migrations, size, mtime, progress)


class MigrationProgressPoller:
    """Reads a running migration's log and reports each new position.

    The migration itself is one blocking SSH command, so a thread polls its log
    over a separate session. A failed read is skipped: progress is only shown,
    it never decides whether the migration worked. Nothing is reported once the
    block exits, even if a read is still in flight.
    """

    def __init__(
        self,
        connection: SshConnection,
        log: str,
        report: Callable[[MigrationProgress], None],
        *,
        interval: float = PROGRESS_POLL_SECONDS,
        read: Callable[[SshConnection, str], tuple[int | None, str | None, MigrationProgress]] = read_migration_log,
    ) -> None:
        self._connection = connection
        self._log = log
        self._report = report
        self._interval = interval
        self._read = read
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="migration-progress", daemon=True)

    def __enter__(self) -> "MigrationProgressPoller":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self._stop.set()

    def _run(self) -> None:
        last = MigrationProgress()
        while not self._stop.wait(self._interval):
            try:
                progress = self._read(self._connection, self._log)[2]
            except Exception:
                continue
            if progress.entries is None or progress == last:
                continue
            last = progress
            with self._lock:
                if self._stop.is_set():
                    return
                self._report(progress)
