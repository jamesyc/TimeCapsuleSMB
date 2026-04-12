from __future__ import annotations

import socket
import struct
from typing import Optional

from timecapsulesmb.checks.models import CheckResult


NBNS_PORT = 137
NB_TYPE_NB = 0x0020
DNS_CLASS_IN = 0x0001


def encode_netbios_name(name: str, suffix: int = 0x20) -> bytes:
    raw = (name.upper()[:15].ljust(15) + chr(suffix)).encode("latin-1")
    encoded = bytearray()
    for value in raw:
        encoded.append(ord("A") + ((value >> 4) & 0x0F))
        encoded.append(ord("A") + (value & 0x0F))
    return bytes([32]) + bytes(encoded) + b"\x00"


def build_nbns_query(name: str, transaction_id: int = 0x1337) -> bytes:
    question_name = encode_netbios_name(name)
    header = struct.pack("!HHHHHH", transaction_id, 0x0000, 1, 0, 0, 0)
    question = question_name + struct.pack("!HH", NB_TYPE_NB, DNS_CLASS_IN)
    return header + question


def _skip_name(packet: bytes, offset: int) -> int:
    while offset < len(packet):
        length = packet[offset]
        if length == 0:
            return offset + 1
        if (length & 0xC0) == 0xC0:
            return offset + 2
        offset += 1 + length
    raise ValueError("truncated NBNS name")


def extract_nbns_response_ip(packet: bytes) -> Optional[str]:
    if len(packet) < 12:
        return None

    _, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if (flags & 0x8000) == 0 or ancount < 1:
        return None

    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(packet, offset)
        offset += 4
        if offset > len(packet):
            return None

    offset = _skip_name(packet, offset)
    if offset + 10 > len(packet):
        return None

    rtype, rclass, _ttl, rdlength = struct.unpack("!HHIH", packet[offset : offset + 10])
    offset += 10
    if rtype != NB_TYPE_NB or rclass != DNS_CLASS_IN or rdlength < 6 or offset + rdlength > len(packet):
        return None

    return socket.inet_ntoa(packet[offset + 2 : offset + 6])


def check_nbns_name_resolution(netbios_name: str, target_host: str, expected_ip: str, *, timeout: float = 2.0) -> CheckResult:
    query = build_nbns_query(netbios_name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (target_host, NBNS_PORT))
        packet, _ = sock.recvfrom(576)
    except TimeoutError:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} timed out against {target_host}:137")
    except OSError as exc:
        return CheckResult("FAIL", f"NBNS query failed: {exc}")
    finally:
        sock.close()

    actual_ip = extract_nbns_response_ip(packet)
    if actual_ip is None:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} returned an invalid response")
    if actual_ip != expected_ip:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} resolved to {actual_ip}, expected {expected_ip}")
    return CheckResult("PASS", f"NBNS query for {netbios_name!r} resolved to {actual_ip}")
