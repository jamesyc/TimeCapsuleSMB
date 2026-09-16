#!/usr/bin/env python3
"""Sanitize a raw ``sysctl(NET_RT_IFLIST)`` dump into a committed fixture.

The raw dumps come from the maintainer-only ``plan/`` directory (see
``plan/spike-2026-09-16/iflist.c``). Committed fixtures must not carry real
MACs or public addresses, but they must preserve everything the parser is
tested on: message layout, interface indexes, address ownership, prefixes,
subnet membership, and the relationship between a MAC and the EUI-64 IID of
its fe80/GUA addresses. This converter rewrites bytes in place (never changes
lengths) and stamps a JSON manifest with the source hash and the decoded,
expected table that ``tests/native/cases/iflist_*.c`` assert against.

Usage: sanitize_iflist.py <raw.bin> <manifest-in.json> <out.bin> <out.json>

The input manifest supplies provenance fields and the sanitization maps:

    {"firmware": "7.8.1", "lane": "netbsd4le", "device": "...",
     "plan_section": "v3.1-implementation-guide F4",
     "expectation": "observed", "completeness": "complete",
     "mac_map": {"e8:8d:28:58:f1:5c": "02:00:00:00:00:01"},
     "ipv4_map": {"192.168.1.0/24": "192.0.2.0/24"},
     "ipv6_prefix_map": {"2600:1700:83b7:20f::/64": "2001:db8:0:1::/64"}}
"""
import hashlib
import ipaddress
import json
import struct
import sys

AF_INET = 2
AF_LINK = 18
AF_INET6 = 24
RTM_NEWADDR = 0xC
RTA_NETMASK = 1 << 2
RTA_IFA = 1 << 5


def layout(version):
    """Per-RTM_VERSION layout facts (verified on both device lanes, 2026-09-16).

    version 3 = NetBSD 4: RTM_IFINFO 0xf, ifa_msghdr 20 bytes, ifam_index at
    12, RT_ROUNDUP 4. version 4 = NetBSD 6/7: RTM_IFINFO 0x14, ifa_msghdr 24
    bytes (index at 16, __align64), RT_ROUNDUP 8.
    """
    if version == 3:
        return {"ifinfo": 0xF, "ifa_hdr": 20, "ifam_index": 12, "roundup": 4}
    if version == 4:
        return {"ifinfo": 0x14, "ifa_hdr": 24, "ifam_index": 16, "roundup": 8}
    raise ValueError(f"unsupported RTM_VERSION {version}")


def roundup(n, unit):
    return max(unit, (n + unit - 1) & ~(unit - 1))


def mac_to_iid(mac):
    b = bytes(int(x, 16) for x in mac.split(":"))
    b0 = b[0] ^ 0x02
    return bytes([b0, b[1], b[2], 0xFF, 0xFE, b[3], b[4], b[5]])


def prefix_from_mask(raw):
    n = 0
    for byte in raw:
        for bit in range(7, -1, -1):
            if byte >> bit & 1:
                n += 1
            else:
                return n
    return n


