#!/usr/bin/env python3
"""Report fake native sockets for discovery's bounded owner inspection."""
import os
import signal
import sys
import time
from pathlib import Path

pid = int(sys.argv[-1])
mode = Path(os.environ["TC_FAKE_FSTAT_MODE"]).read_text().strip()
with open(os.environ["TC_FAKE_WCIFSND_EVENTS"], "a", encoding="ascii") as stream:
    stream.write(f"FSTAT {pid}\n")
if mode == "error":
    raise SystemExit(1)
if mode == "kill":
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.05)
if mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.environ["TC_FAKE_WCIFSND_EVENTS"], "a", encoding="ascii") as stream:
        stream.write(f"HANG {os.getpid()}\n")
    time.sleep(60)
owner = pid + 1 if mode == "foreign" else pid
ports = (137, 138) if mode == "missing-control" else (137, 138, int(os.environ["TC_FAKE_WCIFSND_PORT"]))
for index, port in enumerate(ports, 3):
    print(f"root wcifsnd {owner} {index}* internet dgram udp *:{port}")
