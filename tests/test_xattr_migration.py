"""Deploy orchestration tests. Native value/extent conversion also runs on NetBSD.

Apple owns the HFS mounts; failed or missing mounts are partial migration, never
proof that their TDB records are orphans. Completed volumes must not be walked.
"""
import copy
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from timecapsulesmb.deploy import migration as m
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.transport.ssh import SshConnection

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
KEY_A = "01000000000000000100000000000000"
KEY_B = "02000000000000000100000000000000"
# A disk the legacy database has rows for but that is not attached (not in MaSt).
UUID_C = "33333333-3333-3333-3333-333333333333"
KEY_C = "03000000000000000100000000000000"
NO_OVERSIZED = {"tdb": 0, "appledouble": 0, "folder_forks": 0, "items": []}
CONTAINER = "/Volumes/dk2/Users/me/Library/Containers/com.example.app"
PERSONALITY = "com.apple.data-container-personality"


def oversized_item(path=CONTAINER, name=PERSONALITY, size=12979, kind="tdb", reason="size"):
    return {"kind": kind, "reason": reason, "path_hex": os.fsencode(path).hex(),
            "name_hex": os.fsencode(name).hex(), "size": size}


def fake_inventory():
    """Installer tests inject discovery, while exercising the real action order."""
    return m.MigrationInventory((), [{"path": "/Volumes/dk2/.samba4/private/xattr.tdb"}], [], [], "")


def volume(root, name, uuid):
    return MaStVolume("sd0", name, str(root), name, uuid, True, "hfs")


@pytest.fixture
def device(tmp_path, monkeypatch):
    volumes = [volume(tmp_path / "disk A", "dk2", UUID_A), volume(tmp_path / "disk B", "dk3", UUID_B)]
    source = Path(volumes[0].volume_root) / ".samba4/private/xattr.tdb"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"legacy metadata")
    Path(volumes[1].volume_root).mkdir()
    config = tmp_path / "old-config"
    config.write_text("FRUIT_METADATA_NETATALK=1\n")
    # rows: the attached volumes each source (uuid, relative) still holds rows
    # for. absent: every source also holds a row of disk C, which is never
    # attached, so retirement waits, as the helper decides it.
    # fail_save_after_drop: receipt paths whose writes fail once rows were
    # dropped. moved: after a drop, inspect finds the database under another
    # canonical path.
    state = SimpleNamespace(volumes=volumes, source=source, calls=[], mounted={UUID_A, UUID_B}, fail=None,
                            reads=[], native={UUID_A: "old", UUID_B: "old"},
                            kind={UUID_A: "M", UUID_B: "M"}, oversized={}, rows={}, absent=True,
                            dropped=False, fail_save_after_drop=set(), moved=False)
    keys = {UUID_A: KEY_A, UUID_B: KEY_B, UUID_C: KEY_C}
    monkeypatch.setattr(m, "read_mast_volumes_conn", lambda _conn: state.volumes)
    monkeypatch.setattr(m, "ensure_volume_root_mounted_conn", lambda _c, root, *_a, **_k: any(v.volume_root == root and v.adisk_uuid in state.mounted for v in state.volumes))

    def read(_conn, path, **_kwargs):
        state.reads.append(path)
        path = config if path == "/mnt/Flash/tcapsulesmb.conf" else Path(path)
        return path.read_bytes() if path.is_file() else b""

    def ssh(_conn, command, *, input_bytes=b"", check=True, **_kwargs):
        if command.startswith("umask") and (state.fail == "save" or (state.fail == "save_after_drop" and state.dropped)
                                            or (state.dropped and any(shlex.quote(path) in command
                                                                      for path in state.fail_save_after_drop))):
            raise RuntimeError("flush failed")
        return subprocess.run(command.replace("/bin/sync", "true"), shell=True, executable="/bin/sh",
                              input=input_bytes, capture_output=True, check=check)

    def native(_conn, args, *, request=b"", **kwargs):
        state.calls.append((args, request, kwargs))
        if args[0] in {"inspect", "inspect-root"}:
            path = Path(args[1]); st = path.stat()
            canonical = path.resolve()
            if args[0] == "inspect" and state.moved and state.dropped:
                canonical = canonical.with_name(canonical.name + ".moved")
            return {"path_hex": os.fsencode(canonical).hex(), "dev": st.st_dev, "inode": st.st_ino,
                    "size": st.st_size, "mtime": st.st_mtime_ns // 10**9, "nsec": st.st_mtime_ns % 10**9,
                    "hash": hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else "0" * 16}
        lines = request.decode().splitlines()
        roots = [line.split() for line in lines if line.startswith("R ")]
        sources = [line.split() for line in lines if line.startswith("S ")]
        rows = [state.rows.setdefault((words[2], words[3]), {UUID_A, UUID_B}) for words in sources]
        phase = args[1]
        if state.fail == phase or (roots and state.fail == roots[0][1]):
            raise RuntimeError("injected migration failure")
        if phase == "retire":
            covered = [[] for _ in sources]
            for words in (line.split() for line in lines if line.startswith("K ")):
                volume_uuid = next(uuid for uuid, key in keys.items() if key == words[3])
                if volume_uuid not in rows[int(words[1])]:
                    raise RuntimeError("K names a row the database does not hold")
                covered[int(words[1])].append((words[2], volume_uuid))
            # The helper's whole-cohort preflight: any unnamed row anywhere defers.
            deferred = state.absent or any(
                held - {uuid for _kind, uuid in named} for held, named in zip(rows, covered))
            report = {"version": 1, "entries": 0, "oversized": NO_OVERSIZED, "sources": [
                {"index": i, "total": 2, "coverage": [], "retired": 0, "deleted": 0} for i in range(len(sources))]}
            if not deferred:
                # Aliases first, then the file: quarantined whole when an orphaned
                # or kept row remains, deleted otherwise.
                aliases = [line.split() for line in lines if line.startswith("A ")]
                for index, words in enumerate(sources):
                    path = Path(os.fsdecode(bytes.fromhex(words[4])))
                    quarantine = any(kind in {"O", "X"} for kind, _uuid in covered[index])
                    for alias in aliases:
                        if int(alias[1]) == index:
                            Path(os.fsdecode(bytes.fromhex(alias[2]))).unlink()
                    if quarantine:
                        path.rename(next(slot for slot in (Path(f"{path}.orphaned.{n}") for n in range(1, 100))
                                         if not slot.exists()))
                    else:
                        path.unlink()
                    report["sources"][index]["retired"] = 2 if quarantine else 1
            if "D" in lines and deferred:
                verified = {uuid for kind, uuid in covered[0] if kind == "M"}
                if verified:
                    path = Path(os.fsdecode(bytes.fromhex(sources[0][4])))
                    slot = next(Path(f"{path}.orphaned.{n}") for n in range(1, 100) if not Path(f"{path}.orphaned.{n}").exists())
                    slot.write_bytes(path.read_bytes())
                    path.write_bytes(path.read_bytes() + b" dropped")
                    rows[0] -= verified
                    state.dropped = True
                    report["sources"][0]["deleted"] = len(verified)
                    report["backup_hex"] = os.fsencode(slot).hex()
            return report
        key = roots[0][1]
        if phase == "copy" and any(key in held for held in rows):
            state.native[key] = "migrated"
        return {"version": 1, "entries": 3, "oversized": state.oversized.get(key, NO_OVERSIZED), "sources": [
            {"index": i, "total": 2, "retired": 0, "deleted": 0,
             "coverage": [[state.kind[key], keys[key]]] if phase == "cleanup" and key in rows[i] else []}
            for i in range(len(sources))]}

    monkeypatch.setattr(m, "_read", read)
    monkeypatch.setattr(m, "run_ssh", ssh)
    monkeypatch.setattr(m, "run_ssh_input", ssh)
    monkeypatch.setattr(m, "_native", native)
    state.connection = SshConnection("test", "", "")
    state.plan = SimpleNamespace(payload_dir=str(source.parent.parent), apple_mount_wait_seconds=1)
    state.inventory = lambda: m.inventory_metadata(state.connection, state.plan)
    state.inspect = lambda inv: m.inspect_sources(state.connection, inv)
    state.phase = lambda inv, phase: m.migrate_phase(state.connection, state.plan, inv, phase)
    state.scan_calls = lambda: [call for call in state.calls if call[0] in [["multi", "copy"], ["multi", "cleanup"]]]
    return state


