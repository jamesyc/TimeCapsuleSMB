"""Which of a device's networks this computer is on, and what it is told when
it is on none where the device shares its disks.

Doctor reads the device's link plan (`service --print-link-plan`) to know where
the device shares disks; configure and Enable SSH only know the address they
could not reach.
"""
from __future__ import annotations

import ipaddress
import unittest
from types import SimpleNamespace
from unittest import mock

from timecapsulesmb.checks.doctor_state import DoctorSink, RemoteAccess
from timecapsulesmb.checks.doctor_steps import (
    DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK,
    DOCTOR_CODE_DEVICE_STARTING_UP,
    STARTUP_GRACE_DETAIL_KEY,
    STARTUP_GRACE_MASK,
    _apply_startup_grace,
    _doctor_check_unshared_network,
    client_on_unshared_network_message,
)
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import LocalInterfaceNetwork, host_networks, interface_kind, local_lan_networks
from timecapsulesmb.device.probe import ReadinessProbeResult, _parse_link_plan, link_plan_networks
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services.acp_ssh import (
    ACPDeviceOffNetworkError,
    ACP_PORT,
    enable_ssh_with_port_preflight,
)


# `service --print-link-plan` on the NetBSD 6 and NetBSD 4 LAN devices (both in
# bridge mode), captured 2026-10-07.
NETBSD6_BRIDGE_PLAN = """\
plan: status=validated mode=bridge stale_seconds=0 diskless=0
config: advertise_afp=0
acp: raNA=0 raDS=0 waNM=1 usbF=0x450 laIP=192.168.1.218 waIP=192.168.1.218 waLL=169.254.155.207 gnRo=172.16.42.1
identity: instance="James's AirPort Time Capsule" netbios=jamess-airport- wama=80:EA:96:E6:58:68
link: name=bcmeth1 index=1 role=isolated mask=none
link: name=bcmeth0 index=2 role=isolated mask=none
link: name=lo0 index=3 role=isolated mask=none
addr: link=3 family=inet addr=127.0.0.1 prefix=8
addr: link=3 family=inet6 addr=::1 scope=3 prefix=128
addr: link=3 family=inet6 addr=fe80::1 scope=3 prefix=64
link: name=bwl0 index=4 role=isolated mask=none
link: name=bwl1 index=5 role=isolated mask=none
link: name=wlan0 index=6 role=isolated mask=none
link: name=wlan1 index=7 role=isolated mask=none
link: name=vlan0 index=8 role=isolated mask=none
link: name=bridge0 index=9 role=lan mask=smb,adisk
addr: link=9 family=inet6 addr=fe80::82ea:96ff:fee6:5868 scope=9 prefix=64
addr: link=9 family=inet addr=192.168.1.218 prefix=24
addr: link=9 family=inet6 addr=2600:1700:83b7:20f:82ea:96ff:fee6:5868 scope=9 prefix=64
addr: link=9 family=inet addr=169.254.155.207 prefix=16
link: name=bridge1 index=10 role=isolated mask=none
"""

NETBSD4_BRIDGE_PLAN = """\
plan: status=validated mode=bridge stale_seconds=0 diskless=0
config: advertise_afp=0
acp: raNA=0 raDS=0 waNM=1 usbF=0x450 laIP=192.168.1.10 waIP=192.168.1.10 waLL=169.254.147.85 gnRo=172.16.42.1
identity: instance="AirPort Time Capsule" netbios=airport-time-ca wama=E8:8D:28:58:F1:5C
link: name=mgi0 index=1 role=isolated mask=none
link: name=mgi1 index=2 role=isolated mask=none
link: name=bwl0 index=3 role=isolated mask=none
link: name=bwl1 index=4 role=isolated mask=none
link: name=lo0 index=5 role=isolated mask=none
addr: link=5 family=inet addr=127.0.0.1 prefix=8
addr: link=5 family=inet6 addr=::1 scope=5 prefix=128
addr: link=5 family=inet6 addr=fe80::1 scope=5 prefix=64
link: name=wlan0 index=6 role=isolated mask=none
link: name=wlan1 index=7 role=isolated mask=none
link: name=vlan0 index=8 role=isolated mask=none
link: name=bridge0 index=9 role=lan mask=smb,adisk
addr: link=9 family=inet6 addr=fe80::ea8d:28ff:fe58:f15c scope=9 prefix=64
addr: link=9 family=inet6 addr=2600:1700:83b7:20f:ea8d:28ff:fe58:f15c scope=9 prefix=64
addr: link=9 family=inet addr=192.168.1.10 prefix=24
addr: link=9 family=inet addr=169.254.147.85 prefix=16
link: name=bridge1 index=10 role=isolated mask=none
"""


