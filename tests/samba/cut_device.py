"""Patch 0072 against a real Mac: cut one WRITE per connection the way macOS's
send bug does, under real smbfs traffic, and check how smbd ends each cut
connection. Run on the Mac, from the repo root, holding the device's lock row:

    .venv/bin/python -m tests.samba.cut_device --env .env.backup6 --share 4TB --out DIR
    .venv/bin/python -m tests.samba.cut_device --analyse DIR

The share is mounted through a proxy on 127.0.0.1 that forwards the Mac's
traffic to the device. Once per connection, `--settle` seconds after it opened,
the proxy forwards the first bytes of one WRITE of at least 64 KiB and drops
the rest, then carries on with the Mac's next requests: what macOS puts on the
wire when its 5 s send timeout cuts a WRITE short (FB25065375). The Mac then
reconnects through the proxy, and the next connection gets the next cut. dd
writers fill a sparse bundle on the share (`--jobs`); `--direct` adds writers
straight onto the share and `--small` a small-file create/rename/delete loop,
so other traffic shares the connection. Every request and reply is logged to
DIR/proxy.jsonl, the Mac's smbfs kernel lines to DIR/mac_kernel.log and smbd's
tc_desync lines to DIR/run.log.

For each cut connection the analysis reports who ended it and when, what the
Mac sent after the cut, and which requests were never answered. It fails when
a cut connection never ended, when the Mac gave up on one before smbd ended it
(the WRITE never
completed and smbd never noticed), when smbd logged ending one at 0072's 60 s
cap (DIR/smbd.log), or when a request macOS refuses to reconnect a Time
Machine mount with (a CREATE, CLOSE, LOCK, SET_INFO or IOCTL not in a
CREATE...CLOSE compound) was left unanswered when the connection ended. Needs the device's smbd to be the build under
test (tests.samba.swap_smbd; run.log records which smbd ran) and nothing else
of the user's on that share's smbd; the test's files live in __tc_cut_device__
and are removed at the end. Run one at a time on an otherwise idle device:
smbd.log holds every connection's tc_desync lines.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

STATUS_PENDING = 0x103
FLAGS_ASYNC = 0x2
CANCEL, NOTIFY = 0x0C, 0x0F
NAMES = {0: "NEGOTIATE", 1: "SESSSETUP", 2: "LOGOFF", 3: "TCON", 4: "TDIS", 5: "CREATE", 6: "CLOSE",
         7: "FLUSH", 8: "READ", 9: "WRITE", 10: "LOCK", 11: "IOCTL", 12: "CANCEL", 13: "ECHO", 14: "FIND",
         15: "NOTIFY", 16: "GETINFO", 17: "SETINFO", 18: "BREAK"}
# By opcode: macOS's list is narrower for SET_INFO and IOCTL (some classes only).
NON_IDEMPOTENT = {5, 6, 10, 11, 17}
MIN_CUT = 65536
TEST_DIR = "__tc_cut_device__"
# smbd's line when 0072's cap, not an idle moment, ended a connection.
CAPPED = re.compile(r"tc_desync: ending the connection [0-9.]+ s after the short WRITE with \d+ requests pending")


def headers(body: bytes) -> list[tuple[int, int, int, int]] | None:
    """(command, message ID, flags, status) of each SMB2 PDU in an NBT frame's
    payload; None if it is not plain SMB2 (encrypted, SMB1, truncated)."""
    out, off = [], 0
    while True:
        if body[off:off + 4] != b"\xfeSMB" or len(body) < off + 64:
            return None
        status, cmd = struct.unpack_from("<IH", body, off + 8)
        flags, nxt, mid = struct.unpack_from("<IIQ", body, off + 16)
        out.append((cmd, mid, flags, status))
        if not nxt:
            return out
        off += nxt


def non_idempotent(commands: list[int]) -> bool:
    """Whether macOS counts a frame with these commands as non-idempotent: a
    compound that opens and closes is not."""
    return bool(set(commands) & NON_IDEMPOTENT) and not {5, 6} <= set(commands)


class Proxy:
    """Forwards 127.0.0.1:port to host:server_port and cuts one large WRITE per
    connection, settle seconds after it opened, while fewer than max_cuts were
    made. A FIN from either side is passed on as a FIN. Port 0 takes a free
    port; self.port is the one listening once start() returns."""

    def __init__(self, host: str, log_path: Path, port: int = 4450, server_port: int = 445,
                 settle: float = 30.0, max_cuts: int = 8, seed: int = 397) -> None:
        self.host, self.port, self.server_port = host, port, server_port
        self.settle, self.max_cuts = settle, max_cuts
        self.cuts = 0
        self.conns = 0
        self.cut_conns: set[int] = set()
        self.closed: set[int] = set()
        self.armed = True
        self.rng = random.Random(seed)
        self.log = open(log_path, "a", buffering=1)
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def ev(self, **kw) -> None:
        kw["t"] = round(time.time(), 4)
        with self.lock:
            self.log.write(json.dumps(kw, separators=(",", ":")) + "\n")

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError(f"proxy did not start on 127.0.0.1:{self.port}")

    def stop(self) -> None:
        """Close the listener and every connection, then the loop and the log."""
        async def shut() -> None:
            self.server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.thread.is_alive():
            asyncio.run_coroutine_threadsafe(shut(), self.loop).result(10)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
        self.log.close()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self.handle, "127.0.0.1", self.port))
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    def cuts_done(self) -> bool:
        """Every cut was made and each cut connection has ended."""
        return self.cuts >= self.max_cuts and self.cut_conns <= self.closed

    def _cut_here(self, nbt: bytes, body: bytes, hs, state: dict, opened: float) -> bool:
        return (nbt[0] == 0 and hs is not None and len(hs) == 1 and hs[0][0] == 9 and len(body) >= MIN_CUT
                and not state["cut"] and self.armed and self.cuts < self.max_cuts
                and time.time() - opened >= self.settle)

    async def handle(self, cr, cw) -> None:
        self.conns += 1
        n = self.conns
        try:
            sr, sw = await asyncio.open_connection(self.host, self.server_port)
        except OSError as e:
            self.ev(ev="connect_failed", conn=n, err=repr(e))
            cw.close()
            return
        for w in (cw, sw):
            w.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        opened = time.time()
        self.ev(ev="open", conn=n)
        state = {"cut": False}

        async def c2s() -> None:
            try:
                while True:
                    nbt = await cr.readexactly(4)
                    body = await cr.readexactly(int.from_bytes(nbt[1:], "big"))
                    hs = headers(body) if nbt[0] == 0 else None
                    if self._cut_here(nbt, body, hs, state, opened):
                        # At least a header of the next request lands in the
                        # data, and at least 4 KiB of the WRITE is missing.
                        keep = self.rng.randint(64 + 48 + 4096, len(body) - 4096)
                        state["cut"] = True
                        self.cuts += 1
                        self.cut_conns.add(n)
                        sw.write(nbt + body[:keep])
                        self.ev(ev="cut", conn=n, mid=hs[0][1], len=len(body), keep=keep)
                    else:
                        sw.write(nbt + body)
                        self.ev(ev="req", conn=n, h=[(c, m) for c, m, _, _ in hs] if hs else None,
                                len=len(body))
                    await sw.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                self.ev(ev="c_eof", conn=n, err=type(e).__name__)
            try:
                sw.write_eof()
            except (OSError, RuntimeError):
                pass

        async def s2c() -> None:
            try:
                while True:
                    nbt = await sr.readexactly(4)
                    body = await sr.readexactly(int.from_bytes(nbt[1:], "big"))
                    cw.write(nbt + body)
                    hs = headers(body) if nbt[0] == 0 else None
                    # (message ID, final: not an interim STATUS_PENDING, command)
                    self.ev(ev="rsp", conn=n,
                            r=[(m, int(not (s == STATUS_PENDING and f & FLAGS_ASYNC)), c) for c, m, f, s in hs]
                            if hs else None)
                    await cw.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                self.ev(ev="s_eof", conn=n, err=type(e).__name__)
            try:
                cw.write_eof()
            except (OSError, RuntimeError):
                pass

        await asyncio.gather(c2s(), s2c())
        for w in (cw, sw):
            w.close()
        self.closed.add(n)
        self.ev(ev="closed", conn=n)


def analyse(events: list[dict]) -> list[dict]:
    """One row per cut connection: who ended it and when (seconds after the
    cut), what the Mac sent after the cut, what was never answered (sent before
    the cut too; `at` is seconds from the cut), and the problems (`fail`)."""
    by_conn = collections.defaultdict(list)
    for e in events:
        by_conn[e["conn"]].append(e)
    rows = []
    for conn, es in sorted(by_conn.items()):
        cut = next((e for e in es if e["ev"] == "cut"), None)
        if cut is None:
            continue
        t0 = cut["t"]
        done = set()
        for e in es:
            if e["ev"] == "rsp" and e["r"]:
                done.update(m for m, final, _ in e["r"] if final)
        s_eof = next((e["t"] for e in es if e["ev"] == "s_eof"), None)
        c_eof = next((e["t"] for e in es if e["ev"] == "c_eof"), None)
        end = min(t for t in (s_eof, c_eof) if t is not None) if (s_eof or c_eof) else None
        after = collections.Counter()
        unanswered, refused = [], []
        for e in es:
            if e["ev"] != "req" or not e["h"] or (end is not None and e["t"] > end):
                continue
            commands = [c for c, _ in e["h"]]
            if e["t"] >= t0:
                after.update(NAMES.get(c, str(c)) for c in commands)
            missing = [c for c, m in e["h"] if c not in (CANCEL, NOTIFY) and m not in done]
            if missing:
                unanswered.append({"at": round(e["t"] - t0, 3), "commands": [NAMES.get(c, str(c)) for c in missing]})
                if non_idempotent(commands):
                    refused.append(unanswered[-1])
        # Whoever closed first; the other side closes in reply within ms.
        ended_by = None if end is None else "smbd" if s_eof is not None and (c_eof is None or s_eof <= c_eof) \
            else "client"
        ended = None if end is None else round(end - t0, 3)
        fail = []
        if ended_by is None:
            fail.append("the connection never ended")
        if ended_by == "client":
            fail.append("the client gave up before smbd ended the connection")
        if refused:
            fail.append("a request macOS will not reconnect a Time Machine mount with was unanswered")
        rows.append(dict(conn=conn, mid=cut["mid"], len=cut["len"], missing=cut["len"] - cut["keep"],
                         ended_by=ended_by, ended=ended, after=dict(after), unanswered=unanswered, fail=fail))
    return rows


def capped(smbd_log: str) -> int:
    """Connections smbd ended at 0072's cap, from its tc_desync lines."""
    return len(CAPPED.findall(smbd_log))