def test_detects_incomplete_payload_and_old_decoder_without_an_executable(device):
    inv = device.inventory()
    assert inv.candidates == [{"uuid": UUID_A, "relative": ".samba4/private/xattr.tdb", "path": str(device.source), "mode": "netatalk"}]
    device.inspect(inv)
    assert inv.sources[0]["mode"] == "netatalk"


def test_no_tdb_never_invokes_native_scanner(device):
    device.source.unlink()
    inv = device.inventory()
    assert not inv.candidates
    assert "no_legacy_tdb" in device.phase(inv, "copy")
    assert not device.calls


def test_completed_disk_survives_absent_disk_and_native_edits(device):
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy"); device.phase(inv, "cleanup")
    receipt = Path(str(device.source) + m.RECEIPT_SUFFIX)
    assert m.decode_receipt(receipt.read_bytes())["completed"].keys() == {UUID_A}
    device.native[UUID_A] = "new native edit"
    device.mounted.add(UUID_B)
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert device.native[UUID_A] == "new native edit"
    assert len(device.scan_calls()) == 2
    assert all(UUID_B.encode() in request for _, request, _ in device.scan_calls())
    assert inv.completed.keys() == {UUID_A, UUID_B}


def test_unresolved_multi_source_cohort_preserves_completed_native_edits(device):
    second = Path(device.volumes[0].volume_root) / "tc-netbsd7/private/xattr.tdb"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"newer legacy metadata")
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert len(inv.sources) == 2
    assert all(Path(source["path"] + m.RECEIPT_SUFFIX).is_file() for source in inv.sources)

    device.native[UUID_A] = "new native edit"
    device.mounted.add(UUID_B)
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert device.native[UUID_A] == "new native edit"
    assert all(f"R {UUID_A} ".encode() not in request for _, request, _ in device.scan_calls())
    assert inv.completed.keys() == {UUID_A, UUID_B}


@pytest.mark.parametrize("failure", ["copy", "cleanup", "save"])
def test_failure_never_saves_unverified_volume(device, failure):
    inv = device.inventory(); device.inspect(inv)
    if failure != "copy":
        device.phase(inv, "copy")
    device.fail = failure
    with pytest.raises(RuntimeError):
        device.phase(inv, "copy" if failure == "copy" else "cleanup")
    saved = m.decode_receipt(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())
    assert saved is not None and not saved["completed"]
    assert device.source.read_bytes() == b"legacy metadata"


