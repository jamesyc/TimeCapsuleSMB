"""Deploy-only legacy metadata inventory and verified per-volume completion.

Receipts live beside the input TDBs because another deploy process must know
which volumes were fully verified. They never control the runtime manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import time
import uuid

from timecapsulesmb.device.storage import MaStVolume, ensure_volume_root_mounted_conn, read_mast_volumes_conn
from timecapsulesmb.transport.errors import SshError
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, run_ssh_input

VERSION = 1
POLICY = "mtime-nsec-uuid-path/logical-value/v1"
MAX_SOURCES = 32
MAX_KEYS = 262144
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
STALL_SECONDS = 300
# A progressing migration may legitimately run for hours. Direct exec preserves
# its real status; if SSH disappears it may finish without the client, while the
# process-local inactivity guard still bounds a stalled disk operation.
NATIVE_TIMEOUT_SECONDS: int | None = None
DIAGNOSTIC_TIMEOUT_SECONDS = 30
NATIVE_SSH_ARGS = (
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=2",
)
RECEIPT_SUFFIX = ".migration-progress.json"
RAM_HELPER = "/mnt/Memory/tc-xattr-hfs-migrate"
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_KEY = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{16}\Z")
_HEX = re.compile(r"(?:[0-9a-f]{2})+\Z")
STAT_FIELDS = ("inode", "size", "mtime", "nsec", "hash")
# M: verified value. O: proven orphan. X: verified, except values too large for
# a native HFS attribute that stay in the source DB. O and X both complete a
# volume and make retirement quarantine the DB instead of deleting it.
COVERAGE_KINDS = frozenset({"M", "O", "X"})
# Apple's firmware stores at most this many bytes in one HFS attribute.
NATIVE_XATTR_LIMIT = 3802
# The native report lists at most this many kept values; its counts cover all.
MAX_OVERSIZED_ITEMS = 50
OVERSIZED_KINDS = frozenset({"tdb", "appledouble"})
# Why a value stayed in legacy storage: too large for a native attribute, or a
# resource fork on a folder, which HFS cannot hold at any size.
KEPT_REASONS = frozenset({"size", "folder_fork"})


class MigrationStalledError(RuntimeError):
    """The native migrator made no progress before its inactivity guard fired."""


def normalized_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Missing or ambiguous HFS volume UUID: {value!r}") from exc


def source_id(source: dict) -> str:
    return source["uuid"] + ":" + source["relative"]


def source_identity(source: dict, *, mode: bool = True) -> dict:
    # st_dev and /Volumes/dkN are current transport/mount coordinates. The
    # filesystem UUID and inode survive Apple's disk-number reassignment.
    return {key: source[key] for key in ("uuid", "relative", *STAT_FIELDS, *(("mode",) if mode else ())) }


def cohort_hash(sources: list[dict]) -> str:
    data = {"version": VERSION, "policy": POLICY, "sources": sorted(sources, key=source_id)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_source(source: object) -> dict:
    if not isinstance(source, dict) or not _UUID.fullmatch(str(source.get("uuid", ""))):
        raise ValueError("invalid source UUID")
    relative = source.get("relative")
    if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        raise ValueError("invalid payload-relative path")
    for key in STAT_FIELDS[:-1]:
        if type(source.get(key)) is not int:
            raise ValueError("invalid source stat")
    if source["inode"] < 0 or source["size"] < 0 or not 0 <= source["nsec"] < 10**9:
        raise ValueError("invalid source stat range")
    if not _HASH.fullmatch(str(source.get("hash", ""))) or source.get("mode") not in {"stream", "netatalk"}:
        raise ValueError("invalid source fingerprint or decoder")
    return source_identity(source)


def validate_coverage(value: object, allowed_sources: set[str]) -> dict[str, list[list[str]]]:
    if not isinstance(value, dict) or set(value) != allowed_sources:
        raise ValueError("incomplete source coverage")
    total = 0
    for records in value.values():
        if not isinstance(records, list):
            raise ValueError("invalid key coverage")
        seen = set()
        for record in records:
            if (not isinstance(record, list) or len(record) != 2 or record[0] not in COVERAGE_KINDS
                    or not isinstance(record[1], str) or not _KEY.fullmatch(record[1]) or record[1] in seen):
                raise ValueError("invalid or duplicate covered key")
            seen.add(record[1])
        total += len(records)
    if total > MAX_KEYS:
        raise ValueError("too many covered keys")
    return value


def decode_receipt(raw: bytes) -> dict | None:
    try:
        if len(raw) > MAX_RECEIPT_BYTES:
            return None
        doc = json.loads(raw)
        if doc["version"] != VERSION or doc["policy"] != POLICY:
            return None
        sources = [validate_source(source) for source in doc["sources"]]
        ids = {source_id(source) for source in sources}
        if not sources or len(sources) > MAX_SOURCES or len(ids) != len(sources) or doc["cohort"] != cohort_hash(sources):
            return None
        if not isinstance(doc["completed"], dict) or len(doc["completed"]) > 64:
            return None
        for volume_uuid, entry in doc["completed"].items():
            if not _UUID.fullmatch(volume_uuid):
                return None
            validate_coverage(entry["coverage"], ids)
        return {**doc, "sources": sources}
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None


def compatible_receipts(sources: list[dict], receipts: list[dict], available_uuids: set[str] | None = None) -> tuple[list[dict], dict]:
    """Merge proofs only for one unchanged cohort, allowing known absent disks."""
    current = {source_id(s): source_identity(s) for s in sources}
    compatible = []
    for doc in receipts:
        recorded = {source_id(s): s for s in doc["sources"]}
        if available_uuids is not None and any(recorded[key]["uuid"] in available_uuids for key in recorded.keys() - current.keys()):
            continue  # A removed/retired source changes the cohort; rescan.
        if all(recorded.get(key) == value for key, value in current.items()):
            compatible.append(doc)
    # Inconsistent old cohorts prove nothing. A fresh scan is cheaper and safer
    # than inventing a distributed receipt agreement protocol.
    if not compatible or len({doc["cohort"] for doc in compatible}) != 1:
        return list(current.values()), {}
    cohort = compatible[0]["sources"]
    completed, conflicts = {}, set()
    for doc in compatible:
        for key, value in doc["completed"].items():
            if key in completed and completed[key] != value:
                conflicts.add(key)
            completed[key] = value
    for key in conflicts:
        completed.pop(key, None)
    return cohort, completed


def legacy_mode(text: str, fallback: str = "netatalk") -> str:
    """Read config as data; absent settings use v2.2.9's Netatalk default.

    Either mode still falls back to the other representation when absent.
    The mode only resolves files that carry both historical representations.
    """
    matches = re.findall(r"^\s*fruit:metadata\s*=\s*(stream|netatalk)\s*(?:[#;].*)?$", text, re.M | re.I)
    if matches:
        return matches[-1].lower()
    matches = re.findall(r"^\s*(?:TC_)?FRUIT_METADATA_NETATALK\s*=\s*['\"]?(0|1|true|false|yes|no)['\"]?\s*(?:#.*)?$", text, re.M | re.I)
    return ("netatalk" if matches[-1].lower() in {"1", "true", "yes"} else "stream") if matches else fallback


@dataclass(frozen=True)
class OversizedValue:
    """A legacy value HFS cannot hold natively, kept where it was."""
    kind: str  # "tdb" (xattr.tdb row) or "appledouble" (._ file)
    path: str
    name: str
    size: int
    reason: str = "size"  # see KEPT_REASONS


@dataclass
class OversizedSummary:
    tdb: int = 0
    appledouble: int = 0
    values: list[OversizedValue] = field(default_factory=list)
    # How many of the tdb + appledouble values are folder resource forks.
    folder_forks: int = 0
    # Cleanup only: "quarantined" once every database holding a kept value
    # was set aside, "in_place" while retirement is deferred.
    database_outcome: str | None = None

    @property
    def total(self) -> int:
        return self.tdb + self.appledouble


def decode_oversized(report: dict) -> OversizedSummary:
    block = report.get("oversized")
    if not isinstance(block, dict):
        raise ValueError("missing oversized values")
    tdb, appledouble, items = block.get("tdb"), block.get("appledouble"), block.get("items")
    folder_forks = block.get("folder_forks")
    if (type(tdb) is not int or type(appledouble) is not int or tdb < 0 or appledouble < 0
            or type(folder_forks) is not int or not 0 <= folder_forks <= tdb + appledouble
            or not isinstance(items, list) or len(items) > min(MAX_OVERSIZED_ITEMS, tdb + appledouble)):
        raise ValueError("invalid oversized values")
    values = []
    for item in items:
        reason = item.get("reason") if isinstance(item, dict) else None
        # A folder cannot hold a fork of any size; any other kept value is
        # one a native attribute could not hold.
        smallest = 1 if reason == "folder_fork" else NATIVE_XATTR_LIMIT + 1
        if (not isinstance(item, dict) or item.get("kind") not in OVERSIZED_KINDS or reason not in KEPT_REASONS
                or not _HEX.fullmatch(str(item.get("path_hex", ""))) or not _HEX.fullmatch(str(item.get("name_hex", "")))
                or type(item.get("size")) is not int or item["size"] < smallest):
            raise ValueError("invalid oversized value")
        # Display only, never used to open a file: a name inside a ._ file is
        # arbitrary bytes, and a strict UTF-8 terminal rejects surrogates.
        values.append(OversizedValue(item["kind"], bytes.fromhex(item["path_hex"]).decode("utf-8", "replace"),
                                     bytes.fromhex(item["name_hex"]).decode("utf-8", "replace"), item["size"],
                                     reason))
    return OversizedSummary(tdb, appledouble, values, folder_forks=folder_forks)


@dataclass
class MigrationInventory:
    volumes: tuple[MaStVolume, ...]
    candidates: list[dict]
    payload_dirs: list[str]
    unavailable: list[str]
    old_config: str
    sources: list[dict] = field(default_factory=list)
    cohort: list[dict] = field(default_factory=list)
    completed: dict = field(default_factory=dict)
    copied: set[str] = field(default_factory=set)
    output: list[str] = field(default_factory=list)
    # Per phase, for deploy to show the user and record in telemetry.
    oversized: dict[str, OversizedSummary] = field(default_factory=dict)
    # Verified rows a lone retained source dropped, and the copy made first.
    dropped_rows: int = 0
    backup: str | None = None


def _read(connection: SshConnection, path: str, *, limit: int = 65536) -> bytes:
    # dd is present on both firmware generations; head/stat/wc are not. The
    # caller bounds documents before parsing and distinguishes unreadable files.
    command = f"if [ -f {shlex.quote(path)} ]; then dd if={shlex.quote(path)} bs=1024 count={(limit // 1024) + 1} 2>/dev/null; else exit 0; fi"
    return run_ssh_input(connection, command).stdout


def inventory_metadata(connection: SshConnection, plan) -> MigrationInventory:
    volumes = tuple(read_mast_volumes_conn(connection))
    candidates, payload_dirs, unavailable = [], [], []
    old_config = _read(connection, "/mnt/Flash/tcapsulesmb.conf").decode("utf-8", "replace")
    # Recognize incomplete installs as well as v2.2.9 and earlier directory
    # layouts. No executable/version-marker prerequisite and no recursive walk.
    names = {PurePosixPath(plan.payload_dir).name, ".samba4", "samba4", "tc-netbsd4", "tc-netbsd4le", "tc-netbsd4be", "tc-netbsd7"}
    for raw in re.findall(r"^\s*(?:TC_)?PAYLOAD_DIR_NAME\s*=(.*)$", old_config, re.M):
        try:
            fields = shlex.split(raw, comments=True)
        except ValueError:
            continue
        if len(fields) == 1 and fields[0] not in {"", ".", ".."} and "/" not in fields[0]:
            names.add(fields[0])
    seen_uuids = set()
    for volume in volumes:
        if not ensure_volume_root_mounted_conn(connection, volume.volume_root, volume.device_path, wait_seconds=plan.apple_mount_wait_seconds):
            unavailable.append(volume.volume_root)
            continue
        volume_uuid = normalized_uuid(volume.adisk_uuid)
        if volume_uuid in seen_uuids:
            raise RuntimeError(f"Duplicate HFS volume UUID: {volume_uuid}")
        seen_uuids.add(volume_uuid)
        for name in sorted(names):
            directory = str(PurePosixPath(volume.volume_root) / name)
            probe = run_ssh(connection, f"test -d {shlex.quote(directory)}", check=False)
            if probe.returncode == 1:
                continue
            if probe.returncode:
                raise RuntimeError(f"Could not inspect legacy payload {directory}")
            payload_dirs.append(directory)
            for suffix in ("private/xattr.tdb", "var/locks/xattr.tdb"):
                path = directory + "/" + suffix
                probe = run_ssh(connection, f"test -f {shlex.quote(path)}", check=False)
                if probe.returncode == 1:
                    continue
                if probe.returncode:
                    raise RuntimeError(f"Could not inspect legacy metadata {path}")
                evidence = _read(connection, directory + "/smb.conf").decode("utf-8", "replace")
                if not evidence:
                    evidence = _read(connection, directory + "/smb.conf.template").decode("utf-8", "replace")
                candidates.append({"uuid": volume_uuid, "relative": name + "/" + suffix, "path": path,
                                   "mode": legacy_mode(evidence, legacy_mode(old_config))})
    if len(candidates) > MAX_SOURCES:
        raise RuntimeError("Too many legacy metadata databases")
    return MigrationInventory(volumes, candidates, payload_dirs, unavailable, old_config)


def _saved_log_snapshot(connection: SshConnection, log: str | None) -> str:
    if log is None:
        return "unavailable"
    try:
        tail = run_ssh(
            connection,
            f"/usr/bin/tail -c 8192 {shlex.quote(log)}",
            check=False,
            timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
        ).stdout
        return tail[-8192:] if tail else "unavailable"
    except Exception:
        return "unavailable"


def _native(connection: SshConnection, arguments: list[str], *, request: bytes = b"", log: str | None = None) -> dict:
    command = [RAM_HELPER, "--stall-seconds", str(STALL_SECONDS)]
    if log is not None:
        command.extend(["--log", log])
    command.extend(arguments)
    try:
        result = run_ssh_input(
            connection,
            "exec " + shlex.join(command),
            input_bytes=request,
            timeout=NATIVE_TIMEOUT_SECONDS,
            raw_remote_status=True,
            extra_ssh_args=NATIVE_SSH_ARGS,
        )
    except SshError as exc:
        tail = _saved_log_snapshot(connection, log)
        raise SshError(
            f"{exc}\nSaved migration log snapshot ({log}; may be incomplete):\n{tail}"
        ) from exc
    if result.returncode == 75:
        tail = _saved_log_snapshot(connection, log)
        raise MigrationStalledError(
            f"Native metadata migration made no progress for {STALL_SECONDS} seconds.\n"
            f"Saved migration log snapshot ({log}; may be incomplete):\n{tail}"
        )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        tail = _saved_log_snapshot(connection, log)
        raise RuntimeError(
            f"Native metadata migration failed with exit status {result.returncode}"
            f"{f': {detail}' if detail else ''}.\n"
            f"Saved migration log snapshot ({log}; may be incomplete):\n{tail}"
        )
    if len(result.stdout) > MAX_RECEIPT_BYTES:
        raise RuntimeError("Migration coverage exceeds the supported size")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Native metadata migration returned invalid JSON") from exc


def inspect_sources(connection: SshConnection, inventory: MigrationInventory) -> None:
    receipts, by_inode = [], {}
    for candidate in inventory.candidates:
        stat = _native(connection, ["inspect", candidate["path"]])
        canonical = os.fsdecode(bytes.fromhex(stat["path_hex"]))
        volume = next(v for v in inventory.volumes if normalized_uuid(v.adisk_uuid) == candidate["uuid"])
        if not canonical.startswith(volume.volume_root + "/"):
            raise RuntimeError(f"Legacy database alias crosses its identified volume: {candidate['path']}")
        source = {**candidate, **{key: stat[key] for key in ("dev", *STAT_FIELDS)}, "path": canonical,
                  "aliases": [] if canonical == candidate["path"] else [candidate["path"]]}
        validate_source(source)
        identity = (source["dev"], source["inode"])
        if identity in by_inode:
            other = by_inode[identity]
            paths = {source["path"], *source["aliases"], other["path"], *other["aliases"]}
            # The greater stable spelling is the source's deterministic rank.
            if (source["uuid"], os.fsencode(source["relative"])) > (other["uuid"], os.fsencode(other["relative"])):
                inventory.sources.remove(other)
            else:
                source = other
            source["aliases"] = sorted(paths - {source["path"]})
        if source not in inventory.sources:
            inventory.sources.append(source)
        by_inode[identity] = source
        doc = decode_receipt(_read(connection, candidate["path"] + RECEIPT_SUFFIX, limit=MAX_RECEIPT_BYTES))
        if doc:
            receipts.append(doc)
    for source in inventory.sources:
        modes = {saved["mode"] for doc in receipts for saved in doc["sources"]
                 if source_identity(saved, mode=False) == source_identity(source, mode=False)}
        if len(modes) > 1:
            raise RuntimeError(f"Conflicting saved metadata decoders for {source['path']}")
        if modes:
            source["mode"] = modes.pop()
    available = {normalized_uuid(v.adisk_uuid) for v in inventory.volumes if v.volume_root not in inventory.unavailable}
    inventory.cohort, inventory.completed = compatible_receipts(inventory.sources, receipts, available)
    # Save decoder evidence before software templates can be removed. An empty
    # completion map proves no volume finished, but an interrupted deployment
    # still knows how to interpret both historical FinderInfo representations.
    if inventory.sources:
        save_progress(connection, inventory)


def covered_keys(inventory: MigrationInventory, source: dict) -> dict[str, str]:
    """Every completed volume's saved coverage of one source, by key."""
    keys: dict[str, str] = {}
    for entry in inventory.completed.values():
        for kind, key in entry["coverage"][source_id(source)]:
            if key in keys and keys[key] != kind:
                raise RuntimeError("Inconsistent saved metadata key coverage")
            keys[key] = kind
    return keys


