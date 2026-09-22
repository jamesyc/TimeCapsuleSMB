"""Discovery/telemetry plan: iflist parser, topology, policy and retention."""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.native.build import ROOT, compile_service
from tests.native.cases import run_case

FIXTURES = ROOT / "tests/native/fixtures/iflist"


def parse_kv_lines(text):
    """'kind: k=v k=v' lines -> list of (kind, {k: v}); quoted values keep spaces."""
    rows = []
    for line in text.splitlines():
        kind, _, rest = line.partition(": ") if ": " in line else line.partition(" ")
        fields = {}
        for token in shlex.split(rest):
            key, _, value = token.partition("=")
            fields[key] = value
        rows.append((kind.rstrip(":"), fields))
    return rows


# ---------------------------------------------------------------- iflist ----

@pytest.mark.parametrize("fixture", ["netbsd4le-bridge", "netbsd6-bridge"])
def test_iflist_parses_device_layout_fixture(fixture):
    manifest = json.loads((FIXTURES / f"{fixture}.json").read_text())
    rows = parse_kv_lines(run_case("iflist_parse", FIXTURES / f"{fixture}.bin"))
    assert rows[0] == ("parse=ok", {})
    links = [f for kind, f in rows if kind == "link"]
    addrs = [f for kind, f in rows if kind == "addr"]
    expected_links = manifest["expected"]["links"]
    expected_addrs = manifest["expected"]["addrs"]
    assert [(l["name"], int(l["index"])) for l in links] == [(l["name"], l["index"]) for l in expected_links]
    for got, want in zip(links, expected_links):
        assert int(got["flags"], 16) == want["flags"]
    assert len(addrs) == len(expected_addrs)
    for got, want in zip(addrs, expected_addrs):
        assert int(got["owner"]) == want["owner_index"]
        assert got["family"] == want["family"]
        assert got["addr"] == want["addr"]
        assert int(got["prefix"]) == want["prefix"]
        if want["family"] == "inet6":
            # fe80 carries the kernel's embedded index; everything else scopes to its owner.
            assert int(got["scope"]) == (want["scope"] or want["owner_index"])
            assert got["link_local"] == ("1" if want["addr"].startswith("fe80:") else "0")
    bridge = next(l for l in links if l["name"] == "bridge0")
    assert int(bridge["index"]) == {"netbsd4le-bridge": 9, "netbsd6-bridge": 13}[fixture]  # == ifconfig scopeid
    assert manifest["rtm_version"] == {"netbsd4le-bridge": 3, "netbsd6-bridge": 4}[fixture]


def test_iflist_layout_independence_sdk_header():
    rows = parse_kv_lines(run_case("iflist_parse", "--sdk-layout"))
    links = [f for kind, f in rows if kind == "link"]
    addrs = [f for kind, f in rows if kind == "addr"]
    assert [(l["name"], l["index"]) for l in links] == [("bridge0", "9"), ("bridge1", "10")]
    assert [(a["owner"], a["addr"], a["prefix"], a["scope"]) for a in addrs] == [("9", "192.0.2.10", "24", "9"), ("9", "fe80::ff:fe00:1", "64", "9")]


def test_iflist_version4_layout_skips_af_link_address_rows():
    rows = parse_kv_lines(run_case("iflist_parse", "--version6"))
    links = [f for kind, f in rows if kind == "link"]
    addrs = [f for kind, f in rows if kind == "addr"]
    assert [(l["name"], l["index"]) for l in links] == [("bridge0", "13")]
    assert [(a["owner"], a["addr"], a["prefix"]) for a in addrs] == [("13", "192.0.2.218", "24"), ("13", "fe80::82ea:96ff:fee6:5868", "64")]
    assert addrs[1]["scope"] == "13"


def test_iflist_link_without_sockaddr_dl_stays_unnamed():
    rows = parse_kv_lines(run_case("iflist_parse", "--unnamed"))
    links = [f for kind, f in rows if kind == "link"]
    assert links == [{"name": "", "index": "4", "flags": "0x8843"}]
    addrs = [f for kind, f in rows if kind == "addr"]
    assert addrs[0]["owner"] == "4" and addrs[0]["addr"] == "10.0.0.4"


def test_iflist_truncated_buffer_is_rejected_without_overread():
    assert run_case("iflist_parse", "--truncated").strip() == "parse=error"


@pytest.mark.parametrize("flag", ["--bad-sockaddr", "--bad-sockaddr6"])
def test_iflist_malformed_inner_sockaddr_marks_the_table_incomplete(flag):
    """Review 2 R6: a sockaddr whose length runs past its message used to be
    skipped silently, yielding a 'complete' table missing the LAN address."""
    rows = parse_kv_lines(run_case("iflist_parse", flag))
    assert rows[0] == ("parse=ok", {})
    assert rows[1][1]["truncated"] == "1"


def test_iflist_orphan_address_keeps_owner_index():
    rows = parse_kv_lines(run_case("iflist_parse", "--orphan-addr"))
    addrs = [f for kind, f in rows if kind == "addr"]
    assert addrs == [{"owner": "42", "family": "inet", "addr": "172.16.42.1", "prefix": "24", "scope": "42", "link_local": "0"}]


# ------------------------------------------------------------ facts files ----

