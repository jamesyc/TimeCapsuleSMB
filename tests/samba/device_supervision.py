"""Opt-in integration checks against an installed native manager.

Run manually with smbprotocol available, never through ordinary pytest. These
checks interrupt SMB service and create only uniquely named scratch directories.
Apple's diskd, afpserver and mDNSResponder must retain their original PIDs.
The temporary RAM config is restored before testing manager shutdown/restart.
"""
from __future__ import annotations

import argparse
import re
import shlex
import time
import uuid
from pathlib import Path

from smbprotocol.connection import Connection
from smbprotocol.create_contexts import SMB2CreateDurableHandleRequestV2, SMB2CreateDurableHandleReconnectV2
from smbprotocol.open import Open, ImpersonationLevel, FilePipePrinterAccessMask, FileAttributes, ShareAccess, CreateDisposition, CreateOptions, RequestedOplockLevel
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.transport.ssh import SshConnection, run_ssh, run_ssh_input

CONF = "/mnt/Memory/samba4/etc/smb.conf"
PS = "/bin/ps axww -o pid= -o ppid= -o pgid= -o stat= -o ucomm= -o command="


class Device:
    def __init__(self, config: Path):
        settings = AppConfig.from_file(config)
        self.ssh = SshConnection(settings.get("TC_HOST"), settings.get("TC_PASSWORD"), settings.get("TC_SSH_OPTS"))
        self.host = self.ssh.host.split("@")[-1]
        self.password = settings.get("TC_PASSWORD")

    def command(self, text):
        return run_ssh(self.ssh, text, timeout=30).stdout

    def processes(self):
        rows = []
        for line in self.command(PS).splitlines():
            fields = line.split(None, 5)
            if len(fields) == 6 and fields[0].isdigit() and not fields[3].startswith("Z"):
                rows.append(dict(pid=int(fields[0]), parent=int(fields[1]), group=int(fields[2]), name=fields[4], args=fields[5]))
        return rows

    def role(self, name, rows=None):
        rows = self.processes() if rows is None else rows
        selected = [p for p in rows if (p["name"] == "service" and p["args"].startswith("service: role=" + name + " "))]
        assert len(selected) == 1, (name, selected)
        return selected[0]

    def samba(self, rows=None):
        rows = self.processes() if rows is None else rows
        manager = self.role("manager", rows)
        matches = [p for p in rows if p["name"] == "smbd" and p["parent"] == manager["pid"]]
        assert len(matches) == 1, matches
        samba = matches[0]
        assert "-F --no-process-group" in samba["args"], samba
        assert samba["group"] == samba["pid"] and samba["group"] != manager["group"], (samba, manager)
        return samba

    def signal(self, pid, signal):
        self.command(f"kill -{signal} {int(pid)}")

    def await_state(self, predicate, timeout=90):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = self.processes()
            try:
                result = predicate(rows)
                if result:
                    return result
            except AssertionError:
                pass
            time.sleep(1)
        raise AssertionError("process state did not converge")

    def session(self, client_guid=None):
        # Samba requires signed/encrypted tree connects for its root account,
        # even when optional client signing is disabled in the appliance config.
        connection = Connection(client_guid or uuid.uuid4(), self.host, require_signing=True)
        connection.connect(timeout=15)
        session = Session(connection, username="root", password=self.password, require_encryption=False)
        session.connect()
        return connection, session

    def share(self, session, name):
        tree = TreeConnect(session, f"\\\\{self.host}\\{name}")
        tree.connect()
        return tree

    def publish(self, text):
        run_ssh_input(self.ssh, f"cat > {CONF}", input_bytes=text.encode())
        self.signal(self.samba()["pid"], "HUP")
        time.sleep(2)


def open_file(tree, name, *, contexts=None, create=False, batch=False):
    handle = Open(tree, name)
    response = handle.create(
        ImpersonationLevel.Impersonation,
        FilePipePrinterAccessMask.GENERIC_READ | FilePipePrinterAccessMask.GENERIC_WRITE,
        FileAttributes.FILE_ATTRIBUTE_NORMAL,
        ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
        CreateDisposition.FILE_OPEN_IF if create else CreateDisposition.FILE_OPEN,
        CreateOptions.FILE_NON_DIRECTORY_FILE,
        create_contexts=contexts,
        oplock_level=RequestedOplockLevel.SMB2_OPLOCK_LEVEL_BATCH if batch else RequestedOplockLevel.SMB2_OPLOCK_LEVEL_NONE,
    )
    return handle, response