def claimed_devices(inventory: MigrationInventory) -> dict[int, str]:
    """The device number each completed volume's rows were matched under.

    A legacy key starts with the file's st_dev (8 bytes, little-endian), and
    Apple renumbers /Volumes/dkN across boots and USB attach order. HFS inode
    numbers repeat across volumes, so a volume that now has a completed
    volume's old number would match that volume's rows to its own files.
    Dropped rows leave the coverage with the database (forget_dropped_rows),
    so only rows the database still holds reserve a number.
    """
    return {int.from_bytes(bytes.fromhex(key)[:8], "little"): volume_uuid
            for volume_uuid, entry in inventory.completed.items()
            for records in entry["coverage"].values() for _kind, key in records}


def drops_verified_rows(inventory: MigrationInventory) -> bool:
    """A deferred retirement may drop verified rows only from a lone source.

    Several sources rank by their unchanged mtimes, and a cohort source on an
    absent disk may still outrank this one.
    """
    return len(inventory.sources) == 1 and len(inventory.cohort) == 1


def request_bytes(inventory: MigrationInventory, root: tuple[MaStVolume, dict] | None = None,
                  *, drop_verified: bool = False) -> bytes:
    if root and drop_verified:
        raise ValueError("only retirement drops verified rows")
    lines = ["TCMIGRATE1"]
    for index, source in enumerate(inventory.sources):
        lines.append(" ".join(map(str, ("S", index, source["uuid"], os.fsencode(source["relative"]).hex(),
            os.fsencode(source["path"]).hex(), source["mode"], source["dev"], source["inode"], source["size"],
            source["mtime"], source["nsec"], source["hash"]))))
        lines.extend(f"A {index} {os.fsencode(path).hex()}" for path in source["aliases"])
    if drop_verified:
        # Before any K line: the helper opens the source read-write for it.
        lines.append("D")
    if root:
        volume, stat = root
        lines.append(f"R {normalized_uuid(volume.adisk_uuid)} {os.fsencode(volume.volume_root).hex()} {stat['dev']} {stat['inode']}")
    else:
        for index, source in enumerate(inventory.sources):
            keys = covered_keys(inventory, source)
            lines.extend(f"K {index} {kind} {key}" for key, kind in sorted(keys.items()))
    lines.append("E")
    return ("\n".join(lines) + "\n").encode("ascii")