def sanitize(raw, manifest):
    buf = bytearray(raw)
    mac_map = {k.lower(): v.lower() for k, v in manifest.get("mac_map", {}).items()}
    iid_map = {mac_to_iid(k): mac_to_iid(v) for k, v in mac_map.items()}
    ipv4_map = [(ipaddress.ip_network(k), ipaddress.ip_network(v)) for k, v in manifest.get("ipv4_map", {}).items()]
    ipv6_map = [(ipaddress.ip_network(k), ipaddress.ip_network(v)) for k, v in manifest.get("ipv6_prefix_map", {}).items()]
    links = []
    addrs = []

    def map_v4(b):
        addr = ipaddress.ip_address(bytes(b))
        for src, dst in ipv4_map:
            if addr in src:
                return ipaddress.ip_address(int(dst.network_address) | (int(addr) & ~int(src.netmask))).packed
        return bytes(b)

    def map_v6(b):
        addr = ipaddress.ip_address(bytes(b))
        out = bytearray(addr.packed)
        for src, dst in ipv6_map:
            if addr in src:
                out = bytearray(ipaddress.ip_address(int(dst.network_address) | (int(addr) & ~int(src.netmask))).packed)
        iid = bytes(out[8:16])
        if iid in iid_map:
            out[8:16] = iid_map[iid]
        return bytes(out)

    p = 0
    while p < len(buf):
        msglen, version, mtype = struct.unpack_from("<HBB", buf, p)
        lay = layout(version)
        if mtype == lay["ifinfo"]:
            index = struct.unpack_from("<H", buf, p + 12)[0]
            flags = struct.unpack_from("<I", buf, p + 8)[0]
            entry = {"name": "", "index": index, "flags": flags}
            for q in range(16, msglen - 8):
                sdl_len = buf[p + q]
                if buf[p + q + 1] == AF_LINK and struct.unpack_from("<H", buf, p + q + 2)[0] == index and 8 <= sdl_len <= msglen - q:
                    nlen, alen = buf[p + q + 5], buf[p + q + 6]
                    entry["name"] = bytes(buf[p + q + 8:p + q + 8 + nlen]).decode()
                    if alen == 6:
                        mac_off = p + q + 8 + nlen
                        mac = bytes(buf[mac_off:mac_off + 6]).hex(":")
                        if mac in mac_map:
                            buf[mac_off:mac_off + 6] = bytes(int(x, 16) for x in mac_map[mac].split(":"))
                    break
            links.append(entry)
        elif mtype == RTM_NEWADDR:
            index = struct.unpack_from("<H", buf, p + lay["ifam_index"])[0]
            rta = struct.unpack_from("<i", buf, p + 4)[0]
            q = lay["ifa_hdr"]
            prefix = None
            record = None
            for bit in range(8):
                if not rta & (1 << bit):
                    continue
                sa_len, family = buf[p + q], buf[p + q + 1]
                if bit == 2:
                    if family == AF_INET6 and sa_len >= 24:
                        prefix = prefix_from_mask(buf[p + q + 8:p + q + 24])
                    elif sa_len > 4:
                        prefix = prefix_from_mask(buf[p + q + 4:p + q + sa_len])
                    else:
                        prefix = 0
                elif bit == 5 and family == AF_INET:
                    buf[p + q + 4:p + q + 8] = map_v4(buf[p + q + 4:p + q + 8])
                    record = {"owner_index": index, "family": "inet", "addr": str(ipaddress.ip_address(bytes(buf[p + q + 4:p + q + 8])))}
                elif bit == 5 and family == AF_INET6:
                    buf[p + q + 8:p + q + 24] = map_v6(buf[p + q + 8:p + q + 24])
                    raw6 = bytearray(buf[p + q + 8:p + q + 24])
                    scope = 0
                    if raw6[0] == 0xFE and raw6[1] & 0xC0 == 0x80:
                        scope = raw6[2] << 8 | raw6[3]
                        raw6[2] = raw6[3] = 0
                    record = {"owner_index": index, "family": "inet6", "addr": str(ipaddress.ip_address(bytes(raw6))), "scope": scope}
                elif bit == 7 and family == AF_INET:
                    buf[p + q + 4:p + q + 8] = map_v4(buf[p + q + 4:p + q + 8])
                elif bit == 7 and family == AF_INET6:
                    buf[p + q + 8:p + q + 24] = map_v6(buf[p + q + 8:p + q + 24])
                q += roundup(sa_len, lay["roundup"])
            if record is not None:
                record["prefix"] = prefix
                addrs.append(record)
        p += msglen
    return bytes(buf), links, addrs


def main(argv):
    raw_path, manifest_in, out_bin, out_json = argv[1:5]
    raw = open(raw_path, "rb").read()
    manifest = json.load(open(manifest_in))
    if not raw:
        raise SystemExit(f"{raw_path}: empty source; refusing to emit a fixture")
    out, links, addrs = sanitize(raw, manifest)
    version = out[2]
    stamped = {
        "source_path": raw_path,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_bytes": len(raw),
        "fixture_sha256": hashlib.sha256(out).hexdigest(),
        "rtm_version": version,
        "layout": layout(version),
        **{k: manifest[k] for k in ("firmware", "lane", "device", "plan_section", "expectation", "completeness", "observer") if k in manifest},
        "sanitization": {k: manifest[k] for k in ("mac_map", "ipv4_map", "ipv6_prefix_map") if k in manifest},
        "expected": {"links": links, "addrs": addrs},
    }
    open(out_bin, "wb").write(out)
    with open(out_json, "w") as fp:
        json.dump(stamped, fp, indent=2)
        fp.write("\n")


if __name__ == "__main__":
    main(sys.argv)