def summary(rows: list[dict], capped_endings: int = 0) -> str:
    lines = []
    for r in rows:
        lines.append(f"conn {r['conn']}: WRITE mid {r['mid']}, {r['missing']} of {r['len']} bytes cut; "
                     f"ended by {r['ended_by']} after {r['ended']} s; sent after: {r['after']}; "
                     f"unanswered: {r['unanswered'] or 'none'}" + (f"; FAIL: {'; '.join(r['fail'])}" if r["fail"] else ""))
    if capped_endings:
        lines.append(f"FAIL: smbd ended {capped_endings} connection(s) at its 60 s cap")
    failed = sum(1 for r in rows if r["fail"]) + capped_endings
    lines.append(f"RESULT cuts={len(rows)} fail={failed}")
    return "\n".join(lines)


def load(out: Path) -> list[dict]:
    return [json.loads(line) for line in open(out / "proxy.jsonl")]


def run(args) -> int:
    from timecapsulesmb.core.config import parse_env_file
    from tests.samba.links_device import Device, mount, unmount
    from tests.samba.swap_smbd import Swapper

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "run.log", "a", buffering=1)
    dev = Device(parse_env_file(Path(args.env)), args.share)
    smbd_log = dev.sh("ls /Volumes/*/.samba4/logs/log.smbd").split()[0]
    t0 = time.time()

    def p(*a) -> None:
        line = f"{time.time() - t0:7.1f} [{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
        print(line, flush=True)
        logf.write(line + "\n")

    # Which smbd this run tests, for the record.
    tested = Swapper(dev).status()
    p("smbd under test:", tested)
    if tested.startswith("deployed"):
        p("WARNING: the deployed smbd is running; swap the build under test in with tests.samba.swap_smbd")
    off0 = int(dev.sh(f"ls -l {smbd_log}").split()[4])
    proxy = Proxy(dev.host, out / "proxy.jsonl", port=args.port, settle=args.settle, max_cuts=args.cuts)
    proxy.start()
    kern = subprocess.Popen(["/usr/bin/log", "stream", "--style", "compact", "--level", "debug", "--predicate",
                             'process == "kernel" AND (eventMessage CONTAINS[c] "smb")'],
                            stdout=open(out / "mac_kernel.log", "a"), stderr=subprocess.STDOUT)
    mnt, img = out / "mnt", out / "img"
    mnt.mkdir(exist_ok=True)
    img.mkdir(exist_ok=True)
    try:
        # The Mac's own data caching stays on: its write-behind is part of what is tested.
        mount("smb", dev, mnt, server=f"127.0.0.1:{proxy.port}", options="nobrowse,forcenewsession")
    except RuntimeError as e:
        proxy.stop()
        kern.terminate()
        raise SystemExit(str(e))
    d = mnt / TEST_DIR
    # A run that did not finish leaves its sparse bundle behind.
    subprocess.run(["rm", "-rf", str(d)])
    d.mkdir()
    bundle = d / "t.sparsebundle"
    writers: list[subprocess.Popen] = []
    stop = threading.Event()
    small = collections.Counter()

    def small_loop() -> None:
        sd = d / "small"
        sd.mkdir(exist_ok=True)
        i = 0
        while not stop.is_set():
            a, b = sd / f"f{i % 50}", sd / f"g{i % 50}"
            try:
                a.write_bytes(os.urandom(4096))
                a.rename(b)
                b.unlink()
                small["ok"] += 1
            except OSError as e:
                small[f"errno {e.errno}"] += 1
                time.sleep(1)
            i += 1
            time.sleep(0.2)

    def dd(path: Path) -> subprocess.Popen:
        return subprocess.Popen(["dd", "if=/dev/zero", f"of={path}", "bs=1m", f"count={args.mb}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    try:
        r = subprocess.run(["hdiutil", "create", "-size", "200g", "-type", "SPARSEBUNDLE", "-fs", "HFS+J",
                            "-volname", "tccut", str(bundle)], capture_output=True, text=True)
        p("hdiutil create", r.returncode, r.stderr.strip()[-200:])
        r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-owners", "off", "-mountpoint", str(img),
                            str(bundle)], capture_output=True, text=True)
        p("hdiutil attach", r.returncode, r.stderr.strip()[-200:])
        if r.returncode:
            raise SystemExit("hdiutil attach failed")
        writers = [dd(img / f"big{i}") for i in range(args.jobs)] + [dd(d / f"direct{i}") for i in range(args.direct)]
        if args.small:
            threading.Thread(target=small_loop, daemon=True).start()
        seen = 0
        while time.time() - t0 < args.limit:
            time.sleep(5)
            if proxy.cuts != seen:
                seen = proxy.cuts
                p(f"cut {seen} of {args.cuts}; connections {proxy.conns}")
            if proxy.cuts_done():
                break
            if all(w.poll() is not None for w in writers):
                p("every writer finished before the cuts were made; raise --mb")
                break
        else:
            p(f"stopped after --limit {args.limit:.0f} s")
    finally:
        # Cleanup must finish: a second Ctrl-C would leave a mount on a dead proxy.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        p(f"cleaning up; Ctrl-C is ignored until it is done (kill -9 {os.getpid()} if it hangs, "
          f"then umount -f {mnt})")
        stop.set()
        proxy.armed = False
        for w in writers:
            if w.poll() is None:
                w.kill()
        for w in writers:
            w.wait()
        if small:
            p("small-file loop:", dict(small))
        lines = dev.sh(f"dd if={smbd_log} bs=65536 skip={off0 // 65536} 2>/dev/null | "
                       "sed -n -e '/tc_desync/p' -e '/bad message_id/p'", check=False)
        (out / "smbd.log").write_text(lines)
        p("smbd:\n" + (lines.strip() or "(nothing)"))
        r = subprocess.run(["hdiutil", "detach", "-force", str(img)], capture_output=True, text=True)
        p("hdiutil detach", r.returncode, r.stderr.strip()[-200:])
        subprocess.run(["rm", "-rf", str(d)])
        unmount(mnt)
        time.sleep(3)
        kern.terminate()
        proxy.stop()
    return report(out)