def save_progress(connection: SshConnection, inventory: MigrationInventory) -> None:
    doc = {"version": VERSION, "policy": POLICY, "cohort": cohort_hash(inventory.cohort),
           "sources": inventory.cohort, "completed": inventory.completed}
    data = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_RECEIPT_BYTES or decode_receipt(data) is None:
        raise RuntimeError("Invalid migration completion document")
    for source in inventory.sources:
        # A truncated update cannot become a valid receipt, but losing a
        # receipt is not free: the next deploy walks every volume again and
        # rewrites native values from rows still in the source. Only a lone
        # source drops its verified rows (drops_verified_rows); several keep
        # them until retirement. Flush before recording success in the log.
        for path in [source["path"], *source["aliases"]]:
            run_ssh_input(connection, f"umask 077; cat > {shlex.quote(path + RECEIPT_SUFFIX)} && /bin/sync", input_bytes=data)


def validate_native_report(report: object, inventory: MigrationInventory,
                           *, verified_rows: int | None = None) -> dict[str, list[list[str]]]:
    """Check the helper's JSON. verified_rows is the M keys a D request sent:
    a lone source not retired must have dropped exactly those, and nothing
    else may drop a row."""
    if (
        not isinstance(report, dict)
        or type(report.get("version")) is not int
        or report["version"] != VERSION
    ):
        raise RuntimeError("Invalid migration response")
    entries = report.get("entries")
    results = report.get("sources")
    if type(entries) is not int or entries < 0 or not isinstance(results, list) or len(results) != len(inventory.sources):
        raise RuntimeError("Invalid migration response")
    coverage = {source_id(source): [] for source in inventory.cohort}
    active_coverage = {}
    for index, result in enumerate(results):
        if not isinstance(result, dict) or result.get("index") != index:
            raise RuntimeError("Invalid migration source response")
        total = result.get("total")
        retired = result.get("retired")
        deleted = result.get("deleted")
        records = result.get("coverage")
        if (
            type(total) is not int
            or total < 0
            or type(retired) is not int
            or retired not in {0, 1, 2}
            or type(deleted) is not int
            or deleted != (verified_rows if verified_rows is not None and not retired else 0)
            or not isinstance(records, list)
            or len(records) > total
        ):
            raise RuntimeError("Invalid migration source response")
        active_coverage[source_id(inventory.sources[index])] = records
    backup = report.get("backup_hex")
    if (backup is not None) != any(result["deleted"] for result in results) or (
            backup is not None and not _HEX.fullmatch(str(backup))):
        raise RuntimeError("Invalid migration source response")
    try:
        validate_coverage(active_coverage, {source_id(source) for source in inventory.sources})
        coverage.update(active_coverage)
        validate_coverage(coverage, {source_id(source) for source in inventory.cohort})
        decode_oversized(report)
    except ValueError as exc:
        raise RuntimeError("Invalid migration source response") from exc
    return coverage


