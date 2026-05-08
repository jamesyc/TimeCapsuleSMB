from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.configure_defaults import (  # noqa: E402
    ConfigureValueChoice,
    derived_name_defaults,
    derived_prompt_defaults,
    interface_candidate_for_ip,
    interface_target_ips,
    ipv4_literal,
    saved_syap_value_for_candidates,
    saved_value_choice,
)
from timecapsulesmb.device.probe import (  # noqa: E402
    RemoteInterfaceCandidate,
    RemoteInterfaceCandidatesProbeResult,
)
from timecapsulesmb.discovery.bonjour import BonjourResolvedService  # noqa: E402


class ConfigureDefaultsTests(unittest.TestCase):
    def test_ipv4_literal_accepts_zero_padded_ipv4_and_rejects_ipv6_or_bad_octet(self) -> None:
        self.assertEqual(ipv4_literal("010.000.001.007"), "10.0.1.7")
        self.assertIsNone(ipv4_literal("fe80::1"))
        self.assertIsNone(ipv4_literal("10.0.1.999"))
        self.assertIsNone(ipv4_literal("capsule.local"))

    def test_interface_target_ips_deduplicates_host_and_discovered_ips(self) -> None:
        record = BonjourResolvedService("Capsule", "capsule.local", ipv4=["10.0.1.7", "192.168.1.72"])

        self.assertEqual(
            interface_target_ips({"TC_HOST": "root@010.000.001.007"}, record),
            ("10.0.1.7", "192.168.1.72"),
        )

    def test_interface_candidate_prefers_non_link_local_exact_match(self) -> None:
        probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate("lo0", ("10.0.1.7",), up=True, active=True, loopback=True),
                RemoteInterfaceCandidate("bridge0", ("169.254.1.2",), up=True, active=True, loopback=False),
                RemoteInterfaceCandidate("mgi0", ("10.0.1.7",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="ok",
        )

        match = interface_candidate_for_ip(probe, ("169.254.1.2", "10.0.1.7"))

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.iface, "mgi0")
        self.assertEqual(match.ip, "10.0.1.7")

    def test_derived_name_defaults_precedence_is_host_discovery_then_probe(self) -> None:
        discovered = BonjourResolvedService("Capsule", "capsule.local", ipv4=["192.168.1.72"])
        probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate("bridge0", ("192.168.1.217",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="ok",
        )

        from_host = derived_name_defaults({"TC_HOST": "root@10.0.1.7"}, discovered, probe)
        from_discovery = derived_name_defaults({"TC_HOST": "root@capsule.local"}, discovered, probe)
        from_probe = derived_name_defaults({"TC_HOST": "root@capsule.local"}, None, probe)

        self.assertEqual(from_host.netbios_name if from_host else None, "TimeCapsule007")
        self.assertEqual(from_discovery.netbios_name if from_discovery else None, "TimeCapsule072")
        self.assertEqual(from_probe.netbios_name if from_probe else None, "TimeCapsule217")

    def test_derived_name_defaults_ignores_link_local_sources(self) -> None:
        discovered = BonjourResolvedService("Capsule", "capsule.local", ipv4=["169.254.1.2"])
        probe = RemoteInterfaceCandidatesProbeResult(
            candidates=(
                RemoteInterfaceCandidate("bridge0", ("169.254.1.3",), up=True, active=True, loopback=False),
            ),
            preferred_iface="bridge0",
            detail="ok",
        )

        self.assertIsNone(derived_name_defaults({"TC_HOST": "root@169.254.1.1"}, discovered, probe))
        self.assertEqual(derived_prompt_defaults(None)["TC_NETBIOS_NAME"], "TimeCapsule")

    def test_saved_value_choice_rejects_invalid_saved_config_values(self) -> None:
        self.assertIsNone(
            saved_value_choice(
                {"TC_NETBIOS_NAME": "ABCDEFGHIJKLMNOP"},
                "TC_NETBIOS_NAME",
                "Samba NetBIOS name",
            )
        )
        self.assertEqual(
            saved_value_choice({"TC_NETBIOS_NAME": "Capsule"}, "TC_NETBIOS_NAME", "Samba NetBIOS name"),
            ConfigureValueChoice("Capsule", "saved"),
        )

    def test_saved_syap_must_match_detected_candidates(self) -> None:
        choice = ConfigureValueChoice("119", "saved")

        self.assertEqual(saved_syap_value_for_candidates(choice, ("119", "120")), "119")
        self.assertIsNone(saved_syap_value_for_candidates(choice, ("113",)))

if __name__ == "__main__":
    unittest.main()