def facts_text(*, acp=None, links=(), addrs=(), hostname="airport-time-capsule", config=None, iflist_ok=1):
    """Versioned facts file (native test-input format)."""
    acp = dict(acp or {})
    config = {"advertise_afp": 0, "debug_logging": 0, "netbios": "", "instance": "", **(config or {})}
    lines = ["facts: version=1"]
    for key in ("raNA", "raDS", "waNM", "usbF", "laIP", "waIP", "waLL", "gnRo", "syNm", "waMA"):
        value = acp.get(key)
        if value is None:
            lines.append(f"acp: key={key} status=unavailable value=")
        elif value == "ABORT":
            lines.append(f"acp: key={key} status=abort value=")
        else:
            lines.append(f"acp: key={key} status=ok value={value}")
    lines.append(f"hostname: {hostname}")
    lines.append("config: " + " ".join(f"{k}={v}" for k, v in config.items()))
    lines.append(f"iflist: ok={iflist_ok} truncated=0")
    for name, index in links:
        lines.append(f"link: name={name} index={index} flags=0xe043")
    for owner, addr, prefix in addrs:
        if ":" in addr:
            scope = owner if addr.startswith("fe80:") else 0
            lines.append(f"addr: link={owner} family=inet6 addr={addr} scope={scope} prefix={prefix}")
        else:
            lines.append(f"addr: link={owner} family=inet addr={addr} scope={owner} prefix={prefix}")
    return "\n".join(lines) + "\n"


def build_plans(tmp_path, *facts, diskless=False):
    paths = []
    for i, text in enumerate(facts):
        path = tmp_path / f"facts{i}.txt"
        path.write_text(text)
        paths.append(path)
    args = (["--diskless"] if diskless else []) + paths
    out = run_case("plan_build", *args)
    steps = []
    for chunk in out.split("== step ")[1:]:
        _, _, body = chunk.partition("\n")
        steps.append(parse_kv_lines(body))
    return steps


def roles(plan):
    return {f["name"]: (f["role"], f["mask"]) for kind, f in plan if kind == "link"}


def status(plan):
    return next(f for kind, f in plan if kind == "plan")


MODE = {"bridge": {"raNA": "0", "raDS": "0"}, "dhcp": {"raNA": "0", "raDS": "1"}, "nat": {"raNA": "1", "raDS": "1"},
        "unsupported": {"raNA": "1", "raDS": "0"}, "unknown": {}}
BRIDGE_LINKS = [("bridge0", 9), ("bridge1", 10), ("mgi1", 2), ("lo0", 5)]
BRIDGE_ADDRS = [(9, "192.168.1.10", 24), (9, "169.254.147.85", 16), (9, "fe80::ff:fe00:1", 64), (9, "2001:db8::1", 64),
                (5, "127.0.0.1", 8), (5, "::1", 128), (5, "fe80::1", 64)]
NAT_LINKS = [("bridge0", 9), ("mgi1", 2), ("lo0", 5)]
NAT_ADDRS = [(9, "10.0.1.1", 24), (9, "fe80::ff:fe00:1", 64), (2, "192.168.1.10", 24), (2, "169.254.140.130", 16),
             (2, "fe80::ff:fe00:2", 64), (2, "2001:db8::2", 64), (5, "127.0.0.1", 8)]


# ---------------------------------------------------------------- topology ----

def test_topology_bridge_mode_one_bridge_owns_every_hint(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["bridge"], "laIP": "192.168.1.10", "waIP": "192.168.1.10", "waLL": "169.254.147.85", "usbF": "0x458"},
        links=BRIDGE_LINKS, addrs=BRIDGE_ADDRS))
    assert status(plan)["status"] == "validated" and status(plan)["mode"] == "bridge"
    assert roles(plan) == {"bridge0": ("lan", "smb,adisk"), "bridge1": ("isolated", "none"), "mgi1": ("isolated", "none"), "lo0": ("isolated", "none")}


@pytest.mark.parametrize("wan_iface", ["mgi1", "bcmeth1"])
def test_topology_nat_mode_wan_is_the_link_owning_waip_or_wall(tmp_path, wan_iface):
    links = [("bridge0", 9), (wan_iface, 2), ("lo0", 5)]
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "waLL": "169.254.140.130", "usbF": "0x450"},
        links=links, addrs=NAT_ADDRS))
    assert roles(plan)["bridge0"] == ("lan", "smb,adisk")
    assert roles(plan)[wan_iface] == ("wan", "none")
    assert roles(plan)["lo0"] == ("isolated", "none")


def test_topology_nat_wan_with_only_wall_is_still_wan(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["nat"], "laIP": "10.0.1.1", "waLL": "169.254.140.130", "usbF": "0x458"},
        links=NAT_LINKS, addrs=[(9, "10.0.1.1", 24), (2, "169.254.140.130", 16), (2, "fe80::ff:fe00:2", 64)]))
    assert roles(plan)["mgi1"] == ("wan", "smb,adisk")


def test_topology_guest_bridge_wins_over_lan_and_follows_wan_mask(tmp_path):
    for usbf, expected in (("0x458", "smb,adisk"), ("0x450", "none")):
        plan, = build_plans(tmp_path, facts_text(
            acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "gnRo": "172.16.42.1", "usbF": usbf},
            links=[*NAT_LINKS, ("bridge1", 10)], addrs=[*NAT_ADDRS, (10, "172.16.42.1", 24), (10, "fe80::ff:fe00:a", 64)]))
        assert roles(plan)["bridge1"] == ("guest", expected)
        assert roles(plan)["mgi1"] == ("wan", expected)
        assert roles(plan)["bridge0"] == ("lan", "smb,adisk")


