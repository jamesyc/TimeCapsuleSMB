"""Deploy orchestration tests. Native value/extent conversion also runs on NetBSD.

Apple owns the HFS mounts; failed or missing mounts are partial migration, never
proof that their TDB records are orphans. Completed volumes must not be walked.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from timecapsulesmb.deploy import migration as m
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.transport.errors import SshCommandTimeout
from timecapsulesmb.transport.ssh import SshConnection

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
KEY_A = "01000000000000000100000000000000"
KEY_B = "02000000000000000100000000000000"


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
    state = SimpleNamespace(volumes=volumes, source=source, calls=[], mounted={UUID_A, UUID_B}, fail=None,
                            reads=[], native={UUID_A: "old", UUID_B: "old"}, retired=False)
    monkeypatch.setattr(m, "read_mast_volumes_conn", lambda _conn: state.volumes)
    monkeypatch.setattr(m, "ensure_volume_root_mounted_conn", lambda _c, root, *_a, **_k: any(v.volume_root == root and v.adisk_uuid in state.mounted for v in state.volumes))

    def read(_conn, path, **_kwargs):
        state.reads.append(path)
        path = config if path == "/mnt/Flash/tcapsulesmb.conf" else Path(path)
        return path.read_bytes() if path.is_file() else b""

    def ssh(_conn, command, *, input_bytes=b"", check=True, **_kwargs):
        if state.fail == "save" and command.startswith("umask"):
            raise RuntimeError("flush failed")
        return subprocess.run(command.replace("/bin/sync", "true"), shell=True, executable="/bin/sh",
                              input=input_bytes, capture_output=True, check=check)

    def native(_conn, args, *, request=b"", **kwargs):
        state.calls.append((args, request, kwargs))
        if args[0] in {"inspect", "inspect-root"}:
            path = Path(args[1]); st = path.stat()
            return {"path_hex": os.fsencode(path.resolve()).hex(), "dev": st.st_dev, "inode": st.st_ino,
                    "size": st.st_size, "mtime": st.st_mtime_ns // 10**9, "nsec": st.st_mtime_ns % 10**9,
                    "hash": hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else "0" * 16}
        lines = request.decode().splitlines()
        roots = [line.split() for line in lines if line.startswith("R ")]
        sources = [line.split() for line in lines if line.startswith("S ")]
        phase = args[1]
        if state.fail == phase or (roots and state.fail == roots[0][1]):
            raise RuntimeError("injected migration failure")
        if phase == "retire":
            state.retired = True
            return {"version": 1, "entries": 0, "sources": [{"index": i, "coverage": [], "retired": 0} for i in range(len(sources))]}
        key = roots[0][1]
        if phase == "copy":
            state.native[key] = "migrated"
        return {"version": 1, "entries": 3, "sources": [{"index": i, "total": 2, "retired": 0,
            "coverage": [["M", KEY_A if key == UUID_A else KEY_B]] if phase == "cleanup" else []} for i in range(len(sources))]}

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


def test_remaining_deadline_is_shared_across_volumes(device, monkeypatch):
    inv = device.inventory(); device.inspect(inv)
    ticks = iter([100, 105, 110, 120, 130])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(ticks))
    device.phase(inv, "copy")
    assert [kwargs["timeout"] for _, _, kwargs in device.scan_calls()] == [21590, 21570]


def test_native_command_keeps_protocol_stdout_separate_and_reports_timeout(monkeypatch):
    native = m._native
    captured = []
    def fail(_conn, command, **kwargs):
        captured.append((command, kwargs)); raise SshCommandTimeout("six hour deadline")
    monkeypatch.setattr(m, "run_ssh_input", fail)
    monkeypatch.setattr(m, "run_ssh", lambda *a, **k: SimpleNamespace(stdout="last progress entries=10000"))
    with pytest.raises(SshCommandTimeout, match="last progress entries=10000"):
        native(SshConnection("test", "", ""), ["multi", "copy"], request=b"request", timeout=100, log="/disk/log")
    assert captured[0][1] == {"input_bytes": b"request", "timeout": 100}


def test_rejects_unknown_duplicate_and_excess_key_coverage(device):
    inv = device.inventory(); device.inspect(inv); device.phase(inv, "copy"); device.phase(inv, "cleanup")
    doc = json.loads(Path(str(device.source) + m.RECEIPT_SUFFIX).read_bytes())
    bad = copy.deepcopy(doc)
    coverage = bad["completed"][UUID_A]["coverage"]
    records = next(iter(coverage.values())); records.append(records[0])
    assert m.decode_receipt(json.dumps(bad).encode()) is None
    bad = copy.deepcopy(doc); bad["completed"][UUID_A]["coverage"]["unknown"] = []
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


def test_native_shell_wrapper_passes_stdin_without_mixing_diagnostics(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text('#!/bin/sh\nread first\nprintf "diagnostic from %s\\n" "$first" >&2\nprintf \'{"version":1,"echo":"%s"}\\n\' "$first"\n')
    helper.chmod(0o755)
    log = tmp_path / "migration.log"
    monkeypatch.setattr(m, "RAM_HELPER", str(helper))
    def local(_conn, command, *, input_bytes, **_kwargs):
        return subprocess.run(command, shell=True, executable="/bin/sh", input=input_bytes, capture_output=True, check=True)
    monkeypatch.setattr(m, "run_ssh_input", local)
    assert m._native(SshConnection("test", "", ""), ["multi", "copy"], request=b"TCMIGRATE1\n", log=str(log)) == {"version": 1, "echo": "TCMIGRATE1"}
    assert "diagnostic from TCMIGRATE1" in log.read_text()
    assert "migration_exit_code=0" in log.read_text()


def test_native_shell_interruption_stops_owned_helper(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    helper = tmp_path / "helper"
    pid = tmp_path / "test-child.pid"
    helper.write_text(f'#!/bin/sh\necho $$ > {shlex.quote(str(pid))}\nexec sleep 30\n')
    helper.chmod(0o755)
    monkeypatch.setattr(m, "RAM_HELPER", str(helper))
    running = []
    def local(_conn, command, *, input_bytes, **_kwargs):
        child = subprocess.Popen(command, shell=True, executable="/bin/sh", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        running.append(child)
        stdout, stderr = child.communicate(input_bytes)
        if child.returncode: raise RuntimeError("interrupted")
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    monkeypatch.setattr(m, "run_ssh_input", local)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(m._native, SshConnection("test", "", ""), ["multi", "copy"], log=str(tmp_path / "log"))
        until = time.monotonic() + 5
        while not pid.exists() and time.monotonic() < until: time.sleep(0.01)
        assert pid.exists()
        running[0].terminate()
        with pytest.raises(RuntimeError, match="interrupted"): result.result(timeout=5)
    with pytest.raises(ProcessLookupError): os.kill(int(pid.read_text()), 0)
