from __future__ import annotations

import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
import ipaddress

from timecapsulesmb.checks.models import CheckResult

if TYPE_CHECKING:
    from timecapsulesmb.device.probe import DeviceIpv4Entry


NBNS_PORT = 137
NB_TYPE_NB = 0x0020
DNS_CLASS_IN = 0x0001
NBNS_QUERY_TIMEOUT_CODE = "nbns_query_timeout"
NBNS_OFF_SUBNET_CODE = "nbns_off_subnet"
NBNS_NEGATIVE_RESPONSE_CODE = "nbns_negative_response"


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


@dataclass(frozen=True)
class NbnsResponse:
    rcode: int
    addresses: tuple[str, ...] = ()


def parse_nbns_response(packet: bytes) -> Optional[NbnsResponse]:
    """Parse a name query response; None when it is not a well-formed one.

    Apple's wcifsnd answers with flags 0x8500, no question, the full owner
    name, TTL 0 and one 6-byte NB entry (flags + IPv4) per registered
    interface, so a router-mode device lists its WAN and LAN addresses.
    """
    if len(packet) < 12:
        return None

    try:
        _, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", packet[:12])
        if (flags & 0x8000) == 0 or (flags & 0x7800) != 0:
            return None
        rcode = flags & 0x000F
        if rcode:
            return NbnsResponse(rcode)
        if ancount < 1:
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
        if rtype != NB_TYPE_NB or rclass != DNS_CLASS_IN or offset + rdlength > len(packet):
            return None
        if rdlength == 0 or rdlength % 6:
            return None
        return NbnsResponse(0, tuple(
            socket.inet_ntoa(packet[entry + 2 : entry + 6])
            for entry in range(offset, offset + rdlength, 6)
        ))
    except (IndexError, OSError, ValueError, struct.error):
        return None


def apple_nbns_client_on_subnet(entries: Iterable[DeviceIpv4Entry], client_ip: str) -> bool:
    """Return whether Apple's wcifsnd treats `client_ip` as on one of its subnets.

    For each query wcifsnd picks the interface whose stored broadcast equals
    `(src & mask) | ~mask` (NetBSD 6 `0x486178`). With no match it falls back
    to a default context whose reply socket is the UDP 922 control socket
    (`0x4823f4..0x482420`), and `0x484b00` replies from that socket, so a
    client off every subnet may never see the answer.
    """
    client = int(ipaddress.IPv4Address(client_ip))
    for entry in entries:
        mask = int(ipaddress.IPv4Address(entry.netmask))
        if (client & mask) | (~mask & 0xFFFFFFFF) == int(ipaddress.IPv4Address(entry.broadcast)):
            return True
    return False


def check_nbns_name_resolution(netbios_name: str, target_host: str, expected_ip: str, *, timeout: float = 2.0) -> CheckResult:
    query = build_nbns_query(netbios_name)
    try:
        expected_addr = ipaddress.ip_address(expected_ip)
    except ValueError:
        return CheckResult("FAIL", f"NBNS check expected IP is invalid: {expected_ip}")
    expected_ip = str(expected_addr)
    if expected_addr.version != 4:
        return CheckResult("FAIL", f"NBNS only supports IPv4 addresses, got {expected_ip}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (target_host, NBNS_PORT))
        packet, _ = sock.recvfrom(1024)
    except TimeoutError:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} timed out against {target_host}:137",
                           {"code": NBNS_QUERY_TIMEOUT_CODE})
    except OSError as exc:
        return CheckResult("FAIL", f"NBNS query failed: {exc}")
    finally:
        sock.close()

    response = parse_nbns_response(packet)
    if response is None:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} returned an invalid response")
    if response.rcode:
        return CheckResult(
            "FAIL",
            f"NBNS query for {netbios_name!r} returned a negative response (rcode {response.rcode})",
            {"code": NBNS_NEGATIVE_RESPONSE_CODE, "rcode": response.rcode},
        )
    if expected_ip not in response.addresses:
        return CheckResult("FAIL", f"NBNS query for {netbios_name!r} resolved to {', '.join(response.addresses)}, expected {expected_ip}")
    others = [address for address in response.addresses if address != expected_ip]
    also = f" (also lists {', '.join(others)})" if others else ""
    return CheckResult("PASS", f"NBNS query for {netbios_name!r} resolved to {expected_ip}{also}")