def test_topology_dhcp_only_single_bridge_is_lan_and_wan_bit_ignored(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["dhcp"], "laIP": "192.168.1.10", "waIP": "192.168.1.10", "waLL": "169.254.147.85", "usbF": "0x458"},
        links=BRIDGE_LINKS, addrs=BRIDGE_ADDRS))
    assert status(plan)["mode"] == "dhcp"
    assert roles(plan)["bridge0"] == ("lan", "smb,adisk")
    assert all(role == "isolated" for name, (role, _) in roles(plan).items() if name != "bridge0")


def test_topology_wan_role_requires_nat_even_when_link_owns_wall(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["bridge"], "laIP": "192.168.1.10", "waLL": "169.254.140.130", "usbF": "0x458"},
        links=NAT_LINKS, addrs=[(9, "192.168.1.10", 24), (2, "169.254.140.130", 16)]))
    assert roles(plan)["mgi1"] == ("isolated", "none")


def test_topology_double_nat_private_waip_is_wan(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.0.5", "usbF": "0x450"},
        links=NAT_LINKS, addrs=[(9, "10.0.1.1", 24), (2, "192.168.0.5", 24)]))
    assert roles(plan)["mgi1"] == ("wan", "none") and roles(plan)["bridge0"] == ("lan", "smb,adisk")


def test_topology_unidentified_links_are_isolated(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "usbF": "0x458"},
        links=[*NAT_LINKS, ("gif0", 20), ("stf0", 21), ("ppp0", 22), ("bwl0", 3)],
        addrs=[*NAT_ADDRS, (20, "2001:db8:1::1", 64), (21, "2001:db8:2::1", 64), (22, "203.0.113.9", 32), (3, "fe80::ff:fe00:3", 64)]))
    for name in ("gif0", "stf0", "ppp0", "bwl0"):
        assert roles(plan)[name] == ("isolated", "none")


def test_topology_link_without_service_address_is_isolated_even_if_it_owns_a_hint(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["bridge"], "laIP": "127.0.0.1"}, links=[("lo0", 5)], addrs=[(5, "127.0.0.1", 8)]))
    assert roles(plan)["lo0"] == ("isolated", "none")


def test_topology_hint_owned_by_two_links_is_incoherent(tmp_path):
    plan, = build_plans(tmp_path, facts_text(
        acp={**MODE["bridge"], "laIP": "192.168.1.10", "usbF": "0x450"},
        links=[("bridge0", 9), ("bridge1", 10)], addrs=[(9, "192.168.1.10", 24), (10, "192.168.1.10", 24)]))
    assert status(plan)["status"] == "cold-start" and status(plan)["reason"] == "laIP"
    assert all(role == "isolated" for role, _ in roles(plan).values())


def test_topology_unsupported_mode_and_missing_mode_keys_are_unknown(tmp_path):
    for acp in (MODE["unsupported"], {"raNA": "0"}, {"raNA": "ABORT", "raDS": "ABORT"}):
        plan, = build_plans(tmp_path, facts_text(acp={**acp, "laIP": "192.168.1.10"}, links=BRIDGE_LINKS, addrs=BRIDGE_ADDRS))
        assert status(plan)["mode"] == "unknown" and status(plan)["reason"] == "mode"
        assert all(role == ("isolated", "none") for role in roles(plan).values())


@pytest.mark.parametrize("guest", ["172.16.42.1", "ABORT", None])
def test_unknown_mode_never_guesses_bridge_roles(tmp_path, guest):
    links = [("bridge0", 9), ("bridge1", 10), ("mgi1", 2)]
    addrs = [(9, "10.0.1.1", 24), (10, "172.16.42.1", 24), (2, "192.168.1.10", 24)]
    cold = facts_text(acp={"raNA": "ABORT", "raDS": "ABORT", "gnRo": guest, "usbF": "0x458"},
                      links=links, addrs=addrs)
    valid = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10",
                            "gnRo": "172.16.42.1", "usbF": "0x450"}, links=links, addrs=addrs)
    before, recovered = build_plans(tmp_path, cold, valid)
    assert status(before)["status"] == "cold-start" and status(before)["reason"] == "mode"
    assert all(role == ("isolated", "none") for role in roles(before).values())
    assert roles(recovered)["bridge0"] == ("lan", "smb,adisk")
    assert roles(recovered)["bridge1"] == ("guest", "none")


def test_topology_address_without_ifinfo_record_still_takes_its_role(tmp_path):
    """C.11 (decided with review finding 9): the kernel never reports an address
    for an interface it does not list, so a NEWADDR without IFINFO is a gap in
    our parsing. The address is still ACP's LAN address on that index: the
    unnamed link is LAN and flagged synthetic -- never silently isolated."""
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "laIP": "192.168.1.10", "usbF": "0x450"},
                                             links=[("lo0", 5)], addrs=[(9, "192.168.1.10", 24), (5, "127.0.0.1", 8)]))
    assert status(plan)["status"] == "validated"
    link = next(f for kind, f in plan if kind == "link" and f["index"] == "9")
    assert link == {"name": "", "index": "9", "role": "lan", "mask": "smb,adisk", "synthetic": "1"}
    assert any(kind == "addr" and fields["link"] == "9" and fields["addr"] == "192.168.1.10"
               for kind, fields in plan)
    # A named link never carries the flag.
    assert "synthetic" not in next(f for kind, f in plan if kind == "link" and f["index"] == "5")