def test_first_completed_volume_is_saved_before_next_volume_fails(device):
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    device.fail = UUID_B
    with pytest.raises(RuntimeError):
        device.phase(inv, "cleanup")
    saved = m.decode_receipt(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())
    assert saved["completed"].keys() == {UUID_A}


@pytest.mark.parametrize("change", ["corrupt", "missing", "same_size", "new_source", "new_uuid", "format"])
def test_invalid_completion_rescans(device, change):
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    receipt = Path(str(device.source) + m.RECEIPT_SUFFIX)
    if change == "corrupt": receipt.write_text('{"version":1,')
    elif change == "missing": receipt.unlink()
    elif change == "same_size": device.source.write_bytes(b"changed content")
    elif change == "new_source":
        other = Path(device.volumes[1].volume_root) / ".samba4/private/xattr.tdb"
        other.parent.mkdir(parents=True); other.write_bytes(b"new source")
    elif change == "new_uuid":
        device.volumes[0] = volume(Path(device.volumes[0].volume_root), "dk2", "33333333-3333-3333-3333-333333333333")
        device.mounted.add(device.volumes[0].adisk_uuid)
    else:
        doc = json.loads(receipt.read_bytes()); doc["version"] += 1; receipt.write_text(json.dumps(doc))
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    assert device.scan_calls()


def test_disk_number_reordering_keeps_completion_and_decoder(device):
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    # Apple's dk number and mount pathname are not the volume identity.
    old = Path(device.volumes[0].volume_root); renamed = old.parent / "renumbered dk9"
    old.rename(renamed)
    device.volumes[0] = volume(renamed, "dk9", UUID_A)
    device.source = renamed / ".samba4/private/xattr.tdb"
    device.plan.payload_dir = str(device.source.parent.parent)
    device.calls.clear()
    inv = device.inventory()
    inv.candidates[0]["mode"] = "stream"  # new runtime preference must not reinterpret the old TDB
    device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert inv.sources[0]["mode"] == "netatalk"
    assert not device.scan_calls()


@pytest.mark.parametrize("uuid", ["", UUID_A])
def test_missing_or_duplicate_uuid_never_skips(device, uuid):
    device.volumes[1] = volume(Path(device.volumes[1].volume_root), "dk3", uuid)
    device.mounted.add(uuid)
    with pytest.raises(RuntimeError, match="UUID"):
        device.inventory()


def test_all_sources_are_sent_in_one_walk_per_volume_per_phase(device):
    second = Path(device.volumes[1].volume_root) / ".samba4/private/xattr.tdb"
    second.parent.mkdir(parents=True); second.write_bytes(b"other database")
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert len(device.scan_calls()) == 4
    assert all(request.count(b"\nS ") == 2 for _, request, _ in device.scan_calls())
    for source in inv.sources:
        saved = m.decode_receipt(Path(source["path"] + m.RECEIPT_SUFFIX).read_bytes())
        assert all(len(entry["coverage"]) == 2 for entry in saved["completed"].values())


def test_known_absent_source_preserves_existing_completion_but_cannot_prove_new_volumes(device):
    second = Path(device.volumes[1].volume_root) / ".samba4/private/xattr.tdb"
    second.parent.mkdir(parents=True); second.write_bytes(b"other database")
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    # Save A only, then its peer source disk disappears on the next deployment.
    device.fail = UUID_B
    with pytest.raises(RuntimeError): device.phase(inv, "cleanup")
    device.fail = None; device.volumes = device.volumes[:1]; device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert UUID_A in inv.completed and not device.scan_calls()
    assert "source_volume_absent" in inv.output[-1]


@pytest.mark.parametrize("text, expected", [("FRUIT_METADATA_NETATALK=1", "netatalk"), ("fruit:metadata = stream", "stream"),
    ("FRUIT_METADATA_NETATALK=$(touch /tmp/do-not-run)", "netatalk"), ("FRUIT_METADATA_NETATALK='false' # old config", "stream")])
def test_legacy_config_is_only_parsed_as_data(text, expected):
    assert m.legacy_mode(text) == expected


def test_native_command_uses_direct_guarded_single_attempt(monkeypatch):
    captured = []
    def run(_conn, command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b'{"version":1}', b"")
    monkeypatch.setattr(m, "run_ssh_input", run)
    assert m._native(
        SshConnection("test", "", ""),
        ["multi", "copy"],
        request=b"request",
        log="/disk/log",
    ) == {"version": 1}
    assert captured == [(
        "exec /mnt/Memory/tc-xattr-hfs-migrate --stall-seconds 300 --log /disk/log multi copy",
        {
            "input_bytes": b"request",
            "timeout": None,
            "raw_remote_status": True,
            "extra_ssh_args": m.NATIVE_SSH_ARGS,
        },
    )]


def test_native_stall_reports_saved_log_with_bounded_diagnostic_read(monkeypatch):
    monkeypatch.setattr(
        m,
        "run_ssh_input",
        lambda *_a, **_k: subprocess.CompletedProcess([], 75, b"", b""),
    )
    reads = []
    def read_log(*_args, **kwargs):
        reads.append(kwargs)
        return SimpleNamespace(stdout="last progress entries=10000")
    monkeypatch.setattr(m, "run_ssh", read_log)
    with pytest.raises(m.MigrationStalledError, match="last progress entries=10000"):
        m._native(SshConnection("test", "", ""), ["multi", "copy"], log="/disk/log")
    assert reads == [{"check": False, "timeout": 30}]


