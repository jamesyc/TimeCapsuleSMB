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
STAT_FIELDS = ("inode", "size", "mtime", "nsec", "hash")


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
            if (not isinstance(record, list) or len(record) != 2 or record[0] not in {"M", "O"}
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


def request_bytes(inventory: MigrationInventory, root: tuple[MaStVolume, dict] | None = None) -> bytes:
    lines = ["TCMIGRATE1"]
    for index, source in enumerate(inventory.sources):
        lines.append(" ".join(map(str, ("S", index, source["uuid"], os.fsencode(source["relative"]).hex(),
            os.fsencode(source["path"]).hex(), source["mode"], source["dev"], source["inode"], source["size"],
            source["mtime"], source["nsec"], source["hash"]))))
        lines.extend(f"A {index} {os.fsencode(path).hex()}" for path in source["aliases"])
    if root:
        volume, stat = root
        lines.append(f"R {normalized_uuid(volume.adisk_uuid)} {os.fsencode(volume.volume_root).hex()} {stat['dev']} {stat['inode']}")
    else:
        for index, source in enumerate(inventory.sources):
            keys = {}
            for entry in inventory.completed.values():
                for kind, key in entry["coverage"][source_id(source)]:
                    if key in keys and keys[key] != kind:
                        raise RuntimeError("Inconsistent saved metadata key coverage")
                    keys[key] = kind
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
        # A truncated update only loses the optimization: it cannot become a
        # valid receipt. Flush before recording success in the deploy log.
        for path in [source["path"], *source["aliases"]]:
            run_ssh_input(connection, f"umask 077; cat > {shlex.quote(path + RECEIPT_SUFFIX)} && /bin/sync", input_bytes=data)


def validate_native_report(report: object, inventory: MigrationInventory) -> dict[str, list[list[str]]]:
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
        records = result.get("coverage")
        if (
            type(total) is not int
            or total < 0
            or type(retired) is not int
            or retired not in {0, 1, 2}
            or not isinstance(records, list)
            or len(records) > total
        ):
            raise RuntimeError("Invalid migration source response")
        active_coverage[source_id(inventory.sources[index])] = records
    try:
        validate_coverage(active_coverage, {source_id(source) for source in inventory.sources})
        coverage.update(active_coverage)
        validate_coverage(coverage, {source_id(source) for source in inventory.cohort})
    except ValueError as exc:
        raise RuntimeError("Invalid migration source response") from exc
    return coverage


def migrate_phase(connection: SshConnection, plan, inventory: MigrationInventory, phase: str) -> str:
    if phase not in {"copy", "cleanup"}:
        raise ValueError("unsupported migration phase")
    if not inventory.sources:
        return f"migration_phase={phase} skipped reason=no_legacy_tdb"
    log = f"{plan.payload_dir}/logs/xattr-migration-{phase}.log"
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
        report = _native(connection, ["multi", phase], request=request_bytes(inventory, (volume, stat)), log=log)
        coverage = validate_native_report(report, inventory)
        inventory.output.append(f"phase={phase} uuid={key} entries={report['entries']} complete")
        if phase == "copy":
            inventory.copied.add(key)
        elif {source_id(s) for s in inventory.sources} == {source_id(s) for s in inventory.cohort}:
            inventory.completed[key] = {"coverage": coverage, "completed_at": int(time.time()), "entries": report["entries"]}
            save_progress(connection, inventory)
    if phase == "cleanup":
        # A known but currently absent DB may still outrank or contribute unique
        # values. Preserve the cohort's active sources until it returns.
        if {source_id(s) for s in inventory.sources} != {source_id(s) for s in inventory.cohort}:
            inventory.output.append("retirement deferred reason=source_volume_absent")
        else:
            report = _native(connection, ["multi", "retire"], request=request_bytes(inventory), log=log)
            validate_native_report(report, inventory)
            for index, result in enumerate(report["sources"]):
                if result["retired"]:
                    source = inventory.sources[index]
                    paths = [source["path"], *source["aliases"]]
                    run_ssh(connection, "rm -f " + shlex.join([path + RECEIPT_SUFFIX for path in paths]) + " && /bin/sync")
            inventory.output.append("retirement complete" if all(r["retired"] for r in report["sources"]) else "retirement partial unresolved_metadata_retained")
    return "\n".join(inventory.output)