def test_all_acp_reads_aborted_waits_then_recovers(tmp_path):
    aborted = {key: "ABORT" for key in ("raNA", "raDS", "waNM", "usbF", "laIP", "waIP", "waLL", "gnRo", "syNm", "waMA")}
    cold, recovered = build_plans(tmp_path,
        facts_text(acp=aborted, links=NAT_LINKS, addrs=NAT_ADDRS), NAT_OK)
    assert all(mask == "none" for _, mask in roles(cold).values())
    assert status(recovered)["status"] == "validated"


def test_topology_iflist_failure_is_incoherent(tmp_path):
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "laIP": "192.168.1.10"}, links=[], addrs=[], iflist_ok=0))
    assert status(plan)["reason"] == "iflist" and roles(plan) == {}


def test_topology_sixteen_links_fit_and_more_truncate(tmp_path):
    links = [(f"vlan{i}", 20 + i) for i in range(16)]
    addrs = [(20 + i, f"10.{i}.0.1", 24) for i in range(16)]
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "laIP": "10.3.0.1", "usbF": "0x450"}, links=links, addrs=addrs))
    assert len(roles(plan)) == 16 and roles(plan)["vlan3"] == ("lan", "smb,adisk")


def test_topology_one_link_may_own_sixty_three_addresses(tmp_path):
    """Review finding 10: a link plan holds as many addresses as the interface
    table (64), so discovery does not silently drop address ownership. 63
    addresses on bridge0 plus loopback, IPv4 and IPv6 interleaved."""
    addrs = [(5, "127.0.0.1", 8)]
    for i in range(63):
        if i % 3 == 2:
            addrs.append((9, f"2001:db8:{i:x}::1", 64))
        else:
            addrs.append((9, f"10.0.{i}.1", 24))
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "laIP": "10.0.0.1", "usbF": "0x450"},
                                             links=[("bridge0", 9), ("lo0", 5)], addrs=addrs))
    assert status(plan)["status"] == "validated"
    assert roles(plan)["bridge0"] == ("lan", "smb,adisk")
    assert len([f for kind, f in plan if kind == "addr" and f["link"] == "9"]) == 63


def test_identity_quotes_and_backslashes_round_trip_through_the_plan_line(tmp_path):
    """Review 2 R9: an accepted Apple name may contain `"` or `\\`; the plan
    line escapes them so the doctor's shlex-based parser reads them back."""
    import shlex
    name = 'Capsule "A" \\ B'
    plan, = build_plans(tmp_path, nat_guest_with(syNm=name, waMA="e8:8d:28:58:f1:5c"))
    raw = next(line for line in open(tmp_path / "facts0.txt").read().splitlines() if line.startswith("acp: key=syNm"))
    assert raw.endswith(name)
    identity_line = next(f for kind, f in plan if kind == "identity")
    assert identity_line["instance"] == name
    # The raw text is the documented grammar: shlex reads it as one token.
    out = run_case("plan_build", tmp_path / "facts0.txt")
    raw_identity = next(line for line in out.splitlines() if line.startswith("identity: "))
    tokens = shlex.split(raw_identity.partition(": ")[2])
    assert tokens[0] == f"instance={name}"


def test_topology_table_overflow_is_incomplete(tmp_path):
    """A 65th address exceeds the interface table: the collector keeps 64 and
    reports the snapshot truncated (a facts file cannot even carry a 65th
    row), and the plan is incomplete rather than validated with a subset."""
    addrs = [(9, f"10.{i // 250}.{i % 250}.1", 24) for i in range(64)]
    text = facts_text(acp={**MODE["bridge"], "laIP": "10.0.0.1", "usbF": "0x450"}, links=[("bridge0", 9)], addrs=addrs)
    text = text.replace("iflist: ok=1 truncated=0", "iflist: ok=1 truncated=1")
    plan, = build_plans(tmp_path, text)
    assert status(plan)["status"] == "cold-start" and status(plan)["reason"] == "iflist-truncated"


# ------------------------------------------------------------------ policy ----

@pytest.mark.parametrize("mode", ["bridge", "dhcp", "nat"])
@pytest.mark.parametrize("usbf", [None, "0x450", "0x458"])
@pytest.mark.parametrize("afp", [0, 1])
@pytest.mark.parametrize("diskless", [False, True])
def test_policy_mask_matrix(tmp_path, mode, usbf, afp, diskless):
    acp = {**MODE[mode], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "waLL": "169.254.140.130", "gnRo": "172.16.42.1", "waNM": "0"}
    if usbf is not None:
        acp["usbF"] = usbf
    if mode != "nat":
        acp["laIP"] = acp["waIP"] = "192.168.1.10"
    links = [("bridge0", 9), ("mgi1", 2), ("bridge1", 10)]
    addrs = [(9, acp["laIP"], 24), (2, "192.168.1.10", 24), (2, "169.254.140.130", 16), (10, "172.16.42.1", 24)]
    if mode != "nat":
        addrs = [(9, "192.168.1.10", 24), (9, "169.254.140.130", 16), (10, "172.16.42.1", 24), (2, "fe80::ff:fe00:2", 64)]
    plan, = build_plans(tmp_path, facts_text(acp=acp, links=links, addrs=addrs, config={"advertise_afp": afp}), diskless=diskless)
    lan_mask = "none" if diskless else ("smb,afp,adisk" if afp else "smb,adisk")
    got = roles(plan)
    if mode == "nat" and usbf is None:
        assert status(plan)["reason"] == "usbF"
        assert all(mask == "none" for _, mask in got.values())
        return
    assert got["bridge0"] == ("lan", lan_mask)
    assert got["bridge1"][0] == "guest"
    if mode == "nat":
        assert got["mgi1"][0] == "wan"
        if usbf is None:
            # (b) unvalidated at cold start: nothing granted, plan incomplete.
            assert got["mgi1"][1] == "none" and got["bridge1"][1] == "none"
            assert status(plan)["status"] != "validated" and status(plan)["reason"] == "usbF"
        else:
            expected = lan_mask if usbf == "0x458" else "none"
            assert got["mgi1"][1] == expected and got["bridge1"][1] == expected
            assert status(plan)["status"] == "validated"
    else:
        # The disks-over-WAN bit is stale outside NAT: guest gets nothing, mgi1 is isolated.
        assert got["bridge1"][1] == "none" and got["mgi1"][0] == "isolated"
        assert status(plan)["status"] == "validated"
    assert status(plan)["diskless"] == ("1" if diskless else "0")