def durable_reconnect(device, share, filename):
    client_guid, create_guid = uuid.uuid4(), uuid.uuid4()
    connection, session = device.session(client_guid)
    tree = device.share(session, share)
    durable = SMB2CreateDurableHandleRequestV2()
    durable["timeout"] = 60000
    durable["create_guid"] = create_guid
    handle, responses = open_file(tree, filename, contexts=[durable], create=True, batch=True)
    assert responses and any(type(c).__name__ == "SMB2CreateDurableHandleResponseV2" for c in responses), responses
    content = b"TimeCapsuleSMB direct-child durable reconnect\n" * 256
    handle.write(content, 0)
    handle.flush()
    file_id = handle.file_id
    connection.disconnect(close=False)  # Drop transport without CLOSE/LOGOFF.
    connection, session = device.session(client_guid)
    try:
        tree = device.share(session, share)
        reconnect = SMB2CreateDurableHandleReconnectV2()
        reconnect["file_id"] = file_id
        reconnect["create_guid"] = create_guid
        handle, _ = open_file(tree, filename, contexts=[reconnect], batch=True)
        assert handle.read(0, len(content)) == content
        handle.close()
    finally:
        connection.disconnect()
    print("PASS durable network reconnect under foreground Samba", flush=True)


def targeted_reload(device, base_config, share, root):
    # Apple can replace a disk at the same path. Replacing a scratch share root
    # exercises that identity boundary without unmounting a user's data disk.
    names = ["TC-supervision-A", "TC-supervision-B"]
    paths = [root + "/a", root + "/b"]
    device.command("mkdir -p " + shlex.join(paths))
    production = re.search(r"(?ms)^\[" + re.escape(share) + r"\]\n(.*?)(?=^\[|\Z)", base_config).group(1)
    additions = ""
    for name, path in zip(names, paths):
        body = re.sub(r"(?m)^\s*path\s*=.*$", "    path = " + path, production)
        additions += "\n[" + name + "]\n" + body
    connection = None
    handles = []
    try:
        device.publish(base_config + additions)
        connection, session = device.session()
        for name in names:
            tree = device.share(session, name)
            handle, _ = open_file(tree, "probe", create=True)
            handle.write(name.encode(), 0)
            handle.flush()
            handles.append(handle)
        parent = device.samba()["pid"]
        device.signal(parent, "HUP")
        time.sleep(2)
        for handle, name in zip(handles, names):
            assert handle.read(0, len(name)) == name.encode()
        device.command(f"mv {shlex.quote(paths[0])} {shlex.quote(paths[0] + '.removed')} && mkdir {shlex.quote(paths[0])}")
        device.signal(parent, "HUP")
        time.sleep(2)
        assert handles[1].read(0, len(names[1])) == names[1].encode()
        try:
            handles[0].read(0, len(names[0]))
        except Exception as exc:
            assert any(status in str(exc) for status in ("NETWORK_NAME_DELETED", "USER_SESSION_DELETED", "FILE_CLOSED")), exc
        else:
            raise AssertionError("stale share was still usable")
        assert device.samba()["pid"] == parent
        print("PASS parent HUP preserves valid trees and disconnects only replaced share", flush=True)
    finally:
        try:
            # The intentionally revoked tree rejects CLOSE as well. Close the
            # surviving handle, then drop transport without revisiting that tree.
            if len(handles) == 2:
                handles[1].close()
        finally:
            if connection is not None:
                connection.disconnect(close=False)
            device.publish(base_config)