def report(out: Path) -> int:
    rows = analyse(load(out))
    smbd = out / "smbd.log"
    caps = capped(smbd.read_text(errors="replace")) if smbd.exists() else 0
    (out / "analysis.json").write_text(json.dumps(rows, indent=1))
    print(summary(rows, caps))
    if not smbd.exists():
        print("NOTE: no smbd.log, so endings at the cap are not counted")
    return 1 if not rows or caps or any(r["fail"] for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", help="device .env file")
    ap.add_argument("--share", help="share to mount (default: the env file's)")
    ap.add_argument("--out", required=True, help="directory for the logs and analysis.json")
    ap.add_argument("--cuts", type=int, default=8, help="cut WRITEs, one per connection")
    ap.add_argument("--settle", type=float, default=30, help="seconds into each connection before its cut")
    ap.add_argument("--jobs", type=int, default=3, help="dd writers into the sparse bundle")
    ap.add_argument("--direct", type=int, default=0, help="dd writers straight onto the share")
    ap.add_argument("--small", action="store_true", help="also create, rename and delete small files")
    ap.add_argument("--mb", type=int, default=4000, help="MiB each writer writes at most")
    ap.add_argument("--port", type=int, default=4450, help="the proxy's port on 127.0.0.1")
    ap.add_argument("--limit", type=float, default=1800, help="seconds to run at most")
    ap.add_argument("--analyse", action="store_true", help="only analyse an earlier run's --out")
    args = ap.parse_args(argv)
    if args.analyse:
        return report(Path(args.out))
    if not args.env:
        ap.error("--env is required to run")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