# --------------------------------------------------------------- retention ----

NAT_OK = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "usbF": "0x458"}, links=NAT_LINKS, addrs=NAT_ADDRS)
NAT_DENIED = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "usbF": "0x450"}, links=NAT_LINKS, addrs=NAT_ADDRS)
NAT_ACP_DEAD = facts_text(acp={"raNA": "ABORT", "raDS": "ABORT", "usbF": "ABORT"}, links=NAT_LINKS, addrs=NAT_ADDRS)
NAT_USBF_MISSING = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10"}, links=NAT_LINKS, addrs=NAT_ADDRS)
NAT_RECREATED = facts_text(acp={"raNA": "ABORT", "raDS": "ABORT"}, links=[("bridge0", 11), ("mgi1", 2), ("lo0", 5)],
                           addrs=[(11, "10.0.1.1", 24), (11, "fe80::ff:fe00:b", 64), (2, "192.168.1.10", 24), (5, "127.0.0.1", 8)])
NAT_NEW_LINK = facts_text(acp={"raNA": "ABORT", "raDS": "ABORT"}, links=[*NAT_LINKS, ("bridge1", 10)],
                          addrs=[*NAT_ADDRS, (10, "172.16.42.1", 24)])


def test_retention_failed_reread_keeps_validated_grant_and_ages(tmp_path):
    first, second, third, fourth = build_plans(tmp_path, NAT_OK, NAT_ACP_DEAD, NAT_ACP_DEAD, NAT_ACP_DEAD)
    assert status(first)["status"] == "validated"
    for step, age in ((second, "10"), (third, "20"), (fourth, "30")):
        assert status(step)["status"] == "incomplete" and status(step)["reason"] == "mode"
        assert status(step)["stale_seconds"] == age
        assert roles(step)["mgi1"] == ("wan", "smb,adisk") and roles(step)["bridge0"] == ("lan", "smb,adisk")
        assert {f.get("retained") for kind, f in step if kind == "link" and f["name"] in ("mgi1", "bridge0")} == {"1"}
        # Raw availability is reported honestly even while policy is retained.
        assert next(f for kind, f in step if kind == "acp")["raNA"] == "unavailable"


def test_retention_failed_reread_keeps_validated_denial(tmp_path):
    first, second = build_plans(tmp_path, NAT_DENIED, NAT_ACP_DEAD)
    assert roles(first)["mgi1"] == ("wan", "none")
    assert roles(second)["mgi1"] == ("wan", "none") and status(second)["status"] == "incomplete"


def test_retention_revocation_is_never_undone_by_a_later_failure(tmp_path):
    _, revoked, failed = build_plans(tmp_path, NAT_OK, NAT_DENIED, NAT_ACP_DEAD)
    assert roles(revoked)["mgi1"] == ("wan", "none") and status(revoked)["status"] == "validated"
    assert roles(failed)["mgi1"] == ("wan", "none")


def test_retention_recreated_link_with_new_index_is_isolated(tmp_path):
    _, after = build_plans(tmp_path, NAT_OK, NAT_RECREATED)
    assert roles(after)["bridge0"] == ("isolated", "none")
    assert roles(after)["mgi1"] == ("wan", "smb,adisk")


def test_retention_new_link_during_failure_is_isolated(tmp_path):
    _, after = build_plans(tmp_path, NAT_OK, NAT_NEW_LINK)
    assert roles(after)["bridge1"] == ("isolated", "none") and roles(after)["mgi1"] == ("wan", "smb,adisk")


def test_retention_usbf_failure_after_grant_keeps_wan_permission(tmp_path):
    _, after = build_plans(tmp_path, NAT_OK, NAT_USBF_MISSING)
    assert status(after)["status"] == "incomplete" and status(after)["reason"] == "usbF"
    assert roles(after)["mgi1"] == ("wan", "smb,adisk")
    assert next(f for kind, f in after if kind == "link" and f["name"] == "mgi1")["retained"] == "1"


def test_retention_usbf_failure_after_denial_keeps_denial(tmp_path):
    _, after = build_plans(tmp_path, NAT_DENIED, NAT_USBF_MISSING)
    assert roles(after)["mgi1"] == ("wan", "none")


NAT_GUEST_OK = facts_text(acp={**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "gnRo": "172.16.42.1", "usbF": "0x450"},
                          links=[*NAT_LINKS, ("bridge1", 10)], addrs=[*NAT_ADDRS, (10, "172.16.42.1", 24)])


def nat_guest_with(**overrides):
    acp = {**MODE["nat"], "laIP": "10.0.1.1", "waIP": "192.168.1.10", "gnRo": "172.16.42.1", "usbF": "0x450", **overrides}
    acp = {k: v for k, v in acp.items() if v is not None}
    return facts_text(acp=acp, links=[*NAT_LINKS, ("bridge1", 10)], addrs=[*NAT_ADDRS, (10, "172.16.42.1", 24)])


