from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.device.probe import preferred_interface_name, probe_remote_interface_candidates_conn
from timecapsulesmb.transport.ssh import SshConnection


class ProbeTests(unittest.TestCase):
    def test_probe_remote_interface_candidates_prefers_bridge0_with_private_ipv4(self) -> None:
        ifconfig_output = """
gec0: flags=eb43<UP,BROADCAST,RUNNING,PROMISC,ALLMULTI,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
bridge0: flags=e043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.72 netmask 0xffffff00 broadcast 192.168.1.255
\tinet 169.254.117.175 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")
        self.assertEqual([candidate.name for candidate in result.candidates], ["gec0", "bridge0", "lo0"])

    def test_probe_remote_interface_candidates_prefers_bcmeth1_when_bridge0_has_no_ipv4(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bcmeth0: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bcmeth1")

    def test_probe_remote_interface_candidates_ignores_loopback_only_output(self) -> None:
        ifconfig_output = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
\tinet 127.0.0.1 netmask 0xff000000
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertIsNone(result.preferred_iface)
        self.assertIn("no non-loopback IPv4 interface candidates found", result.detail)

    def test_preferred_interface_name_uses_target_ip_before_generic_ranking(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("10.0.1.1",)), "bcmeth1")

    def test_preferred_interface_name_private_ipv4_beats_link_local_without_target_ip(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(result.preferred_iface, "bridge0")

    def test_preferred_interface_name_exact_link_local_target_can_win(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 169.254.44.9 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("169.254.44.9",)), "bcmeth1")

    def test_probe_remote_interface_candidates_preserves_multiple_private_candidates(self) -> None:
        ifconfig_output = """
bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255
\tstatus: active
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
\tinet 192.168.1.217 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=ifconfig_output)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
            result = probe_remote_interface_candidates_conn(connection)
        self.assertEqual(
            [(candidate.name, candidate.ipv4_addrs) for candidate in result.candidates],
            [("bcmeth1", ("10.0.1.1",)), ("bridge0", ("192.168.1.217",))],
        )
        self.assertEqual(preferred_interface_name(result.candidates), "bridge0")
        self.assertEqual(preferred_interface_name(result.candidates, target_ips=("10.0.1.1",)), "bcmeth1")