def router_plan(
    *,
    wan_mask: str = "none",
    guest_mask: str = "none",
    wan_ipv4: str = "192.168.1.57",
    status: str = "validated",
    lan_mask: str = "smb,adisk",
) -> str:
    """The NetBSD 4 plan above with the device in router mode, as two v3.3.0
    field devices reported: WAN on mgi1 (upstream 192.168.1.0/24 and its ULA
    prefix), the LAN bridge on Apple's 10.0.1.1, the guest network on bridge1."""
    return f"""\
plan: status={status} mode=nat stale_seconds=0 diskless=0
config: advertise_afp=0
acp: raNA=1 raDS=1 waNM=1 usbF=0x450 laIP=10.0.1.1 waIP={wan_ipv4} waLL=unavailable gnRo=172.16.42.1
identity: instance="Time Capsule bcca1b" netbios=time-capsule-bc wama=E8:8D:28:58:F1:5C
link: name=mgi0 index=1 role=isolated mask=none
link: name=mgi1 index=2 role=wan mask={wan_mask}
addr: link=2 family=inet addr={wan_ipv4} prefix=24
addr: link=2 family=inet6 addr=fe80::ea8d:28ff:fe58:f15d scope=2 prefix=64
addr: link=2 family=inet6 addr=fdca:7da7:ed32:428b:ea8d:28ff:fe58:f15d scope=2 prefix=64
link: name=lo0 index=5 role=isolated mask=none
addr: link=5 family=inet addr=127.0.0.1 prefix=8
addr: link=5 family=inet6 addr=::1 scope=5 prefix=128
link: name=bridge0 index=9 role=lan mask={lan_mask}
addr: link=9 family=inet6 addr=fe80::ea8d:28ff:fe58:f15c scope=9 prefix=64
addr: link=9 family=inet addr=10.0.1.1 prefix=24
addr: link=9 family=inet addr=169.254.147.85 prefix=16
link: name=bridge1 index=10 role=guest mask={guest_mask}
addr: link=10 family=inet addr=172.16.42.1 prefix=24
"""