@pytest.mark.parametrize("key", ["laIP", "waIP", "waLL", "gnRo"])
def test_retention_aborted_hint_read_keeps_validated_policy(tmp_path, key):
    """Review finding 1: a hint key whose read failed (status=abort) is not an
    observation that the key is unset. The plan is incomplete, names the key,
    keeps every validated role and grant on unchanged links, and ages."""
    first, second, third = build_plans(tmp_path, NAT_GUEST_OK, nat_guest_with(**{key: "ABORT"}), nat_guest_with(**{key: "ABORT"}))
    assert status(first)["status"] == "validated"
    assert roles(first)["bridge0"] == ("lan", "smb,adisk") and roles(first)["bridge1"] == ("guest", "none")
    for step, age in ((second, "10"), (third, "20")):
        assert status(step)["status"] == "incomplete" and status(step)["reason"] == key
        assert status(step)["stale_seconds"] == age
        assert roles(step)["bridge0"] == ("lan", "smb,adisk")
        assert roles(step)["bridge1"] == ("guest", "none")
        assert roles(step)["mgi1"] == ("wan", "none")
        assert next(f for kind, f in step if kind == "acp")[key] == "unavailable"


@pytest.mark.parametrize("key", ["laIP", "waIP", "waLL", "gnRo"])
def test_malformed_hint_value_is_a_failed_read_not_an_absence(tmp_path, key):
    """Review 2 R5: acp exited 0 but printed something that is not an address.
    That is not "the key is unset": the previous validated policy stays."""
    first, second = build_plans(tmp_path, NAT_GUEST_OK, nat_guest_with(**{key: "not-an-IP"}))
    assert status(first)["status"] == "validated"
    assert status(second)["status"] == "incomplete" and status(second)["reason"] == key
    assert roles(second)["bridge0"] == ("lan", "smb,adisk") and roles(second)["bridge1"] == ("guest", "none")
    assert next(f for kind, f in second if kind == "acp")[key] == "unavailable"
    cold, = build_plans(tmp_path, nat_guest_with(**{key: "not-an-IP"}))
    assert status(cold)["status"] == "cold-start" and status(cold)["reason"] == key
    assert {mask for _, mask in roles(cold).values()} == {"none"}


@pytest.mark.parametrize("key", ["gnRo", "waLL"])
def test_unavailable_optional_hint_is_an_observation_not_a_failure(tmp_path, key):
    """acp answering "not set" for an optional key is a real re-read: the guest
    network was switched off, so bridge1 loses its guest role and the plan is
    validated again with no stale age."""
    first, second = build_plans(tmp_path, NAT_GUEST_OK, nat_guest_with(**{key: None}))
    assert status(first)["status"] == "validated"
    assert status(second)["status"] == "validated" and status(second)["stale_seconds"] == "0"
    assert roles(second)["bridge0"] == ("lan", "smb,adisk")
    assert roles(second)["bridge1"] == (("isolated", "none") if key == "gnRo" else ("guest", "none"))


@pytest.mark.parametrize("key", ["laIP", "waIP", "waLL", "gnRo"])
def test_cold_start_with_aborted_hint_grants_nothing(tmp_path, key):
    (plan,) = build_plans(tmp_path, nat_guest_with(**{key: "ABORT"}))
    assert status(plan)["status"] == "cold-start" and status(plan)["reason"] == key
    # No validated history means no role is trusted and nobody gets a grant.
    assert {mask for _, mask in roles(plan).values()} == {"none"}
    assert roles(plan)["bridge1"] == ("isolated", "none")
    assert roles(plan)["bridge0"] == ("isolated", "none")


def test_acp_budget_exhaustion_marks_unread_keys_aborted():
    assert run_case("acp_budget_exhaustion").strip() == "budget exhaustion marks 3 keys aborted"


def test_retention_successful_reread_replaces_retained_state(tmp_path):
    _, _, recovered = build_plans(tmp_path, NAT_OK, NAT_ACP_DEAD, NAT_DENIED)
    assert status(recovered)["status"] == "validated" and status(recovered)["stale_seconds"] == "0"
    assert roles(recovered)["mgi1"] == ("wan", "none")


def test_retention_restart_without_history_is_cold_start(tmp_path):
    only, = build_plans(tmp_path, NAT_ACP_DEAD)
    assert status(only)["status"] == "cold-start" and roles(only)["mgi1"] == ("isolated", "none")


def test_kernel_failure_retains_native_addresses_until_complete_observation(tmp_path):
    unavailable = facts_text(acp={}, iflist_ok=0)
    first, failed, recovered = build_plans(tmp_path, NAT_OK, unavailable, NAT_DENIED)
    assert [(kind, fields) for kind, fields in first if kind == "addr"] == [
        (kind, fields) for kind, fields in failed if kind == "addr"
    ]
    assert status(failed)["reason"] == "iflist"
    assert roles(recovered)["mgi1"] == ("wan", "none")


def test_native_loop_history_does_not_resurrect_a_disappeared_link(tmp_path):
    absent = facts_text(acp={}, links=[("mgi1", 2)], addrs=[(2, "192.168.1.10", 24)])
    returned = facts_text(acp={}, links=NAT_LINKS, addrs=NAT_ADDRS)
    _, _, last = build_plans(tmp_path, NAT_OK, absent, returned)
    assert roles(last)["bridge0"] == ("isolated", "none")
    assert roles(last)["mgi1"] == ("wan", "smb,adisk")