def forget_dropped_rows(connection: SshConnection, inventory: MigrationInventory, dropped: int, backup: str) -> None:
    """Record that the lone source no longer holds its verified rows.

    Its fingerprint changed, so its receipt is saved again with the new one
    and without the dropped keys, which the next retire request must not name.
    A deploy that stops first finds a stale receipt and only walks again.
    """
    source = inventory.sources[0]
    key = source_id(source)
    stat = _native(connection, ["inspect", source["path"]])
    if os.fsdecode(bytes.fromhex(stat["path_hex"])) != source["path"]:
        raise RuntimeError(f"Legacy database moved while dropping verified rows: {source['path']}")
    for entry in inventory.completed.values():
        entry["coverage"][key] = [record for record in entry["coverage"][key] if record[0] != "M"]
    source.update({name: stat[name] for name in ("dev", *STAT_FIELDS)})
    validate_source(source)
    inventory.cohort = [source_identity(source)]
    save_progress(connection, inventory)
    inventory.dropped_rows += dropped
    inventory.backup = backup
    inventory.output.append(f"dropped_verified_rows source={source['relative']} uuid={source['uuid']} "
                            f"count={dropped} backup={backup}")


def migrate_phase(connection: SshConnection, plan, inventory: MigrationInventory, phase: str) -> str:
    if phase not in {"copy", "cleanup"}:
        raise ValueError("unsupported migration phase")
    if not inventory.sources:
        # AppleDouble ._ files are converted only alongside a legacy xattr.tdb:
        # every release that wrote them (b18c5901 to 2629eaec) also wrote one,
        # and skipping the whole-disk walk keeps deploys fast. On HFS shares
        # fruit ignores ._ files (patch 0055), so a volume whose legacy payload
        # was removed, e.g. by uninstall, keeps its ._ files unconverted.
        return f"migration_phase={phase} skipped reason=no_legacy_tdb"
    log = f"{plan.payload_dir}/logs/xattr-migration-{phase}.log"
    oversized = inventory.oversized.setdefault(phase, OversizedSummary())
    run_ssh(connection, f"mkdir -p {shlex.quote(str(PurePosixPath(log).parent))} && printf '%s\\n' {shlex.quote(f'phase={phase} sources={len(inventory.sources)} stall_seconds={STALL_SECONDS}')} > {shlex.quote(log)} && /bin/date -u '+started_at=%Y-%m-%dT%H:%M:%SZ' >> {shlex.quote(log)}")
    current = read_mast_volumes_conn(connection)
    available = {normalized_uuid(v.adisk_uuid): v for v in current}
    if len(available) != len(current):
        raise RuntimeError("Duplicate HFS volume UUID during migration")
    for original in inventory.volumes:
        key = normalized_uuid(original.adisk_uuid)
        if key in inventory.completed or (phase == "cleanup" and key not in inventory.copied):
            continue
        volume = available.get(key)
        if volume is None or not ensure_volume_root_mounted_conn(connection, volume.volume_root, volume.device_path, wait_seconds=plan.apple_mount_wait_seconds):
            inventory.output.append(f"phase={phase} uuid={key} unavailable")
            continue
        stat = _native(connection, ["inspect-root", volume.volume_root], log=log)
        owner = claimed_devices(inventory).get(stat["dev"])
        if owner is not None:
            # A renumbered disk waits for its number to be free; matching its
            # rows under the old number would need a stored device map.
            inventory.output.append(f"phase={phase} uuid={key} deferred reason=device_renumbered owner={owner}")
            continue
        report = _native(connection, ["multi", phase], request=request_bytes(inventory, (volume, stat)), log=log)
        coverage = validate_native_report(report, inventory)
        kept = decode_oversized(report)
        oversized.tdb += kept.tdb
        oversized.appledouble += kept.appledouble
        oversized.folder_forks += kept.folder_forks
        oversized.values.extend(kept.values[:MAX_OVERSIZED_ITEMS - len(oversized.values)])
        inventory.output.append(f"phase={phase} uuid={key} entries={report['entries']} "
                                f"oversized_tdb={kept.tdb} oversized_appledouble={kept.appledouble} "
                                f"folder_forks={kept.folder_forks} complete")
        if phase == "copy":
            inventory.copied.add(key)
        elif {source_id(s) for s in inventory.sources} == {source_id(s) for s in inventory.cohort}:
            inventory.completed[key] = {"coverage": coverage, "completed_at": int(time.time()), "entries": report["entries"]}
            save_progress(connection, inventory)
    if phase == "cleanup":
        if oversized.tdb:
            oversized.database_outcome = "in_place"
        # A known but currently absent DB may still outrank or contribute unique
        # values. Preserve the cohort's active sources until it returns.
        if {source_id(s) for s in inventory.sources} != {source_id(s) for s in inventory.cohort}:
            inventory.output.append("retirement deferred reason=source_volume_absent")
        else:
            drop = drops_verified_rows(inventory)
            verified = (sum(kind == "M" for kind in covered_keys(inventory, inventory.sources[0]).values())
                        if drop else None)
            report = _native(connection, ["multi", "retire"], request=request_bytes(inventory, drop_verified=drop), log=log)
            validate_native_report(report, inventory, verified_rows=verified)
            if drop and report["sources"][0]["deleted"]:
                forget_dropped_rows(connection, inventory, report["sources"][0]["deleted"],
                                    os.fsdecode(bytes.fromhex(report["backup_hex"])))
            for index, result in enumerate(report["sources"]):
                if result["retired"]:
                    source = inventory.sources[index]
                    paths = [source["path"], *source["aliases"]]
                    run_ssh(connection, "rm -f " + shlex.join([path + RECEIPT_SUFFIX for path in paths]) + " && /bin/sync")
                    kinds = [kind for entry in inventory.completed.values()
                             for kind, _key in entry["coverage"].get(source_id(source), [])]
                    outcome = "quarantined" if result["retired"] == 2 else "deleted"
                    inventory.output.append(f"retired source={source['relative']} uuid={source['uuid']} outcome={outcome} "
                                            f"orphaned={kinds.count('O')} oversized={kinds.count('X')}")
            inventory.output.append("retirement complete" if all(r["retired"] for r in report["sources"]) else "retirement partial unresolved_metadata_retained")
            holders = [index for index, source in enumerate(inventory.sources)
                       if any(kind == "X" for entry in inventory.completed.values()
                              for kind, _key in entry["coverage"].get(source_id(source), []))]
            if oversized.tdb and holders and all(report["sources"][index]["retired"] == 2 for index in holders):
                oversized.database_outcome = "quarantined"
    return "\n".join(inventory.output)