def net(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    return ipaddress.ip_network(text)


def lan(interface: str, address: str, network: str) -> LocalInterfaceNetwork:
    return LocalInterfaceNetwork(interface, address, net(network))


class LinkPlanNetworksTests(unittest.TestCase):
    def test_bridge_mode_devices_share_disks_on_every_network_they_have(self) -> None:
        for name, text, address in (
            ("netbsd6", NETBSD6_BRIDGE_PLAN, "192.168.1.0/24"),
            ("netbsd4", NETBSD4_BRIDGE_PLAN, "192.168.1.0/24"),
        ):
            with self.subTest(device=name):
                shared, unshared = link_plan_networks(_parse_link_plan(text))
                # Loopback, 169.254/16 and fe80::/64 never place a client.
                self.assertCountEqual(shared, (net(address), net("2600:1700:83b7:20f::/64")))
                self.assertEqual(unshared, ())

    def test_router_mode_lists_the_wan_side_and_guest_network_as_unshared(self) -> None:
        shared, unshared = link_plan_networks(_parse_link_plan(router_plan()))
        self.assertEqual(shared, (net("10.0.1.0/24"),))
        self.assertEqual(unshared, (
            ("wan", net("192.168.1.0/24")),
            ("wan", net("fdca:7da7:ed32:428b::/64")),
            ("guest", net("172.16.42.0/24")),
        ))

    def test_share_disks_over_wan_shares_the_wan_side_and_guest_network(self) -> None:
        # The runtime grants both together (build/native/common/policy.c).
        shared, unshared = link_plan_networks(_parse_link_plan(router_plan(wan_mask="smb,adisk", guest_mask="smb,adisk")))
        self.assertCountEqual(shared, (net("10.0.1.0/24"), net("192.168.1.0/24"), net("fdca:7da7:ed32:428b::/64"), net("172.16.42.0/24")))
        self.assertEqual(unshared, ())

    def test_only_the_wan_side_and_guest_network_count_as_unshared(self) -> None:
        # Router-mode devices in telemetry also carry 6to4's stf0 as an
        # isolated link; a client is never told it is on that.
        text = router_plan() + (
            "link: name=stf0 index=11 role=isolated mask=none\n"
            "addr: link=11 family=inet6 addr=2002:5174:2c7b::1 prefix=16\n"
        )
        _shared, unshared = link_plan_networks(_parse_link_plan(text))
        self.assertEqual({role for role, _network in unshared}, {"wan", "guest"})
        self.assertNotIn(net("2002::/16"), [network for _role, network in unshared])

    def test_a_subnet_used_on_both_sides_counts_as_shared(self) -> None:
        # Two Apple routers in a row both default to 10.0.1.0/24.
        shared, unshared = link_plan_networks(_parse_link_plan(router_plan(wan_ipv4="10.0.1.57")))
        self.assertEqual(shared, (net("10.0.1.0/24"),))
        self.assertNotIn(net("10.0.1.0/24"), [network for _role, network in unshared])

    def test_plans_that_say_nothing_reliable_give_nothing(self) -> None:
        cases = {
            "incomplete": _parse_link_plan(router_plan(status="incomplete")),
            "cold-start": _parse_link_plan(router_plan(status="cold-start")),
            "diskless": _parse_link_plan(router_plan(lan_mask="none")),
            "no plan": None,
            "empty output": _parse_link_plan(""),
        }
        for name, plan in cases.items():
            with self.subTest(case=name):
                self.assertEqual(link_plan_networks(plan), ((), ()))

    def test_unparseable_addresses_are_skipped(self) -> None:
        text = router_plan() + "addr: link=10 family=inet addr=172.16.43.1\naddr: link=10 family=inet addr=bogus prefix=24\n"
        _shared, unshared = link_plan_networks(_parse_link_plan(text))
        self.assertEqual([network for role, network in unshared if role == "guest"], [net("172.16.42.0/24")])


class LocalNetworkTests(unittest.TestCase):
    @staticmethod
    def adapter(name: str, ip: str, prefix: int) -> SimpleNamespace:
        return SimpleNamespace(name=name, ips=[SimpleNamespace(ip=ip, network_prefix=prefix)])

    def test_interface_kinds(self) -> None:
        cases = {
            "en0": "lan",
            "eth0": "lan",
            "enp3s0": "lan",
            "wlan0": "lan",
            "utun4": "vpn",
            "ipsec0": "vpn",
            "ppp0": "vpn",
            "wg0": "vpn",
            "tailscale0": "vpn",
            "bridge100": "virtual",
            "vmnet8": "virtual",
            "docker0": "virtual",
            "awdl0": "other",
        }
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(interface_kind(name), kind)

    def test_vpn_and_virtual_interfaces_are_not_this_computers_networks(self) -> None:
        adapters = [
            self.adapter("en0", "192.168.1.170", 24),          # macOS Ethernet or Wi-Fi
            self.adapter("utun4", "100.99.99.10", 10),         # macOS VPN (Tailscale's range)
            self.adapter("bridge100", "192.168.64.1", 24),     # macOS virtual machine sharing
            self.adapter("enp3s0", "192.168.2.10", 24),        # Linux Ethernet
            self.adapter("wlp2s0", "192.168.3.10", 24),        # Linux Wi-Fi
            self.adapter("br0", "192.168.4.10", 24),           # Linux bridge holding the LAN address
            self.adapter("docker0", "172.17.0.1", 16),         # Linux containers
            self.adapter("virbr0", "192.168.122.1", 24),       # Linux virtual machines
            self.adapter("wg0", "10.8.0.2", 24),               # Linux WireGuard
            self.adapter("{6A1E2C4B-0000-0000-0000-000000000000}", "192.168.5.10", 24),  # Windows
        ]
        kept = [item.interface for item in local_lan_networks(adapters)]
        self.assertEqual(kept, ["en0", "enp3s0", "wlp2s0", "br0", "{6A1E2C4B-0000-0000-0000-000000000000}"])

    def test_host_networks(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.resolve_host_ips", return_value=("192.168.1.10", "fe80::1%en0")) as resolve:
            self.assertEqual(host_networks("Time-Capsule.local"), [net("192.168.1.10/32")])
            resolve.assert_called_once_with("Time-Capsule.local")
            resolve.reset_mock()
            self.assertEqual(host_networks("10.0.1.1"), [net("10.0.1.1/32")])
            self.assertEqual(host_networks("[2001:db8::1]"), [net("2001:db8::1/128")])
            # Link-local addresses say nothing about which network a host is on.
            self.assertEqual(host_networks("fe80::1%en0"), [])
            self.assertEqual(host_networks("169.254.1.1"), [])
            resolve.assert_not_called()


class UnsharedNetworkCheckTests(unittest.TestCase):
    REMOTE = RemoteAccess(ssh_checked=True, ssh_ok=True, remote_checks_enabled=True, active_smb_conf_reason="")

    def run_check(self, plan_text: str | None, local: tuple[LocalInterfaceNetwork, ...], *, remote: RemoteAccess | None = None):
        sink = DoctorSink(on_result=None, debug_fields=None)
        plan = _parse_link_plan(plan_text) if plan_text is not None else None
        with mock.patch("timecapsulesmb.checks.doctor_steps.local_lan_networks", return_value=local), \
                mock.patch("timecapsulesmb.checks.doctor_steps.sys.platform", "darwin"):
            failed = _doctor_check_unshared_network(remote or self.REMOTE, plan, sink)
        return failed, sink.results

    def test_a_mac_only_on_the_wan_side_fails_with_what_to_do(self) -> None:
        failed, results = self.run_check(router_plan(), (lan("en0", "192.168.1.170", "192.168.1.0/24"),))
        self.assertTrue(failed)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(
            results[0].message,
            "this Mac is on the device's internet (WAN) side (192.168.1.0/24), where the device does not share its "
            "disks; join the device's main network (10.0.1.0/24) by Wi-Fi or one of its LAN ports, then run doctor "
            "again. Bonjour and SMB were not checked from this Mac",
        )
        self.assertEqual(results[0].details["code"], DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK)
        self.assertEqual(results[0].details["unshared_networks"], [{"role": "wan", "network": "192.168.1.0/24"}])
        self.assertEqual(results[0].details["shared_networks"], ["10.0.1.0/24"])

    def test_a_mac_only_on_the_guest_network_fails_with_guest_wording(self) -> None:
        failed, results = self.run_check(router_plan(), (lan("en0", "172.16.42.20", "172.16.42.0/24"),))
        self.assertTrue(failed)
        self.assertIn("this Mac is on the device's guest network (172.16.42.0/24)", results[0].message)

    def test_an_ipv6_only_overlap_with_the_wan_side_fails(self) -> None:
        failed, results = self.run_check(router_plan(), (lan("en0", "fdca:7da7:ed32:428b::5", "fdca:7da7:ed32:428b::/64"),))
        self.assertTrue(failed)
        self.assertIn("internet (WAN) side (fdca:7da7:ed32:428b::/64)", results[0].message)

    def test_no_failure_when_this_computer_shares_a_disk_network(self) -> None:
        cases = {
            "on the LAN": (lan("en0", "10.0.1.20", "10.0.1.0/24"),),
            "on the WAN side and the LAN": (lan("en0", "192.168.1.170", "192.168.1.0/24"), lan("en1", "10.0.1.20", "10.0.1.0/24")),
            "on the guest network and the LAN": (lan("en0", "172.16.42.20", "172.16.42.0/24"), lan("en1", "10.0.1.20", "10.0.1.0/24")),
        }
        for name, local in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.run_check(router_plan(), local), (False, []))

    def test_public_networks_reach_the_message_only_by_family_and_length(self) -> None:
        # The FAIL message is copied into doctor's telemetry error.
        public_wan = router_plan(wan_ipv4="81.2.69.140")
        failed, results = self.run_check(public_wan, (lan("en0", "81.2.69.160", "81.2.69.128/25"),))
        self.assertTrue(failed)
        self.assertIn("internet (WAN) side (ipv4-public/25)", results[0].message)
        self.assertNotIn("81.2.69", results[0].message)
        self.assertEqual(results[0].details["client_networks"], ["ipv4-public/25"])
        # Private networks stay readable.
        _failed, results = self.run_check(router_plan(), (lan("en0", "192.168.1.170", "192.168.1.0/24"),))
        self.assertIn("(192.168.1.0/24)", results[0].message)
        self.assertIn("main network (10.0.1.0/24)", results[0].message)

    def test_no_failure_when_the_plan_or_the_networks_say_nothing(self) -> None:
        wan_side = (lan("en0", "192.168.1.170", "192.168.1.0/24"),)
        cases = {
            "share disks over WAN": (router_plan(wan_mask="smb,adisk", guest_mask="smb,adisk"), wan_side),
            "same subnet on both sides": (router_plan(wan_ipv4="10.0.1.57"), (lan("en0", "10.0.1.20", "10.0.1.0/24"),)),
            "bridge mode": (NETBSD4_BRIDGE_PLAN, wan_side),
            "plan not validated": (router_plan(status="incomplete"), wan_side),
            "no plan": (None, wan_side),
            # Reached through a router: the existing off-network checks apply.
            "on neither side": (router_plan(), (lan("en0", "192.168.50.10", "192.168.50.0/24"),)),
            # local_lan_networks leaves VPN tunnels out, so a VPN can never match.
            "no LAN networks": (router_plan(), ()),
        }
        for name, (plan, local) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.run_check(plan, local), (False, []))

    def test_no_failure_without_ssh_checks(self) -> None:
        remote = RemoteAccess(ssh_checked=False, ssh_ok=False, remote_checks_enabled=False, active_smb_conf_reason="")
        self.assertEqual(
            self.run_check(router_plan(), (lan("en0", "192.168.1.170", "192.168.1.0/24"),), remote=remote),
            (False, []),
        )

    def test_message_names_the_computer_by_platform(self) -> None:
        args = ([net("192.168.1.0/24")], ["wan"], [net("10.0.1.0/24")])
        mac = client_on_unshared_network_message(*args, platform="darwin")
        linux = client_on_unshared_network_message(*args, platform="linux")
        self.assertTrue(mac.startswith("this Mac is on the device's internet (WAN) side"))
        self.assertTrue(mac.endswith("not checked from this Mac"))
        self.assertTrue(linux.startswith("this computer is on the device's internet (WAN) side"))
        self.assertTrue(linux.endswith("not checked from this computer"))
        self.assertNotIn("Mac", linux)
        # Networks of more than one kind get the general wording.
        mixed = client_on_unshared_network_message([net("192.168.1.0/24")], ["wan", "guest"], [net("10.0.1.0/24")], platform="linux")
        self.assertTrue(mixed.startswith("this computer is on a device network (192.168.1.0/24)"))