def test_kernel_failure_keeps_latest_observed_address_without_refreshing_policy_age(tmp_path):
    changed_address = NAT_ACP_DEAD.replace("addr=10.0.1.1 ", "addr=10.0.1.2 ")
    first, changed, failed = build_plans(tmp_path, NAT_OK, changed_address, facts_text(acp={}, iflist_ok=0))
    first_addrs = [fields["addr"] for kind, fields in first if kind == "addr"]
    changed_addrs = [fields["addr"] for kind, fields in changed if kind == "addr"]
    failed_addrs = [fields["addr"] for kind, fields in failed if kind == "addr"]
    assert "10.0.1.1" in first_addrs
    assert "10.0.1.2" in changed_addrs and "10.0.1.1" not in changed_addrs
    assert failed_addrs == changed_addrs
    assert status(changed)["stale_seconds"] == "10"
    assert status(failed)["stale_seconds"] == "20"


def test_retention_diskless_clears_retained_masks_but_keeps_roles(tmp_path):
    _, failed = build_plans(tmp_path, NAT_OK, NAT_ACP_DEAD, diskless=True)
    assert roles(failed)["mgi1"] == ("wan", "none") and roles(failed)["bridge0"] == ("lan", "none")


# ---------------------------------------------------------- config reader ----

@pytest.mark.parametrize("value", ["", "plain", "with space", "it's here", "'leading", "trailing'", "d'oub''le", "ünïcödé",
                                   "$HOME `x` \\n", "#not comment", "a=b=c", "tab\tinside", "quote\"double"])
def test_config_reader_round_trips_shlex_quote(tmp_path, value):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text(f"TC_CONFIG_VERSION=3\nTC_MDNS_INSTANCE_NAME={shlex.quote(value)}\nNBNS_ENABLED=1\n")
    out = run_case("config_reader_decodes_shlex_quoting", path, "TC_MDNS_INSTANCE_NAME")
    assert out == f"ok:{value}\n"
    assert run_case("config_reader_decodes_shlex_quoting", path, "NBNS_ENABLED") == "ok:1\n"


@pytest.mark.parametrize("line", ["TC_MDNS_INSTANCE_NAME='unterminated", "TC_MDNS_INSTANCE_NAME=$(reboot)", "TC_MDNS_INSTANCE_NAME=a b",
                                  "TC_MDNS_INSTANCE_NAME=`x`", "TC_MDNS_INSTANCE_NAME=\"$x\"", "TC_MDNS_INSTANCE_NAME=a;b"])
def test_config_reader_rejects_shell_syntax_it_cannot_evaluate(tmp_path, line):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text(line + "\n")
    assert run_case("config_reader_decodes_shlex_quoting", path, "TC_MDNS_INSTANCE_NAME") == "unavailable\n"


def test_config_reader_missing_key_prefix_match_and_comments(tmp_path):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text("# comment\nTC_NETBIOS_NAME_OLD='x'\n  TC_NETBIOS_NAME = 'Cap sule' # trailing\nNBNS_ENABLED=0\n")
    assert run_case("config_reader_decodes_shlex_quoting", path, "TC_NETBIOS_NAME") == "ok:Cap sule\n"
    assert run_case("config_reader_decodes_shlex_quoting", path, "MDNS_ADVERTISE_AFP") == "unavailable\n"
    assert run_case("config_reader_decodes_shlex_quoting", tmp_path / "missing", "NBNS_ENABLED") == "unavailable\n"


def test_config_reader_last_assignment_wins(tmp_path):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text("NBNS_ENABLED=0\nNBNS_ENABLED=1\n")
    assert run_case("config_reader_decodes_shlex_quoting", path, "NBNS_ENABLED") == "ok:1\n"


def test_device_config_reads_one_coherent_snapshot(tmp_path):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text("MDNS_ADVERTISE_AFP=1\nNBNS_ENABLED=0\nSMBD_DEBUG_LOGGING=0\nMDNS_DEBUG_LOGGING=1\n")
    assert run_case("config_facts_snapshot", path) == "rc=0 afp=1 debug=1\n"


def test_device_config_preserves_missing_false_and_invalid_states(tmp_path):
    path = tmp_path / "tcapsulesmb.conf"
    path.write_text("NBNS_ENABLED=invalid\nSMBD_DEBUG_LOGGING=1\n")
    assert run_case("config_facts_snapshot", path) == "rc=0 afp=0 debug=1\n"
    path.write_text("NBNS_ENABLED=0\n")
    assert run_case("config_facts_snapshot", path) == "rc=0 afp=0 debug=0\n"
    path.write_text("MDNS_ADVERTISE_AFP=invalid\n")
    assert run_case("config_facts_snapshot", path) == "rc=0 afp=-1 debug=0\n"
    assert run_case("config_facts_snapshot", tmp_path / "missing") == "rc=-1 afp=-1 debug=-1\n"


def test_device_config_rejects_overlong_physical_line_continuations(tmp_path):
    path = tmp_path / "tcapsulesmb.conf"
    prefix = "IGNORED="
    path.write_text(prefix + "x" * (1023 - len(prefix)) + "NBNS_ENABLED=1\n")
    assert run_case("config_facts_snapshot", path) == "rc=-1 afp=-1 debug=-1\n"
    assert run_case("config_reader_decodes_shlex_quoting", path, "NBNS_ENABLED") == "unavailable\n"