def test_native_call_survives_a_rejected_login(monkeypatch):
    """sshpass on macOS sometimes sends ssh an empty password; the migrator
    never started, so its request is sent again (v3.1.x deploys failed)."""
    from timecapsulesmb.transport import ssh as transport
    attempts = []

    def run(command, **kwargs):
        if "-E" not in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        attempts.append(kwargs.get("input"))
        log = Path(command[command.index("-E") + 1])
        if len(attempts) == 1:
            log.write_text("root@device: Permission denied (publickey,password,keyboard-interactive).\n")
            return subprocess.CompletedProcess(command, 5, b"", b"")
        log.write_text('Authenticated to device ([192.0.2.1]:22) using "password".\n')
        return subprocess.CompletedProcess(command, 0, b'{"version":1,"entries":0}', b"")

    monkeypatch.setattr(transport, "find_command", lambda _name: "/usr/bin/sshpass")
    monkeypatch.setattr(transport, "_ssh_option_supported", lambda _name: True)
    monkeypatch.setattr(transport, "_local_ssh_macs", lambda: ())
    monkeypatch.setattr(transport.subprocess, "run", run)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    report = m._native(SshConnection("root@device", "pw", ""), ["multi", "cleanup"], request=b"TCMIGRATE1\nE\n")
    assert report == {"version": 1, "entries": 0}
    assert attempts == [b"TCMIGRATE1\nE\n", b"TCMIGRATE1\nE\n"]


@pytest.mark.parametrize(
    "status, stdout, message",
    [
        (4, b'{"version":1}', "exit status 4"),
        (0, b"not json", "invalid JSON"),
    ],
)
def test_native_requires_zero_status_and_valid_json(monkeypatch, status, stdout, message):
    monkeypatch.setattr(
        m,
        "run_ssh_input",
        lambda *_a, **_k: subprocess.CompletedProcess([], status, stdout, b"native diagnostic"),
    )
    with pytest.raises(RuntimeError, match=message):
        m._native(SshConnection("test", "", ""), ["inspect", "/disk/tdb"])


KEY_X = "04000000000000000100000000000000"


def saved_receipt(device) -> dict:
    """A receipt deploy's own writer saved, recording one of each coverage kind.

    Built without a retire step, so its format does not depend on whether
    retirement dropped any rows."""
    inv = device.inventory(); device.inspect(inv)
    inv.completed[UUID_A] = {"coverage": {m.source_id(inv.sources[0]): [["M", KEY_A], ["O", KEY_B], ["X", KEY_X]]},
                             "completed_at": 1, "entries": 3}
    m.save_progress(device.connection, inv)
    return json.loads(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())


def records_of(doc: dict) -> list:
    return next(iter(doc["completed"][UUID_A]["coverage"].values()))


@pytest.mark.parametrize("change", ["repeated_key", "key_repeated_as_another_kind", "unknown_source"])
def test_rejects_unknown_duplicate_and_excess_key_coverage(device, change):
    doc = saved_receipt(device)
    assert m.decode_receipt(json.dumps(doc).encode()) is not None
    bad = copy.deepcopy(doc)
    if change == "repeated_key": records_of(bad).append(["M", KEY_A])
    elif change == "key_repeated_as_another_kind": records_of(bad).append(["O", KEY_A])
    else: bad["completed"][UUID_A]["coverage"]["unknown"] = []
    assert m.decode_receipt(json.dumps(bad).encode()) is None


@pytest.mark.parametrize("change", ["version", "entries", "count", "index", "total", "retired", "coverage",
                                    "coverage_kind", "oversized_missing", "oversized_negative", "oversized_type",
                                    "oversized_more_items_than_counted", "oversized_over_limit", "oversized_path_hex",
                                    "oversized_name_hex", "oversized_kind", "oversized_fits_natively",
                                    "oversized_reason", "oversized_folder_forks_missing",
                                    "oversized_folder_forks_over_count", "oversized_empty_folder_fork",
                                    "deleted_missing", "deleted_type", "deleted_negative", "deleted_without_d",
                                    "backup_without_deleted"])