def supervise(device, share, filename):
    initial = device.processes()
    apple = {p["name"]: p["pid"] for p in initial if p["name"] in {"mDNSResponder", "afpserver", "diskd"}}
    assert set(apple) == {"mDNSResponder", "afpserver", "diskd"}
    expected = b"TimeCapsuleSMB direct-child durable reconnect\n" * 256

    def client():
        connection, session = device.session()
        try:
            handle, _ = open_file(device.share(session, share), filename)
            assert handle.read(0, len(expected)) == expected
            return connection, handle
        except Exception:
            connection.disconnect(close=False)
            raise

    # rc.local can be invoked after firmware already started it. The existing
    # Flash inode lock must protect both service ownership and Samba's locks.
    manager, samba = device.role("manager", initial), device.samba(initial)
    device.command("/bin/sh /mnt/Flash/rc.local")
    time.sleep(2)
    assert device.role("manager")["pid"] == manager["pid"]
    assert device.samba()["pid"] == samba["pid"]
    print("PASS duplicate boot preserves live manager and Samba", flush=True)
    for role, sig in [("smbd", "TERM"), ("smbd", "KILL"), ("discovery", "KILL"), ("telemetry", "TERM")]:
        connection, handle = client()
        rows = device.processes()
        old = device.samba(rows) if role == "smbd" else device.role(role, rows)
        try:
            device.signal(old["pid"], sig)
            def recovered(rows):
                new = device.samba(rows) if role == "smbd" else device.role(role, rows)
                return new["pid"] != old["pid"] and not any(p["group"] == old["group"] for p in rows)
            device.await_state(recovered)
            if role != "smbd":
                assert handle.read(0, len(expected)) == expected
        finally:
            connection.disconnect(close=False)
        connection, handle = client()
        connection.disconnect()
        print(f"PASS {role} {sig} drains old generation before recovery", flush=True)
    for sig in ["TERM", "KILL"]:
        connection, handle = client()
        manager = device.role("manager")
        device.signal(manager["pid"], sig)
        try:
            device.await_state(lambda rows: not any(p["name"] in {"service", "smbd", "wcifsnd"} for p in rows))
            print(f"PASS manager {sig} drains direct children and descendants", flush=True)
        finally:
            connection.disconnect(close=False)
            device.command("/bin/sh /mnt/Flash/rc.local")
            device.await_state(lambda rows: device.samba(rows))
        connection, handle = client()
        connection.disconnect()
        current = {p["name"]: p["pid"] for p in device.processes() if p["name"] in apple}
        assert current == apple, (current, apple)
    print("PASS Apple daemons survive every supervision test", flush=True)


def native_nbns_failure(device):
    def initial_child(rows):
        controller = device.role("discovery", rows)
        children = [p for p in rows if p["name"] == "wcifsnd" and p["parent"] == controller["pid"]]
        return children[0] if "nbns=ready" in controller["args"] and len(children) == 1 else None

    # A listening Samba parent can precede discovery's native registrations.
    # Wait for the fault's precondition after the preceding manager restart.
    native = device.await_state(initial_child)
    # Deliberate fault injection only: routine manager checks must never kill
    # this child independently of its discovery controller.
    device.signal(native["pid"], "KILL")

    def ready(rows):
        owner = device.role("discovery", rows)
        children = [p for p in rows if p["name"] == "wcifsnd"]
        return ("nbns=ready" in owner["args"] and len(children) == 1
                and children[0]["parent"] == owner["pid"] and children[0]["pid"] != native["pid"])

    device.await_state(ready)
    print("PASS discovery restores native NBNS ownership after child death", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    device = Device(args.config)
    base = device.command("cat " + CONF)
    match = re.search(r"(?m)^\[([^]]+)\]\n\s*path = (/Volumes/[^\n]+)$", base)
    assert match, "No applied HFS share"
    share, share_root = match.groups()
    directory = ".tc-supervision-" + uuid.uuid4().hex[:12]
    root = share_root.rstrip("/") + "/" + directory
    device.command("mkdir " + shlex.quote(root))
    try:
        print("PASS direct manager child and isolated Samba process group", device.samba(), flush=True)
        durable_reconnect(device, share, directory + "\\durable")
        targeted_reload(device, base, share, root)
        supervise(device, share, directory + "\\durable")
        native_nbns_failure(device)
    finally:
        # Only this invocation's randomly named scratch tree is deleted.
        device.command("rm -rf " + shlex.quote(root))


if __name__ == "__main__":
    main()