# ---------------------------------------------------------------- identity ----

@pytest.mark.parametrize("value", ["AirPort Time Capsule", "  James's  Time.Capsule  ", "Über Kapsel ünïcödé " + "x" * 80,
                                   "ctrl\x01char\x7f", "é" * 40, "x" * 63 + "yz", "\t\t"])
def test_identity_instance_name_matches_probe_py(value):
    from timecapsulesmb.device.probe import normalize_runtime_mdns_instance_name
    expected = normalize_runtime_mdns_instance_name(value)
    out = run_case("identity_normalization", "instance", value)
    rc, _, got = out.rstrip("\n").partition(":")
    assert (rc, got) == (("0", expected) if expected else ("-1", ""))


@pytest.mark.parametrize("value", ["airport-time-capsule", "Jamess-AirPort-Time-Capsule.local", "under_score.x", "---", "", "ünï.c",
                                   "CAPS and spaces", "0123456789abcdefgh"])
def test_identity_netbios_name_matches_probe_py(value):
    from timecapsulesmb.device.probe import normalize_runtime_netbios_name
    expected = normalize_runtime_netbios_name(value)
    out = run_case("identity_normalization", "netbios", value)
    rc, _, got = out.rstrip("\n").partition(":")
    assert (rc, got) == (("0", expected) if expected else ("-1", ""))


@pytest.mark.parametrize("value", ["AirPort-Time-Capsule.local", "James's Capsule", "  x..y", "---", "Über"])
def test_identity_host_label_matches_probe_py(value):
    from timecapsulesmb.device.probe import normalize_runtime_mdns_host_label
    expected = normalize_runtime_mdns_host_label(value)
    out = run_case("identity_normalization", "host", value)
    rc, _, got = out.rstrip("\n").partition(":")
    assert (rc, got) == (("0", expected) if expected else ("-1", ""))


@pytest.mark.parametrize("value,expected", [("e8:8d:28:58:f1:5c", "E8:8D:28:58:F1:5C"), ("E8-8D-28-58-F1-5C", "E8:8D:28:58:F1:5C"),
                                            ("e88d2858f15c", "E8:8D:28:58:F1:5C"), ("e8:8d:28:58:f1", ""), ("zz:8d:28:58:f1:5c", "")])
def test_identity_wama_normalization(value, expected):
    out = run_case("identity_normalization", "mac", value)
    assert out == (f"0:{expected}\n" if expected else "-1:\n")


@pytest.mark.parametrize("key", ["syNm", "waMA"])
def test_identity_aborted_read_keeps_previous_name_and_wama(tmp_path, key):
    """Review: a transient identity read failure must not rename the service
    (hostname fallback) or drop waMA; the previous values are carried."""
    good = nat_guest_with(syNm="My Capsule", waMA="e8:8d:28:58:f1:5c")
    aborted = nat_guest_with(**{"syNm": "My Capsule", "waMA": "e8:8d:28:58:f1:5c", key: "ABORT"})
    first, second, third = build_plans(tmp_path, good, aborted, good)
    ident = lambda plan: next(f for kind, f in plan if kind == "identity")
    assert ident(first) == {"instance": "My Capsule", "netbios": "airport-time-ca", "wama": "E8:8D:28:58:F1:5C"}
    assert status(second)["status"] == "validated"
    assert ident(second) == {"instance": "My Capsule", "netbios": "airport-time-ca", "wama": "E8:8D:28:58:F1:5C", "retained": "1"}
    assert ident(third) == ident(first)
    # No previous plan: the documented fallback chain still applies.
    cold, = build_plans(tmp_path, aborted)
    expected = {"instance": "airport-time-capsule", "wama": "E8:8D:28:58:F1:5C"} if key == "syNm" else {"instance": "My Capsule", "wama": "unavailable"}
    assert {k: ident(cold)[k] for k in ("instance", "wama")} == expected


def test_identity_ignores_deprecated_overrides_and_uses_synm_then_hostname(tmp_path):
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "syNm": "AirPort Time Capsule", "waMA": "e8-8d-28-58-f1-5c"},
                                             hostname="airport-time-capsule.local"))
    identity = next(f for kind, f in plan if kind == "identity")
    assert identity == {"instance": "AirPort Time Capsule", "netbios": "airport-time-ca", "wama": "E8:8D:28:58:F1:5C"}
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"], "syNm": "AirPort Time Capsule"},
                                             config={"instance": "My Capsule", "netbios": "MYCAP"}))
    identity = next(f for kind, f in plan if kind == "identity")
    assert identity == {"instance": "AirPort Time Capsule", "netbios": "airport-time-ca", "wama": "unavailable"}
    plan, = build_plans(tmp_path, facts_text(acp={**MODE["bridge"]}, hostname=""))
    identity = next(f for kind, f in plan if kind == "identity")
    assert identity == {"instance": "timecapsule", "netbios": "TimeCapsule", "wama": "unavailable"}


def test_service_link_plan_reports_closed_output(tmp_path):
    binary = compile_service(tmp_path / "service")
    facts = tmp_path / "facts.txt"
    facts.write_text(NAT_OK)
    process = subprocess.Popen([str(binary), "--print-link-plan", "--facts-file", str(facts)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    process.stdout.close()
    assert process.stderr is not None
    stderr = process.stderr.read()
    process.wait(timeout=10)
    assert process.returncode == 13, stderr.decode()



# Native NBNS eligibility/lifecycle is covered by test_wcifsnd.py. Apple now
# owns answer selection; the removed responder's subnet policy is not emulated.