def test_native_report_validation_rejects_status_and_schema_mismatches(device, change):
    inv = device.inventory(); device.inspect(inv)
    report = {
        "version": 1,
        "entries": 1,
        "oversized": {"tdb": 1, "appledouble": 0, "folder_forks": 0, "items": [oversized_item()]},
        "sources": [{"index": 0, "total": 1, "retired": 0, "deleted": 0, "coverage": [["M", KEY_A]]}],
    }
    m.validate_native_report(copy.deepcopy(report), inv)
    item = report["oversized"]["items"][0]
    if change == "version": report["version"] = True
    elif change == "entries": report["entries"] = True
    elif change == "count": report["sources"] = []
    elif change == "index": report["sources"][0]["index"] = 1
    elif change == "total": report["sources"][0]["total"] = 0
    elif change == "retired": report["sources"][0]["retired"] = 3
    elif change == "coverage": report["sources"][0]["coverage"] = [["M", "bad"]]
    elif change == "coverage_kind": report["sources"][0]["coverage"] = [["Z", KEY_A]]
    elif change == "oversized_missing": del report["oversized"]
    elif change == "oversized_negative": report["oversized"]["appledouble"] = -1
    elif change == "oversized_type": report["oversized"]["tdb"] = True
    elif change == "oversized_more_items_than_counted": report["oversized"]["tdb"] = 0
    elif change == "oversized_over_limit":
        report["oversized"] = {"tdb": 51, "appledouble": 0, "folder_forks": 0, "items": [oversized_item()] * 51}
    elif change == "oversized_path_hex": item["path_hex"] = "not hex"
    elif change == "oversized_name_hex": item["name_hex"] = ""
    elif change == "oversized_kind": item["kind"] = "sidecar"
    elif change == "oversized_reason": item["reason"] = "unknown"
    elif change == "oversized_folder_forks_missing": del report["oversized"]["folder_forks"]
    elif change == "oversized_folder_forks_over_count": report["oversized"]["folder_forks"] = 2
    elif change == "oversized_empty_folder_fork": item.update(reason="folder_fork", size=0)
    elif change == "oversized_fits_natively": item["size"] = m.NATIVE_XATTR_LIMIT
    elif change == "deleted_missing": del report["sources"][0]["deleted"]
    elif change == "deleted_type": report["sources"][0]["deleted"] = True
    elif change == "deleted_negative": report["sources"][0]["deleted"] = -1
    elif change == "deleted_without_d": report.update(backup_hex="2f78"); report["sources"][0]["deleted"] = 1
    elif change == "backup_without_deleted": report["backup_hex"] = "2f78"
    else: raise AssertionError(change)
    with pytest.raises(RuntimeError, match="Invalid migration"):
        m.validate_native_report(report, inv)


def drop_report(deleted=1, retired=0, backup="/x.orphaned.1"):
    report = {"version": 1, "entries": 0, "oversized": NO_OVERSIZED,
              "sources": [{"index": 0, "total": 3, "retired": retired, "deleted": deleted, "coverage": [["O", KEY_B]]}]}
    if backup is not None:
        report["backup_hex"] = os.fsencode(backup).hex()
    return report


@pytest.mark.parametrize("report, verified", [
    (drop_report(), 1),                                  # deferred: every verified row named was dropped
    (drop_report(deleted=0, backup=None), 0),            # deferred with nothing verified: no copy either
    (drop_report(deleted=0, retired=2, backup=None), 1),  # not deferred: whole-file retirement drops none
])
def test_native_report_accepts_dropped_rows_only_as_requested(device, report, verified):
    inv = device.inventory(); device.inspect(inv)
    m.validate_native_report(report, inv, verified_rows=verified)


@pytest.mark.parametrize("report, verified", [
    (drop_report(deleted=1), 2),                          # fewer rows dropped than were verified
    (drop_report(deleted=0, backup=None), 1),             # a deferred retire that kept verified rows
    (drop_report(deleted=1, retired=2), 1),               # retired and dropped at once
    (drop_report(deleted=1, backup=None), 1),             # dropped rows without their copy
    (drop_report(deleted=0), 0),                          # a copy without dropped rows
    (drop_report(backup="not hex"), 1),
])
def test_native_report_rejects_dropped_rows_that_do_not_match_the_request(device, report, verified):
    inv = device.inventory(); device.inspect(inv)
    if report.get("backup_hex") == os.fsencode("not hex").hex():
        report["backup_hex"] = "not hex"
    with pytest.raises(RuntimeError, match="Invalid migration"):
        m.validate_native_report(report, inv, verified_rows=verified)


def test_native_report_accepts_kept_values_and_decodes_them(device):
    inv = device.inventory(); device.inspect(inv)
    report = {
        "version": 1,
        "entries": 1,
        "oversized": {"tdb": 60, "appledouble": 1, "folder_forks": 0, "items": [oversized_item(size=3803)] * 49 + [
            oversized_item("/Volumes/dk2/Photos/._x", "com.apple.big", 5000, "appledouble")]},
        "sources": [{"index": 0, "total": 2, "retired": 0, "deleted": 0, "coverage": [["X", KEY_A], ["M", KEY_B]]}],
    }
    coverage = m.validate_native_report(report, inv)
    assert coverage == {m.source_id(inv.sources[0]): [["X", KEY_A], ["M", KEY_B]]}
    kept = m.decode_oversized(report)
    assert (kept.tdb, kept.appledouble, kept.total, len(kept.values)) == (60, 1, 61, 50)
    assert kept.values[0] == m.OversizedValue("tdb", CONTAINER, PERSONALITY, 3803)
    assert kept.values[-1] == m.OversizedValue("appledouble", "/Volumes/dk2/Photos/._x", "com.apple.big", 5000)


def test_native_report_accepts_folder_forks_of_any_size(device):
    """HFS folders hold no resource fork, so even a small one stays in the ._ file."""
    inv = device.inventory(); device.inspect(inv)
    report = {
        "version": 1,
        "entries": 2,
        "oversized": {"tdb": 1, "appledouble": 2, "folder_forks": 2, "items": [
            oversized_item("/Volumes/dk2/pass.txt.rtfd", "com.apple.ResourceFork", 64, "appledouble", "folder_fork"),
            oversized_item("/Volumes/dk2/Old.rtfd", "com.apple.ResourceFork", 10, "tdb", "folder_fork"),
            oversized_item("/Volumes/dk2/Photos/._x", "com.apple.big", 5000, "appledouble")]},
        "sources": [{"index": 0, "total": 2, "retired": 0, "deleted": 0, "coverage": [["X", KEY_A], ["M", KEY_B]]}],
    }
    m.validate_native_report(report, inv)
    kept = m.decode_oversized(report)
    assert (kept.tdb, kept.appledouble, kept.folder_forks, kept.total) == (1, 2, 2, 3)
    assert kept.values[0] == m.OversizedValue(
        "appledouble", "/Volumes/dk2/pass.txt.rtfd", "com.apple.ResourceFork", 64, "folder_fork")
    assert kept.values[2].reason == "size"


