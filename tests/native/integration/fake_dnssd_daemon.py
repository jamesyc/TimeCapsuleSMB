"""A fake mDNSResponder for the registrant tests.

Speaks the `dns_sd` Unix-socket IPC the vendored Apple stub uses
(`build/native/dnssd/dnssd_ipc.h`, VERSION 1, all integers big-endian):

    header (28 bytes): version u32, datalen u32, ipc_flags u32, op u32,
                       client_context u32 x2, reg_index u32
    reg_service_request (op 5) payload: flags u32, interfaceIndex u32,
        name\\0, regtype\\0, domain\\0, host\\0, port u16 (network order),
        txtLen u16, txt bytes
    cancel_request (op 63): sent by DNSServiceRefDeallocate on subordinate
        connections; a primary connection simply closes its socket.

For a primary connection the daemon first writes the 4-byte error code for
the request on the same socket (0 = accepted), then any number of
asynchronous reply messages: header with op reg_service_reply_op (65) and
payload flags u32 (kDNSServiceFlagsAdd = 2), interfaceIndex u32, error u32,
name\\0, regtype\\0, domain\\0.

The fake records every request and connection close in a transcript and
supports scripted responses per instance name: ``accept`` (default),
``conflict`` (error -65548 in the reply), ``delay`` (accept, but hold the
reply until ``release``), ``slow-delay`` (delay the acknowledgement, then hold
the reply), ``drop`` (close the connection without a reply),
``stall`` (read the request but never send the 4-byte acknowledgement --
the synchronous stub blocks inside DNSServiceRegister), ``slow`` (send the
acknowledgement after one second, then the reply).
"""
from __future__ import annotations

import os
import selectors
import socket
import struct
import threading
import time

REG_SERVICE_REQUEST = 5
CANCEL_REQUEST = 63
REG_SERVICE_REPLY_OP = 65
FLAG_ADD = 0x2
FLAG_NO_AUTO_RENAME = 0x8
ERR_NAME_CONFLICT = -65548 & 0xFFFFFFFF
HEADER = struct.Struct(">IIIIIII")


def _cstr(data, offset):
    end = data.index(b"\0", offset)
    return data[offset:end].decode(), end + 1


def parse_txt(raw):
    items = []
    pos = 0
    while pos < len(raw):
        n = raw[pos]
        items.append(raw[pos + 1:pos + 1 + n].decode())
        pos += 1 + n
    return items


