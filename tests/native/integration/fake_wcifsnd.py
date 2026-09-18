#!/usr/bin/env python3
"""Test-only stand-in for Apple's wcifsnd UDP control listener."""
import os
import signal
import socket
import struct


PORT = int(os.environ["TC_FAKE_WCIFSND_PORT"])
EVENTS = os.environ["TC_FAKE_WCIFSND_EVENTS"]
MODE = os.environ["TC_FAKE_WCIFSND_MODE"]
running = True


def record(text):
    with open(EVENTS, "a", encoding="ascii") as stream:
        stream.write(text + "\n")


def stop(_signo, _frame):
    global running
    running = False


def hup(_signo, _frame):
    record("HUP")


def reply(request, flags, rdlength, size):
    packet = bytearray(size)
    packet[:2] = request[:2]
    struct.pack_into(">HHHHH", packet, 2, flags, 0, 1, 0, 0)
    packet[12:46] = request[12:46]
    packet[46:50] = request[46:50]
    struct.pack_into(">H", packet, 54, rdlength)
    return packet


sentinel = os.environ.get("TC_FAKE_WCIFSND_SENTINEL_FD")
if sentinel is not None:
    try:
        os.fstat(int(sentinel))
        record("FD_OPEN")
    except OSError:
        record("FD_CLOSED")
signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get("TC_FAKE_WCIFSND_IGNORE_TERM") else stop)
signal.signal(signal.SIGHUP, hup)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", PORT))
sock.settimeout(0.1)
record(f"START {os.getpid()}")
adds = 0
while running:
    try:
        request, peer = sock.recvfrom(512)
    except socket.timeout:
        continue
    record("ADD " + request.hex())
    adds += 1
    try:
        mode = open(MODE, encoding="ascii").read().strip()
    except FileNotFoundError:
        mode = "success"
    if mode == "drop" or (mode == "drop-after-1" and adds > 1) or (mode == "drop-after-2" and adds > 2):
        continue
    if mode == "wack":
        sock.sendto(reply(request, 0xB800, 2, 58), peer)
        continue
    if mode == "wack-success":
        sock.sendto(reply(request, 0xB800, 2, 58), peer)
    flags = 0xA805 if mode == "negative" else 0xA800
    sock.sendto(reply(request, flags, 6, 62), peer)
record("STOP")