def test_kept_values_complete_the_volume_like_orphans_and_quarantine_on_retirement(device):
    """Issue 345: a record with a value too large for HFS is done, never rescanned."""
    device.kind[UUID_A] = "X"
    device.oversized[UUID_A] = {"tdb": 2, "appledouble": 1, "folder_forks": 1, "items": [
        oversized_item(), oversized_item(size=41409),
        oversized_item("/Volumes/dk2/Music/song.rtfd", "com.apple.ResourceFork", 50, "appledouble", "folder_fork")]}
    # Deploy 1: the second disk is absent, and retirement keeps the DB.
    device.absent = False  # disk B is the only one missing
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy")
    output = device.phase(inv, "cleanup")
    assert (f"phase=copy uuid={UUID_A} entries=3 oversized_tdb=2 oversized_appledouble=1 folder_forks=1 "
            "complete") in output
    assert inv.oversized["copy"].total == 3 and inv.oversized["cleanup"].total == 3
    assert inv.oversized["copy"].folder_forks == 1 and inv.oversized["cleanup"].folder_forks == 1
    # Retirement did not set the database aside, so it is still live.
    assert inv.oversized["copy"].database_outcome is None
    assert inv.oversized["cleanup"].database_outcome == "in_place"
    assert inv.oversized["copy"].values[0] == m.OversizedValue("tdb", CONTAINER, PERSONALITY, 12979)
    saved = m.decode_receipt(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())
    assert list(saved["completed"][UUID_A]["coverage"].values()) == [[["X", KEY_A]]]
    retire = next(request for args, request, _ in device.calls if args == ["multi", "retire"])
    assert f"K 0 X {KEY_A}\n".encode() in retire
    assert device.source.read_bytes() == b"legacy metadata"

    # Deploy 2: the completed volume is not walked again, the returning disk
    # is, and retirement replays X from the receipt and quarantines the DB.
    device.mounted.add(UUID_B)
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy")
    output = device.phase(inv, "cleanup")
    assert all(f"R {UUID_A} ".encode() not in request for _, request, _ in device.scan_calls())
    assert inv.oversized["copy"].total == 0
    retire = next(request for args, request, _ in device.calls if args == ["multi", "retire"])
    assert f"K 0 X {KEY_A}\n".encode() in retire and f"K 0 M {KEY_B}\n".encode() in retire
    assert (f"retired source=.samba4/private/xattr.tdb uuid={UUID_A} outcome=quarantined "
            "orphaned=0 oversized=1") in output
    assert not Path(str(device.source) + m.RECEIPT_SUFFIX).exists()


@pytest.mark.parametrize("absent_disk, absent_source, outcome", [
    (False, False, "quarantined"), (True, False, "in_place"), (False, True, "in_place")])
def test_cleanup_records_where_kept_database_values_ended_up(device, absent_disk, absent_source, outcome):
    device.kind[UUID_A] = "X"
    device.oversized[UUID_A] = {"tdb": 1, "appledouble": 0, "folder_forks": 0, "items": [oversized_item()]}
    device.absent = absent_disk
    if absent_source:
        other = Path(device.volumes[1].volume_root) / ".samba4/private/xattr.tdb"
        other.parent.mkdir(parents=True); other.write_bytes(b"other database")
        inv = device.inventory(); device.inspect(inv)
        device.mounted.remove(UUID_B)  # its source disk leaves before this deploy migrates
        device.volumes = device.volumes[:1]
        inv = device.inventory(); device.inspect(inv)
    else:
        inv = device.inventory(); device.inspect(inv)
    device.phase(inv, "copy"); output = device.phase(inv, "cleanup")
    assert inv.oversized["cleanup"].database_outcome == outcome
    assert inv.oversized["copy"].database_outcome is None
    assert ("source_volume_absent" in output) is absent_source


def test_reported_names_are_decoded_for_display_only():
    """A ._ file can hold any bytes; a strict UTF-8 terminal must still print them."""
    report = {"oversized": {"tdb": 0, "appledouble": 1, "folder_forks": 0, "items": [
        {"kind": "appledouble", "reason": "size", "path_hex": b"/Volumes/dk2/caf\xe9".hex(),
         "name_hex": b"com.apple.\xff".hex(), "size": 5000}]}}
    value = m.decode_oversized(report).values[0]
    assert value.path == "/Volumes/dk2/caf\ufffd" and value.name == "com.apple.\ufffd"
    (value.path + value.name).encode("utf-8", "strict")


