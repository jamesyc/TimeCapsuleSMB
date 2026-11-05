"""Samba patch 0043's interface list parser (overlay
lib/socket/tc_netbsd_iflist.c), fed the NET_RT_IFLIST dumps captured on both
devices: every address smbd should list, with the netmask the device has."""
import ipaddress
import json
from pathlib import Path
import subprocess

import pytest

from tests.native.build import compile_program

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/native/fixtures/iflist"
OVERLAY = ROOT / "build/patches/samba4x/overlay/lib/socket"
UNIT = Path(__file__).parent / "unit"


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    output = tmp_path_factory.mktemp("samba_iflist") / "samba_iflist"
    return compile_program(output, "-I", UNIT / "samba_iflist_shim", "-I", OVERLAY,
                           UNIT / "test_samba_iflist.c")


def expected(fixture):
    """What smbd lists from the fixture: addresses of named links that are up,
    IPv4 only on broadcast or loopback links, without IPv6 link-local and
    v4-compatible addresses (Samba's getifaddrs policy)."""
    manifest = json.loads((FIXTURES / f"{fixture}.json").read_text())
    links = {link["index"]: link for link in manifest["expected"]["links"]}
    rows = []
    for addr in manifest["expected"]["addrs"]:
        link = links[addr["owner_index"]]
        flags = link["flags"]
        if not flags & 0x1:
            continue
        if addr["family"] == "inet" and not flags & (0x2 | 0x8):
            continue
        ip = ipaddress.ip_address(addr["addr"])
        if ip.version == 6 and (ip.is_link_local or (int(ip) >> 32 == 0 and int(ip) > 1)):
            continue
        rows.append((link["name"], link["index"], addr["family"], addr["addr"], addr["prefix"]))
    return rows


@pytest.mark.parametrize("fixture", ["netbsd4le-bridge", "netbsd6-bridge"])
def test_iflist_fixture_netmasks(binary, fixture):
    run = subprocess.run([str(binary), str(FIXTURES / f"{fixture}.bin")],
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    want = expected(fixture)
    assert lines[0] == f"count {len(want)}"
    got = [line.split()[1:] for line in lines[1:]]
    assert [(name, int(index), family, addr, int(prefix)) for name, index, family, addr, prefix in got] == want
    # Both kinds of netmask the device sends: a /24 and a /16 IPv4 mask, and a
    # /64 IPv6 mask, each trimmed of its trailing zero bytes.
    prefixes = {(family, prefix) for _, _, family, _, prefix in want}
    assert {("inet", 24), ("inet", 16), ("inet6", 64)} <= prefixes


def widen_ipv4_netmasks(dump):
    """The dump with every IPv4 netmask sent whole, as a 16-byte sockaddr_in
    whose family stays as the kernel left it, instead of trimmed. Its length
    then looks like an IPv6 mask's; the parser must still read it by the
    address's family. Returns the new dump and how many masks it widened."""
    out, offset, widened = bytearray(), 0, 0
    while offset + 4 <= len(dump):
        msglen = int.from_bytes(dump[offset:offset + 2], "little")
        message = dump[offset:offset + msglen]
        version, kind = message[2], message[3]
        if kind == 0xC and version in (3, 4):
            header, roundup = (20, 4) if version == 3 else (24, 8)
            addrs = int.from_bytes(message[4:8], "little")
            position, parts = header, []
            for bit in range(8):
                if addrs & (1 << bit):
                    length = message[position]
                    used = roundup if length == 0 else -(-length // roundup) * roundup
                    parts.append((bit, message[position:position + used]))
                    position += used
            found = dict(parts)
            mask, address = found.get(2), found.get(5)
            if address is not None and address[1] == 2 and mask is not None and mask[0] < 16:
                whole = bytes([16, mask[1], 0, 0]) + (mask[4:mask[0]] + bytes(4))[:4] + bytes(8)
                body = b"".join(whole if bit == 2 else part for bit, part in parts)
                message = message[:header] + body + message[position:]
                message = len(message).to_bytes(2, "little") + message[2:]
                widened += 1
        out += message
        offset += msglen
    return bytes(out), widened


@pytest.mark.parametrize("fixture", ["netbsd4le-bridge", "netbsd6-bridge"])
def test_iflist_untrimmed_ipv4_netmask(binary, fixture, tmp_path):
    """A whole IPv4 netmask is longer than an IPv6 one's address offset; a
    parser that guessed the family from the length would read its zero
    padding and list a /0."""
    dump, widened = widen_ipv4_netmasks((FIXTURES / f"{fixture}.bin").read_bytes())
    assert widened > 0
    path = tmp_path / "widened.bin"
    path.write_bytes(dump)
    run = subprocess.run([str(binary), str(path)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    got = [line.split()[1:] for line in run.stdout.splitlines()[1:]]
    assert [(name, int(index), family, addr, int(prefix)) for name, index, family, addr, prefix in got] == expected(fixture)