class StartupGraceTests(unittest.TestCase):
    UNSHARED = CheckResult("FAIL", "this Mac is on the device's internet (WAN) side", {"code": DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK})
    STARTING = CheckResult("FAIL", "managed smbd is not running", {STARTUP_GRACE_DETAIL_KEY: STARTUP_GRACE_MASK})

    def test_waiting_is_never_suggested_for_being_on_the_wrong_network(self) -> None:
        results = [CheckResult("PASS", "SSH works"), self.UNSHARED]
        transformed, synthesized = _apply_startup_grace(results, 41.0)
        self.assertEqual(transformed, results)
        self.assertEqual(synthesized, ())

    def test_startup_failures_still_collapse_and_the_network_failure_stays(self) -> None:
        transformed, synthesized = _apply_startup_grace([self.STARTING, self.UNSHARED], 41.0)
        self.assertEqual([result.status for result in transformed], ["INFO", "FAIL", "FAIL"])
        self.assertIs(transformed[1], self.UNSHARED)
        self.assertEqual(synthesized[0].details["code"], DOCTOR_CODE_DEVICE_STARTING_UP)
        self.assertEqual(synthesized[0].details["masked_failures"], ["managed smbd is not running"])


class ReadinessProbeResultTests(unittest.TestCase):
    def test_link_plan_is_not_part_of_equality_or_hashing(self) -> None:
        left = ReadinessProbeResult(True, "ok", link_plan={"status": "validated"})
        right = ReadinessProbeResult(True, "ok", link_plan=None)
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))