class FakeDnssdDaemon:
    def __init__(self, path):
        self.path = path
        self.transcript = []
        self.scripts = {}          # instance name -> accept|conflict|delay|drop
        self.default_name = "AirPort Time Capsule"
        self.lock = threading.Lock()
        self.held = {}             # conn id -> (sock, reply bytes)
        self._selector = selectors.DefaultSelector()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(path):
            os.unlink(path)
        self._listener.bind(path)
        self._listener.listen(16)
        self._listener.setblocking(False)
        self._selector.register(self._listener, selectors.EVENT_READ, ("listen", None))
        self._next_conn = 0
        self._conns = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.accepting = True      # False = refuse new connections (daemon "gone")

    # ----------------------------------------------------------- control --
    def script(self, name, behaviour):
        with self.lock:
            self.scripts[name] = behaviour

    def rename_default(self, name):
        """Apple updates all default-name registrations after a name conflict."""
        with self.lock:
            self.default_name = name
            for entry in self.transcript:
                if entry["op"] != "register" or entry["name"] or entry["conn"] not in self._conns:
                    continue
                body = struct.pack(">III", FLAG_ADD, entry["ifindex"], 0)
                body += name.encode() + b"\0" + entry["regtype"].encode() + b"\0local.\0"
                reply = HEADER.pack(1, len(body), 0, REG_SERVICE_REPLY_OP, *entry["context"], 0) + body
                self._conns[entry["conn"]].sendall(reply)

    def release(self, name=None):
        """Send the held reply for delayed registrations."""
        with self.lock:
            for conn_id, (sock, reply, held_name) in list(self.held.items()):
                if name is None or held_name == name:
                    try:
                        sock.sendall(reply)
                    except OSError:
                        pass
                    del self.held[conn_id]

    def held_reply_count(self):
        with self.lock:
            return len(self.held)

    def go_away(self):
        """Refuse connections and drop every live one (daemon death)."""
        self.accepting = False
        with self.lock:
            for conn_id, sock in list(self._conns.items()):
                self._close(conn_id, sock, record=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def come_back(self):
        self._listener.close()
        self._selector.unregister(self._listener)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(16)
        self._listener.setblocking(False)
        self._selector.register(self._listener, selectors.EVENT_READ, ("listen", None))
        self.accepting = True

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                snapshot = list(self.transcript)
            if predicate(snapshot):
                return snapshot
            time.sleep(0.02)
        with self.lock:
            return list(self.transcript) if predicate(list(self.transcript)) else None

    def registrations(self, live_only=True):
        """(ifindex, regtype, name) of registrations whose connection is still open."""
        with self.lock:
            open_conns = set(self._conns)
            return sorted({(e["ifindex"], e["regtype"], e["name"] or self.default_name) for e in self.transcript
                           if e["op"] == "register" and (not live_only or e["conn"] in open_conns)})

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ server --
    def _serve(self):
        while not self._stop.is_set():
            for key, _ in self._selector.select(timeout=0.05):
                kind, conn_id = key.data
                if kind == "listen":
                    self._accept()
                else:
                    self._read(conn_id, key.fileobj)

    def _accept(self):
        try:
            sock, _ = self._listener.accept()
        except OSError:
            return
        if not self.accepting:
            sock.close()
            return
        sock.setblocking(True)
        with self.lock:
            conn_id = self._next_conn
            self._next_conn += 1
            self._conns[conn_id] = sock
        self._selector.register(sock, selectors.EVENT_READ, ("conn", conn_id))

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _close(self, conn_id, sock, record):
        try:
            self._selector.unregister(sock)
        except (KeyError, ValueError):
            pass
        sock.close()
        self._conns.pop(conn_id, None)
        self.held.pop(conn_id, None)
        if record:
            self.transcript.append({"op": "close", "conn": conn_id})

    def _read(self, conn_id, sock):
        header = self._recv_exact(sock, HEADER.size)
        if header is None:
            with self.lock:
                self._close(conn_id, sock, record=True)
            return
        version, datalen, ipc_flags, op, ctx0, ctx1, reg_index = HEADER.unpack(header)
        payload = self._recv_exact(sock, datalen) if datalen else b""
        if payload is None:
            with self.lock:
                self._close(conn_id, sock, record=True)
            return
        if op == REG_SERVICE_REQUEST:
            flags, ifindex = struct.unpack_from(">II", payload, 0)
            pos = 8
            name, pos = _cstr(payload, pos)
            regtype, pos = _cstr(payload, pos)
            domain, pos = _cstr(payload, pos)
            host, pos = _cstr(payload, pos)
            port = struct.unpack_from(">H", payload, pos)[0]
            txt_len = struct.unpack_from(">H", payload, pos + 2)[0]
            txt = payload[pos + 4:pos + 4 + txt_len]
            with self.lock:
                reply_name = name or self.default_name
                behaviour = self.scripts.get(reply_name, "accept")
                self.transcript.append({"op": "register", "conn": conn_id, "version": version, "flags": flags,
                                        "context": (ctx0, ctx1),
                                        "no_auto_rename": bool(flags & FLAG_NO_AUTO_RENAME), "ifindex": ifindex,
                                        "name": name, "regtype": regtype, "domain": domain, "host": host,
                                        "port": port, "txt": parse_txt(txt), "behaviour": behaviour})
                if behaviour == "drop":
                    self._close(conn_id, sock, record=False)
                    return
                if behaviour == "stall":
                    return                            # accepted, never acknowledged
                if behaviour == "slow":
                    time.sleep(1.0)
                elif behaviour == "slow-delay":
                    time.sleep(0.6)
                sock.sendall(struct.pack(">I", 0))   # request accepted
                err = ERR_NAME_CONFLICT if behaviour == "conflict" else 0
                body = struct.pack(">III", FLAG_ADD if not err else 0, ifindex, err)
                body += reply_name.encode() + b"\0" + regtype.encode() + b"\0" + (domain or "local.").encode() + b"\0"
                reply = HEADER.pack(1, len(body), 0, REG_SERVICE_REPLY_OP, ctx0, ctx1, 0) + body
                if behaviour in {"delay", "slow-delay"}:
                    self.held[conn_id] = (sock, reply, reply_name)
                else:
                    sock.sendall(reply)
        elif op == CANCEL_REQUEST:
            with self.lock:
                self.transcript.append({"op": "cancel", "conn": conn_id})
        else:
            with self.lock:
                self.transcript.append({"op": "unknown", "conn": conn_id, "code": op})
                sock.sendall(struct.pack(">I", 0))