def test_receipt_accepts_known_coverage_kinds_and_rescans_on_unknown_ones(device):
    """Builds older than X reject it the same way this build rejects Z: a rescan."""
    doc = saved_receipt(device)
    decoded = m.decode_receipt(json.dumps(doc).encode())
    assert decoded is not None and sorted(kind for kind, _key in records_of(decoded)) == ["M", "O", "X"]
    for index in range(3):
        bad = copy.deepcopy(doc)
        records_of(bad)[index][0] = "Z"
        assert m.decode_receipt(json.dumps(bad).encode()) is None


def test_aliases_are_deduplicated_and_each_receipt_keeps_the_original_decoder(device):
    other = Path(device.volumes[0].volume_root) / "tc-netbsd7/private/xattr.tdb"
    other.parent.mkdir(parents=True)
    other.symlink_to(device.source)
    inv = device.inventory(); device.inspect(inv)
    assert len(inv.sources) == 1
    assert inv.sources[0]["path"] == str(device.source)
    assert inv.sources[0]["aliases"] == [str(other)]
    assert inv.sources[0]["relative"] == "tc-netbsd7/private/xattr.tdb"
    for path in [other, device.source]:
        doc = m.decode_receipt(Path(str(path) + m.RECEIPT_SUFFIX).read_bytes())
        assert doc is not None and doc["sources"][0]["mode"] == "netatalk" and not doc["completed"]


def retire_request(device):
    return next(request for args, request, _ in reversed(device.calls) if args == ["multi", "retire"])


def test_lone_source_drops_verified_rows_so_a_lost_receipt_cannot_replay(device):
    """Retirement waits for disk B, so the database stays. Its verified rows
    for disk A go: losing A's receipt later costs a walk, not native edits."""
    device.mounted.remove(UUID_B)
    original = device.source.read_bytes()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    output = device.phase(inv, "cleanup")
    backup = Path(f"{device.source}.orphaned.1")
    lines = retire_request(device).decode().splitlines()
    assert lines.index("D") < lines.index(f"K 0 M {KEY_A}")
    assert f"dropped_verified_rows source=.samba4/private/xattr.tdb uuid={UUID_A} count=1 backup={backup}" in output
    assert (inv.dropped_rows, inv.backup) == (1, str(backup))
    assert backup.read_bytes() == original and device.source.read_bytes() != original
    # The receipt names the changed database and no longer lists A's dropped key.
    receipt = Path(str(device.source) + m.RECEIPT_SUFFIX)
    saved = m.decode_receipt(receipt.read_bytes())
    assert list(saved["completed"][UUID_A]["coverage"].values()) == [[]]
    assert saved["sources"][0]["hash"] == hashlib.sha256(device.source.read_bytes()).hexdigest()[:16]

    device.native[UUID_A] = "new native edit"
    receipt.write_text('{"version":1,')
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert any(f"R {UUID_A} ".encode() in request for _, request, _ in device.scan_calls())
    assert device.native[UUID_A] == "new native edit"
    assert UUID_A in inv.completed and inv.dropped_rows == 0
    assert not Path(f"{device.source}.orphaned.2").exists()


def test_receipt_lost_after_the_drop_costs_only_a_walk(device):
    device.mounted.remove(UUID_B)
    device.fail = "save_after_drop"
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    with pytest.raises(RuntimeError, match="flush failed"):
        device.phase(inv, "cleanup")
    device.fail = None
    device.native[UUID_A] = "new native edit"
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    # The receipt still names the database before the drop: a walk, no replay.
    assert any(f"R {UUID_A} ".encode() in request for _, request, _ in device.scan_calls())
    assert device.native[UUID_A] == "new native edit"
    assert UUID_A in inv.completed


def test_redeploy_after_the_drop_changes_nothing(device):
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    receipt = Path(str(device.source) + m.RECEIPT_SUFFIX)
    database, saved = device.source.read_bytes(), receipt.read_bytes()
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); output = device.phase(inv, "cleanup")
    assert not device.scan_calls() and "D" in retire_request(device).decode().splitlines()
    assert "K 0 M" not in retire_request(device).decode()
    assert inv.dropped_rows == 0 and inv.backup is None and "dropped_verified_rows" not in output
    assert device.source.read_bytes() == database and receipt.read_bytes() == saved
    assert not Path(f"{device.source}.orphaned.2").exists()


def test_receipt_from_before_row_dropping_drops_its_rows_without_a_walk(device, monkeypatch):
    """A v3.1.x receipt still lists verified rows in a retained database."""
    device.mounted.remove(UUID_B)
    with monkeypatch.context() as old_build:
        old_build.setattr(m, "drops_verified_rows", lambda _inventory: False)
        inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert "D" not in retire_request(device).decode().splitlines()
    assert device.source.read_bytes() == b"legacy metadata"
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert not device.scan_calls()
    assert f"K 0 M {KEY_A}" in retire_request(device).decode()
    assert inv.dropped_rows == 1 and Path(f"{device.source}.orphaned.1").read_bytes() == b"legacy metadata"


def test_returning_disk_retires_the_database_whole_after_a_drop(device):
    device.absent = False  # disk B is the only one missing
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    backup = Path(f"{device.source}.orphaned.1")
    copied = backup.read_bytes()
    device.mounted.add(UUID_B)
    device.kind[UUID_B] = "O"
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); output = device.phase(inv, "cleanup")
    request = retire_request(device).decode()
    assert f"K 0 O {KEY_B}" in request and "K 0 M" not in request
    assert "outcome=quarantined" in output and inv.dropped_rows == 0
    assert not Path(str(device.source) + m.RECEIPT_SUFFIX).exists() and not device.source.exists()
    # The quarantine takes the next slot; the copy made before the drop stays.
    assert backup.read_bytes() == copied
    assert Path(f"{device.source}.orphaned.2").read_bytes() == copied + b" dropped"