class AcpPortPreflightTests(unittest.TestCase):
    """configure and Enable SSH both go through enable_ssh_with_port_preflight."""

    def preflight(self, host: str, local: tuple[LocalInterfaceNetwork, ...], *, error: str | None = "timed out", platform: str = "darwin"):
        with mock.patch("timecapsulesmb.services.acp_ssh.local_lan_networks", return_value=local), \
                mock.patch("timecapsulesmb.services.acp_ssh.sys.platform", platform), \
                mock.patch("timecapsulesmb.services.acp_ssh._record_port_probe_context"), \
                mock.patch("timecapsulesmb.services.acp_ssh._run_enable_ssh") as enable:
            enable_ssh_with_port_preflight(
                host, "pw", tcp_connect_error_func=lambda _host, _port: error, sleep_func=lambda _seconds: None,
            )
        return enable

    def failure(self, host: str, local: tuple[LocalInterfaceNetwork, ...], **kwargs) -> ACPConnectionError:
        with self.assertRaises(ACPConnectionError) as raised:
            self.preflight(host, local, **kwargs)
        return raised.exception

    def test_an_address_off_this_macs_network_says_so(self) -> None:
        # A saved address after the Mac moved networks, or a router-mode device seen from its WAN side.
        error = self.failure("10.0.1.1", (lan("en0", "192.168.1.170", "192.168.1.0/24"),))
        self.assertIsInstance(error, ACPDeviceOffNetworkError)
        self.assertEqual(
            str(error),
            f"Could not connect to ACP on 10.0.1.1:{ACP_PORT}. 10.0.1.1 is not on this Mac's network (192.168.1.0/24). "
            "Check the address, or connect this Mac to the device's network by Wi-Fi or one of its LAN ports, then try again.",
        )

    def test_this_macs_public_network_reaches_the_message_only_by_family_and_length(self) -> None:
        # The message becomes the operation's telemetry error.
        error = self.failure("10.0.1.1", (lan("en0", "81.2.69.160", "81.2.69.128/25"),))
        self.assertIn("10.0.1.1 is not on this Mac's network (ipv4-public/25)", str(error))
        self.assertNotIn("81.2.69", str(error))

    def test_linux_and_other_platforms_say_this_computer(self) -> None:
        error = self.failure("10.0.1.1", (lan("enp3s0", "192.168.1.170", "192.168.1.0/24"),), platform="linux")
        self.assertIn("10.0.1.1 is not on this computer's network (192.168.1.0/24)", str(error))
        self.assertIn("connect this computer to the device's network", str(error))
        self.assertNotIn("Mac", str(error))

    def test_a_hostname_resolving_off_this_macs_network_says_so(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.resolve_host_ips", return_value=("10.0.1.1",)):
            error = self.failure("Time-Capsule.local", (lan("en0", "192.168.1.170", "192.168.1.0/24"),))
        self.assertIsInstance(error, ACPDeviceOffNetworkError)
        self.assertIn("Time-Capsule.local is not on this Mac's network", str(error))

    def test_other_failures_keep_the_general_advice(self) -> None:
        cases = {
            "address on this Mac's network": ("192.168.1.218", (lan("en0", "192.168.1.170", "192.168.1.0/24"),)),
            "only a VPN to compare with": ("10.0.1.1", ()),
            "link-local address": ("fe80::1%en0", (lan("en0", "192.168.1.170", "192.168.1.0/24"),)),
        }
        for name, (host, local) in cases.items():
            with self.subTest(case=name):
                error = self.failure(host, local)
                self.assertNotIsInstance(error, ACPDeviceOffNetworkError)
                self.assertTrue(str(error).endswith("Check the device IP address or hostname."), str(error))

    def test_a_failing_network_check_keeps_the_general_advice(self) -> None:
        with mock.patch("timecapsulesmb.services.acp_ssh.host_networks", side_effect=OSError("boom")):
            error = self.failure("10.0.1.1", (lan("en0", "192.168.1.170", "192.168.1.0/24"),))
        self.assertNotIsInstance(error, ACPDeviceOffNetworkError)

    def test_an_answering_address_off_this_macs_network_is_still_used(self) -> None:
        # A device on another subnet reached through a router.
        enable = self.preflight("10.0.1.1", (lan("en0", "192.168.1.170", "192.168.1.0/24"),), error=None)
        enable.assert_called_once()


if __name__ == "__main__":
    unittest.main()