def test_lone_source_with_every_row_resolved_is_deleted_whole(device):
    """Nothing waits for a disk, so D asks for nothing: no drop, no copy."""
    device.absent = False
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); output = device.phase(inv, "cleanup")
    assert "D" in retire_request(device).decode().splitlines()
    assert f"retired source=.samba4/private/xattr.tdb uuid={UUID_A} outcome=deleted orphaned=0 oversized=0" in output
    assert "retirement complete" in output and inv.dropped_rows == 0 and inv.backup is None
    assert not device.source.exists() and not Path(str(device.source) + m.RECEIPT_SUFFIX).exists()
    assert not Path(f"{device.source}.orphaned.1").exists()


def test_several_sources_never_drop_rows(device):
    """Their precedence is their mtimes, and a cohort source may be absent."""
    second = Path(device.volumes[0].volume_root) / "tc-netbsd7/private/xattr.tdb"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"newer legacy metadata")
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert "D" not in retire_request(device).decode().splitlines()
    assert device.source.read_bytes() == b"legacy metadata" and second.read_bytes() == b"newer legacy metadata"
    assert inv.dropped_rows == 0


def test_only_a_retire_request_can_ask_to_drop_rows(device):
    inv = device.inventory(); device.inspect(inv)
    stat = {"dev": 1, "inode": 2}
    with pytest.raises(ValueError):
        m.request_bytes(inv, (inv.volumes[0], stat), drop_verified=True)
    assert "D" not in m.request_bytes(inv).decode().splitlines()


def test_database_path_changing_during_the_drop_saves_nothing(device):
    """Deploy cannot tell which file it changed: it stops before pruning its
    coverage or writing a receipt. The receipt from before the drop no longer
    matches the file, so the next deploy walks again; the dropped rows were
    verified, so the walk rewrites nothing."""
    device.mounted.remove(UUID_B)
    device.moved = True
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    with pytest.raises(RuntimeError, match="moved while dropping"):
        device.phase(inv, "cleanup")
    key = m.source_id(inv.sources[0])
    assert ["M", KEY_A] in inv.completed[UUID_A]["coverage"][key] and inv.dropped_rows == 0
    saved = m.decode_receipt(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())
    assert ["M", KEY_A] in records_of(saved)
    assert saved["sources"][0]["hash"] == hashlib.sha256(b"legacy metadata").hexdigest()[:16]

    device.moved = False
    device.native[UUID_A] = "new native edit"
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert any(f"R {UUID_A} ".encode() in request for _, request, _ in device.scan_calls())
    assert device.native[UUID_A] == "new native edit"


def aliased_source(device) -> Path:
    """A second payload spelling of the same database: a symlinked file."""
    alias = Path(device.volumes[0].volume_root) / "tc-netbsd7/private/xattr.tdb"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(device.source)
    return alias


def test_after_a_drop_every_spelling_of_the_database_has_a_current_receipt(device):
    alias = aliased_source(device)
    device.mounted.remove(UUID_B)
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert inv.dropped_rows == 1 and inv.sources[0]["aliases"] == [str(alias)]
    current = hashlib.sha256(device.source.read_bytes()).hexdigest()[:16]
    receipts = [Path(str(path) + m.RECEIPT_SUFFIX).read_bytes() for path in (device.source, alias)]
    assert receipts[0] == receipts[1]
    saved = m.decode_receipt(receipts[0])
    assert saved["sources"][0]["hash"] == current and records_of(saved) == []

    device.calls.clear()
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert not device.scan_calls() and "K 0 M" not in retire_request(device).decode()
    assert inv.dropped_rows == 0


def test_a_stale_alias_receipt_after_a_drop_is_ignored_and_rewritten(device):
    """The canonical receipt is written first. If the alias's write fails, the
    current receipt is trusted, the stale one ignored, and the next inspect
    rewrites both."""
    alias = aliased_source(device)
    alias_receipt = Path(str(alias) + m.RECEIPT_SUFFIX)
    canonical_receipt = Path(str(device.source) + m.RECEIPT_SUFFIX)
    device.mounted.remove(UUID_B)
    device.fail_save_after_drop = {str(alias_receipt)}
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy")
    with pytest.raises(RuntimeError, match="flush failed"):
        device.phase(inv, "cleanup")
    current = hashlib.sha256(device.source.read_bytes()).hexdigest()[:16]
    assert m.decode_receipt(canonical_receipt.read_bytes())["sources"][0]["hash"] == current
    stale = m.decode_receipt(alias_receipt.read_bytes())
    assert stale["sources"][0]["hash"] != current and ["M", KEY_A] in records_of(stale)

    device.fail_save_after_drop = set()
    device.native[UUID_A] = "new native edit"
    device.calls.clear()
    inv = device.inventory(); device.inspect(inv)
    assert UUID_A in inv.completed and alias_receipt.read_bytes() == canonical_receipt.read_bytes()
    device.phase(inv, "copy"); device.phase(inv, "cleanup")
    assert not device.scan_calls() and "K 0 M" not in retire_request(device).decode()
    assert device.native[UUID_A] == "new native edit" and inv.dropped_rows == 0
