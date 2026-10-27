from __future__ import annotations

import ipaddress
import json
import plistlib
import sys
import socket
import struct
import tempfile
import time
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import subprocess

from timecapsulesmb.checks.bonjour import (
    DOCTOR_SERVICE_TYPES,
    build_expected_smb_instance,
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_smb_instance,
    check_smb_service_target,
    discover_smb_services_detailed,
    resolve_expected_smb_record,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record,
    select_resolved_smb_record_by_ip,
    select_smb_instance,
)
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.doctor_steps import _add_bonjour_results, check_xattr_tdb_persistence
from timecapsulesmb.checks.doctor_debug import _data_disk_unresponsive_result
from timecapsulesmb.checks.doctor_state import DoctorSink
from timecapsulesmb.checks.doctor_steps import (
    _add_admin_password_result,
    DEVICE_SMB_PASSED,
    DOCTOR_CODE_HOSTNAME_UNMAPPED,
    DOCTOR_STARTUP_WINDOW_SECONDS,
    DISK_NOT_MOUNTING_MESSAGE,
    DOCTOR_CODE_DISK_INFO,
    DOCTOR_CODE_DISK_INFO_UNAVAILABLE,
    DOCTOR_CODE_DISK_NOT_MOUNTING,
    DOCTOR_CODE_DISK_SMART_FAILING,
    _decimal_size,
    _disk_health_result,
    _doctor_check_disk_health,
    DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK,
    _doctor_device_blocks,
    NATIVE_NBNS_NOT_READY,
    NATIVE_NBNS_OFF,
    NATIVE_NBNS_READY,
    NATIVE_NBNS_STILL_STARTING,
)
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.doctor_steps import BONJOUR_OFF_LINK_CODE, DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK
from tests.test_client_network import router_plan
from tests.reboot_support import acp_password_answer, acp_reading
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.services.doctor import doctor_report
from timecapsulesmb.checks.network import LocalInterfaceNetwork, check_smb_port, check_ssh_login, closed_ssh_port_error
from timecapsulesmb.core.net import RouteSelection
from timecapsulesmb.checks.nbns import (
    NBNS_NEGATIVE_RESPONSE_CODE,
    NBNS_OFF_SUBNET_CODE,
    NBNS_NOT_SERVED_CODE,
    NBNS_QUERY_TIMEOUT_CODE,
    NbnsResponse,
    apple_nbns_client_on_subnet,
    build_nbns_query,
    check_nbns_name_resolution,
    parse_nbns_response,
)
from timecapsulesmb.checks.smb import (
    SmbClientTarget,
    check_authenticated_smb_file_ops_detailed,
    check_authenticated_smb_listing,
    parse_smbclient_disk_shares,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result
from timecapsulesmb.device.migration_jobs import MigrationActivity, MigrationProgress, RunningMigration
from timecapsulesmb.core.error_codes import ErrorCode
from timecapsulesmb.device.disk_errors import DISK_FAILING_WAY_OUT, disk_failing_message
from timecapsulesmb.device.probe import (
    lock_waiters_command,
    DISK_PAYLOAD_PROBLEMS,
    FILE_LOCKS_DISK_TDBS,
    FILE_LOCKS_RAM_TDBS,
    FOREIGN_WCIFSND_RUNNING,
    NATIVE_NBNS_OFF_DETAIL,
    AirportAcpReading,
    DeviceInterfaceIpv4,
    DeviceIpv4Entry,
    DeviceIpv4SubnetsProbeResult,
    DeviceNetworksProbeResult,
    UsbPrinterProbeResult,
    ipv4_address_kind,
    link_plan_ipv4_kinds,
    parse_ifconfig_interface_ipv4,
    parse_ifconfig_ipv4_entries,
    parse_ifconfig_networks,
    probe_device_networks_conn,
    DeviceHostnameProbeResult,
    DeployedVersionProbeResult,
    FLASH_RUNTIME_CONFIG,
    MDNS_REGISTRATIONS_DEGRADED,
    MDNS_REGISTRATIONS_STILL_STARTING,
    ManagerStartupAgeProbeResult,
    probe_manager_startup_age_conn,
    ProbedDeviceState,
    ProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
    RUNTIME_RAM_ROOT,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    SshAccessStatus,
    _parse_link_plan,
)
from timecapsulesmb.device.acpd import APPLE_ACPD_BROKEN_CODE
from timecapsulesmb.device.wipe import EraseLog, InMemoryCheck
from timecapsulesmb.device.storage import (
    MAST_ACP_COMMAND,
    MAST_PROBE_COMMAND,
    MAST_PROBE_TIMEOUT_SECONDS,
    MaStDiskSnapshot,
    MaStPartitionSnapshot,
    MaStProbeDiagnostics,
    MaStVolume,
)
from tests import test_storage_runtime
from timecapsulesmb.discovery.models import _merge_snapshots
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    BonjourServiceInstance,
)
from timecapsulesmb.transport.errors import (
    SshAlgorithmNegotiationError,
    SshAuthenticationError,
    SshClientConfigError,
    SshClientCrashedError,
    SshLocalNetworkFilteredError,
    SshNetworkError,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError, parse_ssh_client_diagnostics


DEFAULT_SMB_PORT_CHECK = object()
# The device the Doctor harness talks to: bridge0 on 10.0.0.0/24.
SAME_SUBNET_DEVICE_PROBE = DeviceIpv4SubnetsProbeResult(
    (DeviceIpv4Entry("bridge0", "10.0.0.2", "255.255.255.0", "10.0.0.255"),)
)
# This computer in the Doctor harness: on the device's network.
SAME_NETWORK_LOCAL_NETWORKS = (LocalInterfaceNetwork("en0", "10.0.0.50", ipaddress.ip_network("10.0.0.0/24")),)


def device_networks_probe(ipv4_probe) -> mock.Mock:
    """A probe_device_networks_conn mock built on an IPv4-subnet probe mock."""
    def probe(connection):
        result = ipv4_probe(connection)
        if isinstance(result, DeviceNetworksProbeResult):
            return result
        networks = tuple(dict.fromkeys(entry.network for entry in result.entries))
        return DeviceNetworksProbeResult(result.entries, networks, result.error)

    return mock.Mock(side_effect=probe)
REAL_SMB_PORT_CHECK = object()
DEFAULT_ACTIVE_SMB_CONF = """[global]
    netbios name = TimeCapsule
    xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb
[Data]
    path = /Volumes/dk2/ShareRoot
"""


def _nbns_owner_name(name: str) -> bytes:
    """Full (uncompressed) first-level encoded NetBIOS owner name, suffix 0x20."""
    raw = (name.upper()[:15].ljust(15) + "\x20").encode("latin-1")
    return b"\x20" + bytes(ord("A") + (c >> 4) if i % 2 == 0 else ord("A") + (c & 15)
                           for c in raw for i in range(2)) + b"\x00"



class FakeClock:
    """Doctor's clock (doctor_steps._clock) for tests: only sleeps and
    simulated work move it. With `delegate_sleep`, sleeps also go through
    time.sleep, which older tests patch and count."""

    def __init__(self, start: float = 1000.0, *, delegate_sleep: bool = False) -> None:
        self.start = self.now = start
        self.delegate_sleep = delegate_sleep
        self.sleeps: list[float] = []
        self.mdns_at: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.delegate_sleep:
            time.sleep(seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds

class CheckTests(unittest.TestCase):
    def smb_listing_result(self, server: str = "timecapsulesamba4.local", disk_shares: list[str] | None = None) -> CheckResult:
        return CheckResult("PASS", "listing ok", {"server": server, "disk_shares": ["Data"] if disk_shares is None else disk_shares})

    def dual_stack_discovery(
        self,
        ipv4: str | tuple[str, ...] = "10.0.0.2",
        ipv6: str = "fd00::2",
    ) -> mock.Mock:
        instance = BonjourServiceInstance(
            "_smb._tcp.local.", "Time Capsule Samba 4", "Time Capsule Samba 4._smb._tcp.local.",
        )
        snapshots = [
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                instance.name, "timecapsulesamba4.local", instance.service_type, port=445,
                ipv4=[ipv4] if isinstance(ipv4, str) else list(ipv4),
            )]),
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                instance.name, "timecapsulesamba4.local", instance.service_type, port=445, ipv6=[ipv6],
            )]),
        ]
        from timecapsulesmb.discovery.models import _merge_snapshots
        return mock.Mock(return_value=(_merge_snapshots(snapshots), None, None))

    def doctor_config(self, values: dict[str, str], *, exists: bool = True) -> AppConfig:
        return AppConfig.from_values(
            values,
            path=REPO_ROOT / ".env",
            exists=exists,
            file_values=values if exists else {},
        )

    def valid_doctor_values(self, **overrides: str) -> dict[str, str]:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        values.update(overrides)
        return values

    def runtime_identity_from_values(self, values: dict[str, str] | None = None) -> RuntimeNamingIdentityProbeResult:
        resolved = values or self.valid_doctor_values()
        return RuntimeNamingIdentityProbeResult(
            system_name=resolved.get("TC_MDNS_INSTANCE_NAME") or "Time Capsule Samba 4",
            hostname=resolved.get("TC_MDNS_HOST_LABEL") or "timecapsulesamba4",
            mdns_instance_name=resolved.get("TC_MDNS_INSTANCE_NAME") or "Time Capsule Samba 4",
            mdns_host_label=resolved.get("TC_MDNS_HOST_LABEL") or "timecapsulesamba4",
            netbios_name=resolved.get("TC_NETBIOS_NAME") or "TimeCapsule",
            detail="ok",
        )

    def run_ssh_with_active_smb_conf(
        self,
        *,
        active_smb_conf: str = DEFAULT_ACTIVE_SMB_CONF,
        other_stdout: str = "",
        returncode: int = 0,
    ):
        def fake_run_ssh(_connection: SshConnection, remote_cmd: str, **_kwargs: object):
            if RUNTIME_SMB_CONF in remote_cmd:
                return mock.Mock(returncode=0, stdout=active_smb_conf)
            return mock.Mock(returncode=returncode, stdout=other_stdout)

        return fake_run_ssh

    def mast_probe_diagnostics(self) -> MaStProbeDiagnostics:
        return MaStProbeDiagnostics(
            command=MAST_PROBE_COMMAND,
            returncode=0,
            volumes=(
                MaStVolume(
                    "sd0",
                    "dk2",
                    "/Volumes/dk2",
                    "Data",
                    "f42bdb83-c265-5522-a087-25606a4d0abf",
                    False,
                    "hfs",
                ),
            ),
            stdout="MaSt = (...)\n",
            stderr="",
        )

    def run_doctor_with_mocks(
        self,
        values: dict[str, str] | None = None,
        *,
        exists: bool = True,
        local_tools=None,
        artifacts=None,
        ssh_login=None,
        smb_port=DEFAULT_SMB_PORT_CHECK,
        smb_instance=None,
        smb_listing=None,
        smb_file_ops=None,
        run_ssh_stdout: str = "",
        run_ssh_returncode: int = 0,
        run_ssh_side_effect=None,
        read_active_smb_conf: str | None = None,
        xattr_result=None,
        smbd_probe=None,
        mdns_probe=None,
        connection=None,
        precomputed_probe_state=None,
        device_reading=None,
        skip_ssh: bool = False,
        skip_bonjour: bool = False,
        skip_smb: bool = False,
        debug_fields=None,
        on_result=None,
        on_stage=None,
        clock: FakeClock | None = None,
        runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None,
        deployed_config_present: bool = True,
        migration_activity: MigrationActivity | Exception | None = None,
        deployed_version: DeployedVersionProbeResult | None = None,
        runtime_ram_root_present: bool = True,
        client_source: str | None = None,
        device_subnets_probe: DeviceIpv4SubnetsProbeResult = SAME_SUBNET_DEVICE_PROBE,
        local_networks=SAME_NETWORK_LOCAL_NETWORKS,
        extra_patches: dict[str, object] | None = None,
        real_device_smb: bool = False,
        local_network_permission: str | None = None,
    ):
        resolved_values = values or self.valid_doctor_values()
        mocks = SimpleNamespace()
        with ExitStack() as stack:
            # A startup window is waited out on this clock, never in real time.
            mocks.clock = clock or FakeClock(delegate_sleep=True)
            stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps._clock", mocks.clock))
            mocks.check_nbns_name_resolution = stack.enter_context(mock.patch(
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution",
                return_value=CheckResult("PASS", "native NBNS resolved"),
            ))
            mocks.check_required_local_tools = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[] if local_tools is None else local_tools)
            )
            mocks.check_required_artifacts = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[] if artifacts is None else artifacts)
            )
            # This Mac's own nsmb.conf stays out of doctor-flow cases.
            mocks.check_nsmb_conf = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor_steps.check_nsmb_conf", return_value=([], None))
            )
            if ssh_login is not None:
                mocks.check_ssh_login = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.check_ssh_login", return_value=ssh_login))
            if smb_port is DEFAULT_SMB_PORT_CHECK:
                mocks.check_smb_port = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.checks.doctor_steps.check_smb_port",
                        return_value=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                    )
                )
            elif smb_port is not REAL_SMB_PORT_CHECK:
                mocks.check_smb_port = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_port", return_value=smb_port))
            if smb_instance is not None:
                mocks.check_smb_instance = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_instance", return_value=smb_instance))
            if smb_listing is not None:
                mocks.check_authenticated_smb_listing = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing", return_value=smb_listing)
                )
            if smb_file_ops is not None:
                mocks.check_authenticated_smb_file_ops_detailed = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed", return_value=smb_file_ops)
                )
            # The device's own SMB check reaches smbd through an SSH tunnel.
            # Cases about this computer's LAN checks leave it out, so their
            # smbclient mocks serve only the LAN listing.
            # smbclient is on this computer unless a case says otherwise (CI's may lack it).
            if "timecapsulesmb.checks.doctor_steps.command_exists" not in (extra_patches or {}):
                mocks.command_exists = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.command_exists", return_value=True)
                )
            if "timecapsulesmb.checks.doctor_steps.broken_smbclient_output" not in (extra_patches or {}):
                stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.broken_smbclient_output", return_value=None)
                )
            if not real_device_smb:
                mocks.device_smb = stack.enter_context(mock.patch(
                    "timecapsulesmb.checks.doctor_steps._add_tunneled_authenticated_smb_results",
                    return_value=DEVICE_SMB_PASSED,
                ))
            if "timecapsulesmb.checks.doctor_steps.ssh_local_forward" not in (extra_patches or {}):
                mocks.ssh_local_forward = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.ssh_local_forward", return_value=nullcontext())
                )
            if "timecapsulesmb.checks.doctor_steps.find_free_local_port" not in (extra_patches or {}):
                mocks.find_free_local_port = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.find_free_local_port", return_value=2445)
                )
            if run_ssh_side_effect is not None:
                mocks.run_ssh = stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=run_ssh_side_effect))
            else:
                mocks.run_ssh = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.device.probe.run_ssh",
                        return_value=mock.Mock(returncode=run_ssh_returncode, stdout=run_ssh_stdout),
                    )
                )
            mocks.read_active_smb_conf_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.read_active_smb_conf_conn",
                    return_value=DEFAULT_ACTIVE_SMB_CONF if read_active_smb_conf is None else read_active_smb_conf,
                )
            )
            if xattr_result is not None:
                mocks.check_xattr_tdb_persistence = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.check_xattr_tdb_persistence", return_value=xattr_result)
                )
            if smbd_probe is not None:
                mocks.probe_managed_smbd_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn", return_value=smbd_probe)
                )
            if mdns_probe is not None:
                mocks.probe_managed_mdns_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn", return_value=mdns_probe)
                )
            mocks.probe_remote_runtime_naming_identity_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn",
                    return_value=runtime_naming_identity or self.runtime_identity_from_values(resolved_values),
                )
            )
            mocks.probe_migration_activity = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.probe_migration_activity",
                    **({"side_effect": migration_activity} if isinstance(migration_activity, Exception)
                       else {"return_value": migration_activity or MigrationActivity(())}),
                )
            )
            mocks.flash_runtime_config_present_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.flash_runtime_config_present_conn",
                    return_value=deployed_config_present,
                )
            )
            mocks.read_deployed_version_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.read_deployed_version_conn",
                    return_value=deployed_version or DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok"),
                )
            )
            mocks.runtime_ram_root_present_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.runtime_ram_root_present_conn",
                    return_value=runtime_ram_root_present,
                )
            )
            mocks.select_route_to_address = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.select_route_to_address",
                    return_value=RouteSelection("unknown") if client_source is None else RouteSelection("available", source=client_source),
                )
            )
            mocks.probe_device_networks_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor_steps.probe_device_networks_conn",
                    device_networks_probe(mock.Mock(return_value=device_subnets_probe)),
                )
            )
            mocks.local_interface_networks = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor_steps.local_interface_networks", return_value=local_networks)
            )
            mocks.local_lan_networks = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor_steps.local_lan_networks", return_value=local_networks)
            )
            # Most Doctor cases supply Bonjour records and do not exercise DNS.
            # Keep those cases independent of the host's .local resolver while
            # DNS-specific cases still exercise their patched getaddrinfo calls.
            if "timecapsulesmb.core.net.socket.getaddrinfo" not in (extra_patches or {}):
                mocks.resolve_host_ips = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor_steps.resolve_host_ips", return_value=())
                )
                mocks.resolve_bonjour_host_ips = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=())
                )
                mocks.resolve_network_host_ips = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.network.resolve_host_ips", return_value=())
                )
            for index, (target, replacement) in enumerate((extra_patches or {}).items()):
                setattr(mocks, f"extra_{index}", stack.enter_context(mock.patch(target, replacement, create=True)))

            results, fatal = run_doctor_checks(
                self.doctor_config(resolved_values, exists=exists),
                repo_root=REPO_ROOT,
                connection=connection,
                precomputed_probe_state=precomputed_probe_state,
                device_reading=device_reading,
                skip_ssh=skip_ssh,
                skip_bonjour=skip_bonjour,
                skip_smb=skip_smb,
                local_network_permission=local_network_permission,
                on_result=on_result,
                on_stage=on_stage,
                debug_fields=debug_fields,
            )

        return SimpleNamespace(results=results, fatal=fatal, mocks=mocks)

    def setUp(self) -> None:
        self._exit_stack = ExitStack()
        # Every doctor run here waits on a fake clock, never in real time;
        # run_doctor_with_mocks swaps in its own per run.
        self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.checks.doctor_steps._clock", FakeClock(delegate_sleep=True))
        )
        default_bonjour_instance = BonjourServiceInstance(
            service_type="_smb._tcp.local.",
            name="Time Capsule Samba 4",
            fullname="Time Capsule Samba 4._smb._tcp.local.",
        )
        default_adisk_instance = BonjourServiceInstance(
            service_type="_adisk._tcp.local.",
            name="Time Capsule Samba 4",
            fullname="Time Capsule Samba 4._adisk._tcp.local.",
        )
        default_bonjour_record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            service_type="_smb._tcp.local.",
            port=445,
            ipv4=["10.0.0.2"],
        )
        default_adisk_record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            service_type="_adisk._tcp.local.",
            port=9,
            ipv4=["10.0.0.2"],
            properties={
                "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                "adVF": "0x1010",
                "dk2": "adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
            },
        )
        default_bonjour_snapshot = BonjourDiscoverySnapshot(
            instances=[default_bonjour_instance, default_adisk_instance],
            resolved=[default_bonjour_record, default_adisk_record],
        )
        default_bonjour_diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local.", "_adisk._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.0,
            ip_version="V4Only",
            instance_count=2,
            resolved_count=2,
            pending_count=0,
            service_added_count=1,
            service_updated_count=0,
            resolve_attempt_count=1,
            resolve_success_count=1,
            resolve_error_count=0,
            instances=[default_bonjour_instance, default_adisk_instance],
            resolved=[default_bonjour_record, default_adisk_record],
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed",
                return_value=(default_bonjour_snapshot, None, default_bonjour_diagnostics),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance",
                return_value=(default_bonjour_record, None),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.probe_connection_state",
                return_value=mock.Mock(
                    probe_result=mock.Mock(
                        ssh_authenticated=True,
                        error=None,
                        os_name="NetBSD",
                        os_release="6.0",
                        arch="earmv4",
                        elf_endianness="little",
                    ),
                    compatibility=DeviceCompatibility(
                        os_name="NetBSD",
                        os_release="6.0",
                        arch="earmv4",
                        elf_endianness="little",
                        payload_family="netbsd6_samba4",
                        device_generation="gen5",
                        supported=True,
                        reason_code="supported_netbsd6",
                    ),
                ),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.read_deployed_version_conn",
                return_value=DeployedVersionProbeResult(RELEASE_TAG, CLI_VERSION_CODE, "ok"),
            )
        )
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.flash_runtime_config_present_conn", return_value=True))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.runtime_ram_root_present_conn", return_value=True))
        # ACPd routes diskd's RPCs (the diskd guard kept its names).
        self._diskd_rpc_probe = self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.checks.doctor_steps.diskd_rpc_status_conn", return_value="answered")
        )
        # MaSt lists no disks unless a case gives it some.
        # The disk health step's own read is doctor_steps' only run_ssh.
        self._mast_read = self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.run_ssh",
            return_value=subprocess.CompletedProcess(["ssh"], 0, "MaSt=" + plistlib.dumps([]).decode(), ""),
        ))
        # The manager's log shows no erase unless a case gives it one.
        self._erase_log = self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.read_in_memory_volumes_conn", return_value=InMemoryCheck(EraseLog()),
        ))
        # A healthy device: the hostname is set and mapped for Samba.
        self._device_hostname_probe = self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.probe_device_hostname_conn",
            return_value=DeviceHostnameProbeResult(
                "timecapsulesamba4", ("127.0.0.1\ttimecapsulesamba4 timecapsulesamba4.local",)
            ),
        ))
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn",
                return_value=self.runtime_identity_from_values(),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn",
                return_value=mock.Mock(
                    ready=True,
                    detail="managed smbd ready",
                    lines=(
                        "PASS:managed runtime smb.conf present",
                        "PASS:managed smbd parent process is running",
                "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners",
                    ),
                ),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn",
                return_value=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.probe_managed_rsync_conn",
                return_value=mock.Mock(
                    ready=True,
                    detail="managed rsync disabled",
                    lines=(
                        "PASS:persistent rsync binary is executable",
                        "PASS:persistent rsync config is present",
                        "SKIP:rsync daemon is disabled and not running",
                    ),
                ),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip",
                return_value=mock.Mock(
                    status="PASS",
                    message="resolved Bonjour host timecapsulesamba4.local to 10.0.0.2 from service record",
                ),
            )
        )
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.discovery.bonjour.command_exists", return_value=False))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.resolve_host_ips", return_value=()))
        self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.resolve_smb_instance",
            side_effect=lambda instance, missing_message=None, **kwargs: (None, CheckResult(
                "FAIL", missing_message or f"discovered _smb._tcp instance {instance.name!r} but could not resolve service target")),
        ))
        # Doctor compares the device's networks with this computer's; keep tests
        # independent of the machine running them.
        self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.local_interface_networks", return_value=SAME_NETWORK_LOCAL_NETWORKS,
        ))
        # Direct doctor calls also check routes before their mocked SMB probe;
        # an offline runner must not reject those fixture addresses first.
        self._exit_stack.enter_context(mock.patch(
            "timecapsulesmb.checks.doctor_steps.select_route_to_address", return_value=RouteSelection("unknown"),
        ))
        self._exit_stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=DEFAULT_ACTIVE_SMB_CONF)))

    def tearDown(self) -> None:
        self._exit_stack.close()

    def test_run_doctor_checks_stops_invalid_config_before_remote_checks(self) -> None:
        ssh_login = mock.Mock()
        managed_smbd = mock.Mock()
        smb_port = mock.Mock()
        bonjour = mock.Mock()
        smb_listing = mock.Mock()

        with mock.patch("timecapsulesmb.checks.doctor_steps.check_ssh_login", ssh_login):
            with mock.patch("timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn", managed_smbd):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_port", smb_port):
                    with mock.patch("timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed", bonjour):
                        with mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing", smb_listing):
                            results, fatal = run_doctor_checks(
                                self.doctor_config(self.valid_doctor_values(), exists=False),
                                repo_root=REPO_ROOT,
                            )

        self.assertTrue(fatal)
        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("missing required configuration file", results[0].message)
        ssh_login.assert_not_called()
        managed_smbd.assert_not_called()
        smb_port.assert_not_called()
        bonjour.assert_not_called()
        smb_listing.assert_not_called()

    def test_run_doctor_checks_passes_when_deployed_version_matches_current_cli(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
        )

        self.assertTrue(
            any(
                result.status == "PASS" and result.message == f"deployed version matches current release {RELEASE_TAG}"
                for result in run.results
            )
        )

    def test_run_doctor_checks_passes_when_deployed_config_exists(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
        )

        self.assertTrue(
            any(
                result.status == "PASS" and result.message == f"deployed payload config {FLASH_RUNTIME_CONFIG} exists"
                for result in run.results
            )
        )

    def test_run_doctor_checks_reports_device_samba_version_pass_after_smbd_check(self) -> None:
        smbd_probe = mock.Mock(
            ready=True,
            detail="managed smbd ready",
            lines=(
                "PASS:managed runtime smbd binary present",
                "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners",
                "PASS:device Samba version: 4.24.3",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smbd_probe=smbd_probe,
            skip_bonjour=True,
            skip_smb=True,
        )

        version_result = next(result for result in run.results if result.message == "device Samba version: 4.24.3")
        smbd_result = next(result for result in run.results if result.message == "smbd owns IPv4 and IPv6 wildcard TCP 445 listeners")
        self.assertEqual(version_result.status, "PASS")
        self.assertEqual(version_result.details, {})
        self.assertLess(run.results.index(smbd_result), run.results.index(version_result))

    def test_run_doctor_checks_reports_device_samba_version_failure(self) -> None:
        smbd_probe = mock.Mock(
            ready=False,
            detail="device Samba version unavailable (exit code 1)",
            lines=(
                "PASS:managed runtime smbd binary present",
                "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners",
                "FAIL:device Samba version unavailable (exit code 1)",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smbd_probe=smbd_probe,
            skip_bonjour=True,
            skip_smb=True,
        )

        self.assertTrue(run.fatal)
        self.assertTrue(
            any(result.status == "FAIL" and result.message == "device Samba version unavailable (exit code 1)" for result in run.results)
        )

    def test_doctor_debug_context_records_the_naming_inputs_and_result(self) -> None:
        # syDN goes to telemetry beside syNm (system_dns_name beside
        # system_name), so a host label that follows it
        # can be explained from the report alone.
        debug_fields: dict[str, object] = {}
        identity = RuntimeNamingIdentityProbeResult(
            system_name="Time Capsule NM",
            hostname="time-capsule-dd5301",
            mdns_instance_name="Time Capsule NM",
            mdns_host_label="time-capsule-dd5301",
            netbios_name="time-capsule-dd",
            detail="ok",
            system_dns_name="Time Capsule dd5301",
        )
        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
            debug_fields=debug_fields,
            runtime_naming_identity=identity,
        )

        self.assertEqual(debug_fields["runtime_naming_identity"], {
            "system_name": "Time Capsule NM",
            "system_dns_name": "Time Capsule dd5301",
            "hostname": "time-capsule-dd5301",
            "mdns_instance_name": "Time Capsule NM",
            "mdns_host_label": "time-capsule-dd5301",
            "netbios_name": "time-capsule-dd",
        })

    def test_run_doctor_checks_stops_when_deployed_config_is_missing(self) -> None:
        debug_fields: dict[str, object] = {}
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            deployed_config_present=False,
            debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            run.results[-1].message,
            "installed Samba configuration not found; run \"Install / Update Samba\" in the macOS app, "
            "or run tcapsule deploy from the command line",
        )
        self.assertEqual(run.results[-1].details["code"], "runtime_not_installed")
        self.assertEqual(debug_fields["deployed_config_present"], False)
        run.mocks.read_deployed_version_conn.assert_not_called()
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_passes_when_runtime_ram_root_exists(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
        )

        self.assertTrue(
            any(
                result.status == "PASS" and result.message == f"managed runtime directory {RUNTIME_RAM_ROOT} exists"
                for result in run.results
            )
        )

    def test_run_doctor_checks_stops_when_runtime_ram_root_is_missing(self) -> None:
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            runtime_ram_root_present=False,
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            run.results[-1].message,
            f"managed runtime directory {RUNTIME_RAM_ROOT} is missing; run deploy or activate to start the managed runtime",
        )
        # Installed but not running: the app offers Activate on NetBSD4.
        self.assertEqual(run.results[-1].details, {"code": "runtime_not_started"})
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_stops_when_deployed_version_metadata_is_missing(self) -> None:
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            deployed_version=DeployedVersionProbeResult(None, None, "missing version metadata"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            run.results[-1].message,
            f"installed Samba payload has no version metadata; current version is {RELEASE_TAG}; "
            "run \"Install / Update Samba\" in the macOS app, or run tcapsule deploy from the command line",
        )
        run.mocks.flash_runtime_config_present_conn.assert_called_once()
        run.mocks.read_deployed_version_conn.assert_called_once()
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_tells_user_to_reboot_when_deployed_version_probe_fails(self) -> None:
        managed_smbd = mock.Mock()
        error = SshCommandTimeout(
            "Timed out waiting for ssh command to finish: /bin/sh -c 'config=/mnt/Flash/tcapsulesmb.conf; ...'"
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.read_deployed_version_conn": mock.Mock(side_effect=error),
                "timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd,
            },
        )

        self.assertTrue(run.fatal)
        self.assertIn("deployed payload version probe failed", run.results[-1].message)
        self.assertIn("reboot the device and rerun doctor", run.results[-1].message)
        self.assertIn("/mnt/Flash/tcapsulesmb.conf", run.results[-1].message)
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_stops_when_deployed_version_is_older(self) -> None:
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            deployed_version=DeployedVersionProbeResult("v2.1.0-rc3", CLI_VERSION_CODE - 1, "ok"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            run.results[-1].message,
            f"installed Samba version v2.1.0-rc3 is older than current {RELEASE_TAG}; "
            "run \"Install / Update Samba\" in the macOS app, or run tcapsule deploy from the command line",
        )
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_reports_a_running_migration_instead_of_the_old_version(self) -> None:
        managed_smbd = mock.Mock()
        activity = MigrationActivity(
            (RunningMigration(412, "copy", "/Volumes/dk2/.samba4/logs/xattr-migration-copy.log", "1:02.50"),),
            48213, "Oct 5 00:20:15 2026", MigrationProgress("copy", "u1", 120000),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            migration_activity=activity,
            deployed_version=DeployedVersionProbeResult("v2.2.9", CLI_VERSION_CODE - 1, "ok"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        last = run.results[-1]
        self.assertEqual(last.status, "FAIL")
        self.assertEqual(
            last.message,
            "a metadata migration is still running (copy phase, 120000 files checked); "
            "run \"Install / Update Samba\" in the macOS app, or tcapsule deploy from the command line: "
            "it waits for the migration to finish",
        )
        self.assertEqual(last.details, {"code": "metadata_migration_in_progress", "phase": "copy", "entries": 120000})
        self.assertFalse(any("older than current" in result.message for result in run.results))
        run.mocks.flash_runtime_config_present_conn.assert_not_called()
        run.mocks.read_deployed_version_conn.assert_not_called()
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_names_a_legacy_migration_without_a_position(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            migration_activity=MigrationActivity((RunningMigration(20, "legacy", None, "0:00.02"),)),
        )
        self.assertTrue(run.fatal)
        self.assertIn("still running (legacy phase); run", run.results[-1].message)

    def test_run_doctor_checks_omits_an_unknown_phase(self) -> None:
        # NetBSD 4's ps shows "(tc-xattr-hfs-mig)" instead of the migrator's arguments.
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            migration_activity=MigrationActivity((RunningMigration(1504, "unknown", None, "0:00.01"),)),
        )
        self.assertTrue(run.fatal)
        self.assertTrue(run.results[-1].message.startswith(
            "a metadata migration is still running; run \"Install / Update Samba\""))

    def test_run_doctor_checks_continues_when_the_migration_probe_fails_or_finds_none(self) -> None:
        for activity in (SshError("ps failed"), MigrationActivity(())):
            with self.subTest(activity=activity):
                run = self.run_doctor_with_mocks(
                    ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                    migration_activity=activity,
                    deployed_version=DeployedVersionProbeResult("v2.1.0-rc3", CLI_VERSION_CODE - 1, "ok"),
                )
                self.assertTrue(run.fatal)
                self.assertIn("is older than current", run.results[-1].message)
                self.assertFalse(any(r.details.get("code") == "metadata_migration_in_progress" for r in run.results))

    def test_run_doctor_checks_stops_when_deployed_version_is_newer(self) -> None:
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            deployed_version=DeployedVersionProbeResult("v2.1.0-rc5", CLI_VERSION_CODE + 1, "ok"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            run.results[-1].message,
            f"deployed version v2.1.0-rc5 is newer than this doctor {RELEASE_TAG}; please update before running doctor",
        )
        managed_smbd.assert_not_called()

    def test_run_doctor_checks_streams_results_until_deployed_version_stop(self) -> None:
        emitted: list[str] = []
        managed_smbd = mock.Mock()
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            deployed_version=DeployedVersionProbeResult(None, None, "missing version metadata"),
            on_result=lambda result: emitted.append(result.message),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": managed_smbd},
        )

        self.assertTrue(run.fatal)
        self.assertEqual([result.message for result in run.results], emitted)
        managed_smbd.assert_not_called()

    def test_check_smb_port_reports_local_socket_error(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value="[Errno 113] No route to host"):
            result = check_smb_port("10.0.0.2")

        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.message, "SMB not reachable at 10.0.0.2:445 ([Errno 113] No route to host)")
        self.assertEqual(result.details, {"error": "[Errno 113] No route to host"})

    def test_run_doctor_checks_adds_socket_debug_when_direct_smb_is_unreachable(self) -> None:
        debug_fields: dict[str, object] = {}
        socket_debug = "smbd:\nroot smbd 101 10 internet stream tcp 0x0 *:445\nnbns:\n(no internet sockets reported)"
        socket_debug_mock = mock.Mock(return_value=socket_debug)

        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("WARN", "SMB not reachable at 10.0.0.2:445 ([Errno 113] No route to host)"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.read_remote_service_socket_diagnostics_conn": socket_debug_mock,
            },
        )

        self.assertEqual(debug_fields["remote_service_sockets"], socket_debug)
        socket_debug_mock.assert_called_once()

    def test_run_doctor_checks_reports_unroutable_ipv6_as_info_and_checks_other_ipv6_prefix(self) -> None:
        routes = {
            "10.0.0.2": RouteSelection("available", source="10.0.0.9"),
            "fdbb::2": RouteSelection("unavailable", error="[Errno 65] No route to host", error_number=65),
            "fda3::2": RouteSelection("available", source="fda3::9"),
        }
        port_results = {
            "10.0.0.2": CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            "fda3::2": CheckResult("PASS", "SMB reachable at fda3::2:445"),
        }
        port_mock = mock.Mock(side_effect=port_results.__getitem__)
        instance = BonjourServiceInstance("_smb._tcp.local.", "Time Capsule Samba 4", "Time Capsule Samba 4._smb._tcp.local.")
        snapshots = [
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                "Time Capsule Samba 4", "timecapsulesamba4.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"],
            )]),
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                "Time Capsule Samba 4", "timecapsulesamba4.local", "_smb._tcp.local.", port=445,
                ipv6=["fdbb::2", "fda3::2"],
            )]),
        ]

        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            skip_smb=True,
            smb_port=REAL_SMB_PORT_CHECK,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(_merge_snapshots(snapshots), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.select_route_to_address": mock.Mock(side_effect=routes.__getitem__),
                "timecapsulesmb.checks.doctor_steps.check_smb_port": port_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertEqual(port_mock.call_args_list, [mock.call("10.0.0.2"), mock.call("fda3::2")])
        ipv6_info = next(result for result in run.results if "fdbb::2" in result.message)
        self.assertEqual(ipv6_info.status, "INFO")
        self.assertFalse(any(result.status == "WARN" and "fdbb::2" in result.message for result in run.results))

    def test_run_doctor_checks_warns_when_routable_ipv6_smb_fails(self) -> None:
        routes = {
            "10.0.0.2": RouteSelection("available", source="10.0.0.9"),
            "fd00::2": RouteSelection("available", source="fd00::9"),
        }
        port_results = {
            "10.0.0.2": CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            "fd00::2": CheckResult("WARN", "SMB not reachable at fd00::2:445 (connection timed out)", {"error": "connection timed out"}),
        }
        instance = BonjourServiceInstance("_smb._tcp.local.", "Time Capsule Samba 4", "Time Capsule Samba 4._smb._tcp.local.")
        snapshots = [
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                "Time Capsule Samba 4", "timecapsulesamba4.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"],
            )]),
            BonjourDiscoverySnapshot([instance], [BonjourResolvedService(
                "Time Capsule Samba 4", "timecapsulesamba4.local", "_smb._tcp.local.", port=445, ipv6=["fd00::2"],
            )]),
        ]
        listing = CheckResult("PASS", "authenticated SMB listing works over IPv4", {
            "server": "timecapsulesamba4.local", "ip_address": "10.0.0.2", "disk_shares": ["Data"], "attempts": [],
        })
        listing_mock = mock.Mock(return_value=listing)
        file_ops_mock = mock.Mock(return_value=[CheckResult("PASS", "file ops ok")])

        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            smb_port=REAL_SMB_PORT_CHECK,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(_merge_snapshots(snapshots), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.select_route_to_address": mock.Mock(side_effect=routes.__getitem__),
                "timecapsulesmb.checks.doctor_steps.check_smb_port": mock.Mock(side_effect=port_results.__getitem__),
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        ipv6_result = next(result for result in run.results if "fd00::2:445" in result.message)
        self.assertEqual(ipv6_result.status, "WARN")
        self.assertEqual(listing_mock.call_args.args[2], [SmbClientTarget("timecapsulesamba4.local", "10.0.0.2")])
        # SSH works, so the device changes files itself; the LAN only lists.
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_fails_when_enabled_native_nbns_does_not_answer(self) -> None:
        debug_fields: dict[str, object] = {}
        socket_debug_mock = mock.Mock(return_value="smbd:\n(no internet sockets reported)\nwcifsnd:\n(no internet sockets reported)")

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": mock.Mock(
                    return_value=CheckResult("FAIL", "NBNS query for 'TimeCapsule' timed out against 10.0.0.2:137")
                ),
                "timecapsulesmb.checks.doctor_debug.read_remote_service_socket_diagnostics_conn": socket_debug_mock,
            },
        )

        self.assertTrue(run.fatal)
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "FAIL")
        self.assertIn("timed out against 10.0.0.2:137", nbns_result.message)
        # A failed NBNS check records which processes own UDP 137/138.
        self.assertEqual(debug_fields["remote_service_sockets"], socket_debug_mock.return_value)
        socket_debug_mock.assert_called_once()

    def test_doctor_smb_servers_uses_probed_host_label(self) -> None:
        base_values = {"TC_HOST": "root@10.0.1.99"}
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(base_values), None, self.runtime_identity_from_values()),
            ["timecapsulesamba4.local", "10.0.1.99"],
        )
        self.assertEqual(
            doctor_smb_servers(AppConfig.from_values(base_values), None),
            ["10.0.1.99"],
        )

    def test_build_bonjour_expected_identity_uses_instance_host_label_and_ip_literal(self) -> None:
        identity = build_bonjour_expected_identity(
            AppConfig.from_values({
                "TC_HOST": "root@10.0.1.1",
            }),
            self.runtime_identity_from_values({
                "TC_MDNS_INSTANCE_NAME": "Home",
                "TC_MDNS_HOST_LABEL": "home",
                "TC_NETBIOS_NAME": "Home",
            }),
        )
        self.assertEqual(identity.instance_name, "Home")
        self.assertEqual(identity.host_label, "home")
        self.assertEqual(identity.target_ip, "10.0.1.1")

    def test_build_bonjour_expected_identity_keeps_a_link_local_zone(self) -> None:
        identity = build_bonjour_expected_identity(AppConfig.from_values({"TC_HOST": "root@fe80::82ea:96ff:fee6:5868%en0"}))

        self.assertEqual(identity.target_ip, "fe80::82ea:96ff:fee6:5868%en0")

    def test_scoped_target_selects_the_smb_record_on_its_interface(self) -> None:
        records = [
            BonjourResolvedService("Other", "other.local", "_smb._tcp.local.", ipv4=["10.0.0.9"], ipv6=["fe80::1%en1"]),
            BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", ipv4=["10.0.0.2"], ipv6=["fe80::1%en0"]),
        ]
        with mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", side_effect=lambda name: {"en0": 4, "en1": 5}[name]):
            self.assertEqual(select_resolved_smb_record_by_ip(records, "fe80::1%en0").name, "Home")
            self.assertEqual(select_resolved_smb_record_by_ip(records, "10.0.0.2").name, "Home")
            self.assertIsNone(select_resolved_smb_record_by_ip(records, "fe80::1"))

    def test_build_bonjour_expected_identity_ignores_non_ip_ssh_target(self) -> None:
        identity = build_bonjour_expected_identity(
            AppConfig.from_values({
                "TC_HOST": "root@timecapsule.local",
            }),
            self.runtime_identity_from_values({
                "TC_MDNS_INSTANCE_NAME": "Home",
                "TC_MDNS_HOST_LABEL": "home",
                "TC_NETBIOS_NAME": "Home",
            }),
        )
        self.assertEqual(identity.instance_name, "Home")
        self.assertEqual(identity.host_label, "home")
        self.assertIsNone(identity.target_ip)

    def selected_snapshot(self, *, name="Home", host="home.local", ipv4=("10.0.0.2",), ipv6=("fd00::2",), port=445):
        records = [
            BonjourResolvedService(name, host, "_smb._tcp.local.", port=port, ipv4=ipv4, ipv6=ipv6, fullname=f"{name}._smb._tcp.local."),
            BonjourResolvedService(name, host, "_airport._tcp.local.", port=5009, ipv4=ipv4, ipv6=ipv6, properties={"syAP": "119"}, fullname=f"{name}._airport._tcp.local."),
            BonjourResolvedService(name, host, "_adisk._tcp.local.", port=9, ipv4=ipv4, ipv6=ipv6,
                properties={"sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010", "dk2": "adVF=0x82,adVN=Data,adVU=12345678-1234-1234-1234-123456789012"}, fullname=f"{name}._adisk._tcp.local."),
        ]
        return BonjourDiscoverySnapshot([BonjourServiceInstance(r.service_type, r.name, r.fullname) for r in records], records)

    def run_selected_bonjour(self, snapshot, *, values=None, resolver=None, provider="dns-sd", discovery_error=None, local_networks=None, extra=None):
        from timecapsulesmb.discovery.bonjour import BonjourQueryDiagnostics
        debug = {}
        diagnostics = BonjourQueryDiagnostics(provider, [r.service_type for r in snapshot.resolved], 6, 6,
                                             len(snapshot.instances), len(snapshot.resolved))
        browse = mock.Mock(return_value=(snapshot, discovery_error, diagnostics))
        patches = {
            "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": browse,
            "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
            "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=provider == "dns-sd"),
        }
        if resolver is not None:
            patches["timecapsulesmb.checks.doctor_steps.resolve_smb_instance"] = resolver
        patches.update(extra or {})
        kwargs = {"local_networks": local_networks} if local_networks is not None else {}
        run = self.run_doctor_with_mocks(
            values or self.valid_doctor_values(TC_MDNS_INSTANCE_NAME="Home", TC_MDNS_HOST_LABEL="home"),
            smb_port=CheckResult("PASS", "445 ok"), ssh_login=CheckResult("PASS", "ssh ok"),
            skip_smb=True, debug_fields=debug, extra_patches=patches, **kwargs,
        )
        return run, debug, browse, diagnostics

    def test_doctor_validates_each_family_port_independently_of_record_order(self):
        from timecapsulesmb.discovery.models import _merge_snapshots
        for bad_family in ("ipv4", "ipv6"):
            for reverse in (False, True):
                with self.subTest(bad_family=bad_family, reverse=reverse):
                    v4 = self.selected_snapshot(ipv6=(), port=1445 if bad_family == "ipv4" else 445)
                    v6 = self.selected_snapshot(ipv4=(), port=1445 if bad_family == "ipv6" else 445)
                    snapshot = _merge_snapshots([v4, v6])
                    if reverse:
                        snapshot.resolved.reverse()
                    run, _debug, browse, _ = self.run_selected_bonjour(snapshot)
                    self.assertTrue(run.fatal)
                    browse.assert_called_once()
                    self.assertTrue(any(r.status == "FAIL" and f"Bonjour {bad_family.replace('ip', 'IP')}: _smb._tcp port is 1445" in r.message for r in run.results))

    def test_doctor_retains_conflicting_same_family_ports_and_txt(self):
        from copy import deepcopy
        for service, change in (("_smb", "port"), ("_adisk", "txt")):
            for reverse in (False, True):
                with self.subTest(service=service, reverse=reverse):
                    snapshot = self.selected_snapshot()
                    conflicting = deepcopy(next(r for r in snapshot.resolved if r.service_type.startswith(service)))
                    if change == "port":
                        conflicting.port = 1445
                    else:
                        conflicting.properties["dk2"] = "adVF=0x82,adVN=Wrong,adVU=another-volume"
                    snapshot.resolved.append(conflicting)
                    if reverse:
                        snapshot.resolved.reverse()
                    run, *_ = self.run_selected_bonjour(snapshot)
                    self.assertTrue(run.fatal)
                    self.assertTrue(any(r.status == "FAIL" and "conflicting target, port or TXT" in r.message for r in run.results))

    def test_doctor_ignores_same_name_peer_on_another_observed_link(self):
        snapshot = self.selected_snapshot()
        for record in snapshot.resolved:
            record.interface_index = 14
        for instance in snapshot.instances:
            instance.interface_index = 14
        peer = self.selected_snapshot(ipv4=("10.0.1.9",), ipv6=("fd01::9",), port=1445)
        for record in peer.resolved:
            record.interface_index = 18
        for instance in peer.instances:
            instance.interface_index = 18
        snapshot.resolved = peer.resolved + snapshot.resolved
        snapshot.instances = peer.instances + snapshot.instances
        run, *_ = self.run_selected_bonjour(snapshot)
        self.assertFalse(run.fatal)
        self.assertFalse(any("1445" in r.message for r in run.results))

    def test_doctor_accepts_separate_valid_family_observations(self):
        from timecapsulesmb.discovery.models import _merge_snapshots
        snapshot = _merge_snapshots([self.selected_snapshot(ipv6=()), self.selected_snapshot(ipv4=())])
        run, _debug, browse, _ = self.run_selected_bonjour(snapshot)
        self.assertFalse(run.fatal)
        browse.assert_called_once()
        for family in ("IPv4", "IPv6"):
            self.assertTrue(any(r.status == "PASS" and f"Bonjour {family}: resolved _smb" in r.message for r in run.results))

    def test_doctor_validates_adisk_independently_of_its_address_family(self):
        for family in ("ipv4", "ipv6"):
            for reverse in (False, True):
                for problem in (None, "missing", "txt", "target"):
                    with self.subTest(family=family, reverse=reverse, problem=problem):
                        snapshot = self.selected_snapshot()
                        adisk = next(r for r in snapshot.resolved if r.service_type.startswith("_adisk."))
                        setattr(adisk, "ipv6" if family == "ipv4" else "ipv4", [])
                        if problem == "missing":
                            snapshot.resolved.remove(adisk)
                            snapshot.instances = [i for i in snapshot.instances if not i.service_type.startswith("_adisk.")]
                        elif problem == "txt":
                            adisk.properties["dk2"] = "adVF=0x82,adVN=Wrong,adVU=another-volume"
                        elif problem == "target":
                            adisk.hostname = "wrong.local"
                        if reverse:
                            snapshot.resolved.reverse()
                        run, *_ = self.run_selected_bonjour(snapshot)
                        self.assertEqual(run.fatal, problem is not None)
                        # Both family checks must validate the advertisement, even
                        # when only the other family's address lookup completed.
                        other_family = "IPv6" if family == "ipv4" else "IPv4"
                        results = [r for r in run.results if r.message.startswith(f"Bonjour {other_family}:")]
                        if problem is None:
                            self.assertTrue(any(r.status == "PASS" and "discovered _adisk" in r.message for r in results))
                            self.assertFalse(any(r.status == "FAIL" for r in results))
                        else:
                            self.assertTrue(any(r.status == "FAIL" and "_adisk" in r.message for r in results))

    def test_run_doctor_checks_adds_bonjour_debug_on_instance_mismatch(self):
        snapshot = self.selected_snapshot(name="Kitchen", host="kitchen.local", ipv4=("10.0.0.99",), ipv6=())
        run, debug, browse, diagnostics = self.run_selected_bonjour(snapshot)
        self.assertTrue(run.fatal)
        self.assertEqual(debug["bonjour_expected"]["instance_name"], "Home")
        self.assertIs(debug["bonjour_discovery"], diagnostics)
        self.assertTrue(any("expected device instance 'Home'" in r.message for r in run.results))
        browse.assert_called_once()

    def test_run_doctor_checks_does_not_run_secondary_browse_when_bonjour_matches(self):
        run, debug, browse, _diagnostics = self.run_selected_bonjour(self.selected_snapshot())
        self.assertFalse(run.fatal)
        browse.assert_called_once()
        self.assertNotIn("bonjour_discovery", debug)
        self.assertTrue(any(r.status == "PASS" and "Bonjour IPv4:" in r.message for r in run.results))
        self.assertTrue(any(r.status == "PASS" and "Bonjour IPv6:" in r.message for r in run.results))

    def test_run_doctor_checks_reads_afp_from_adisk_flags_not_afp_browse(self):
        for advf, advertise_afp, cached_afp_ptr, failure in (
            # A deploy reboot sends no goodbye, so the Mac can still list the old AFP service.
            ("0x82", False, True, None),
            ("0x83", True, False, None),
            ("0x83", False, False, "advertises AFP (dk2 adVF=0x83) although Advertise AFP over Bonjour is off"),
            ("0x82", True, True, "advertises SMB only (dk2 adVF=0x82) although Advertise AFP over Bonjour is on"),
        ):
            with self.subTest(advf=advf, advertise_afp=advertise_afp, cached_afp_ptr=cached_afp_ptr):
                snapshot = self.selected_snapshot()
                adisk = next(r for r in snapshot.resolved if r.service_type.startswith("_adisk."))
                adisk.properties["dk2"] = f"adVF={advf},adVN=Data,adVU=12345678-1234-1234-1234-123456789012"
                if cached_afp_ptr:
                    snapshot.instances.append(BonjourServiceInstance("_afpovertcp._tcp.local.", "Home", "Home._afpovertcp._tcp.local."))
                values = self.valid_doctor_values(TC_MDNS_INSTANCE_NAME="Home", TC_MDNS_HOST_LABEL="home",
                                                  TC_MDNS_ADVERTISE_AFP="true" if advertise_afp else "false")
                run, debug, _browse, diagnostics = self.run_selected_bonjour(snapshot, values=values)
                failures = [r.message for r in run.results if r.status == "FAIL"]
                if failure is None:
                    mode = "AFP and SMB" if advertise_afp else "SMB only"
                    self.assertFalse(run.fatal, failures)
                    self.assertNotIn("bonjour_discovery", debug)
                    self.assertTrue(any(r.status == "PASS" and f"_adisk._tcp TXT advertises {mode} as configured" in r.message
                                        for r in run.results))
                else:
                    self.assertTrue(run.fatal)
                    self.assertTrue(any(failure in m for m in failures), failures)
                    self.assertIs(debug["bonjour_discovery"], diagnostics)

    def test_run_doctor_checks_resolves_expected_smb_when_browse_misses_instance(self) -> None:
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME="Home",
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        airport_instance = BonjourServiceInstance("_airport._tcp.local.", "Home", "Home._airport._tcp.local.")
        adisk_instance = BonjourServiceInstance("_adisk._tcp.local.", "Home", "Home._adisk._tcp.local.")
        airport_record = BonjourResolvedService(
            "Home",
            "home.local",
            "_airport._tcp.local.",
            port=5009,
            ipv4=["10.0.0.2"],
        )
        adisk_record = BonjourResolvedService(
            "Home",
            "home.local",
            "_adisk._tcp.local.",
            port=9,
            properties={
                "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                "adVF": "0x1010",
                "dk2": "adVF=0x82,adVN=Data,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
            },
        )
        diagnostics = BonjourDiscoveryDiagnostics(
            service=None,
            service_types=["_airport._tcp.local.", "_smb._tcp.local.", "_adisk._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.0,
            ip_version="V4Only",
            instance_count=2,
            resolved_count=2,
            pending_count=0,
            service_added_count=1,
            service_updated_count=0,
            resolve_attempt_count=1,
            resolve_success_count=1,
            resolve_error_count=0,
            instances=[airport_instance, adisk_instance],
            resolved=[airport_record, adisk_record],
        )
        resolved_smb = BonjourResolvedService(
            "Home",
            "home.local",
            "_smb._tcp.local.",
            port=445,
            ipv4=["10.0.0.2"],
            fullname="Home._smb._tcp.local.",
        )
        resolve_mock = mock.Mock(return_value=(resolved_smb, None))
        debug_fields: dict[str, object] = {}

        run = self.run_doctor_with_mocks(
            values,
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([airport_instance, adisk_instance], [airport_record, adisk_record]), None, diagnostics)
                ),
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": resolve_mock,
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
            },
        )

        self.assertFalse(run.fatal)
        self.assertNotIn("bonjour_native_dns_sd", debug_fields)
        self.assertNotIn("bonjour_native_dns_sd_error", debug_fields)
        self.assertEqual(resolve_mock.call_count, 2)
        first_resolve = resolve_mock.call_args_list[0]
        resolved_instance = first_resolve.args[0]
        self.assertEqual(resolved_instance, build_expected_smb_instance("Home"))
        self.assertEqual(first_resolve.kwargs["target_ip"], "10.0.0.2")
        self.assertEqual(first_resolve.kwargs["family"], "ipv4")
        self.assertIsNone(first_resolve.kwargs["interfaces"])
        self.assertIn("targeted query", first_resolve.kwargs["missing_message"])
        messages = [result.message for result in run.results]
        self.assertIn(
            "Bonjour IPv4: Bonjour browse did not observe expected _smb._tcp instance 'Home'; targeted resolve succeeded",
            messages,
        )
        self.assertIn("Bonjour IPv4: resolved expected _smb._tcp instance 'Home' by targeted query", messages)
        self.assertIn("Bonjour IPv4: resolved _smb._tcp instance 'Home' to home.local:445", messages)
        self.assertIn("Bonjour IPv4: resolved Bonjour host home.local to 10.0.0.2 from service record", messages)

    def test_run_doctor_checks_fails_when_targeted_smb_resolve_returns_wrong_ip(self) -> None:
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME="Home",
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        wrong_smb = BonjourResolvedService(
            "Home",
            "home.local",
            "_smb._tcp.local.",
            port=445,
            ipv4=["10.0.0.99"],
            fullname="Home._smb._tcp.local.",
        )
        debug_fields: dict[str, object] = {}

        run = self.run_doctor_with_mocks(
            values,
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([], []), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": mock.Mock(return_value=(wrong_smb, None)),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(
                    return_value=False
                ),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
            },
        )

        self.assertTrue(run.fatal)
        self.assertTrue(
            any(
                result.status == "FAIL"
                and result.message == "Bonjour IPv4: Bonjour host home.local resolved to 10.0.0.99, expected 10.0.0.2"
                for result in run.results
            )
        )
        self.assertIn("bonjour_expected", debug_fields)

    def test_run_doctor_checks_fails_when_browse_and_targeted_smb_resolve_miss(self) -> None:
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME="Home",
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        resolve_error = CheckResult(
            "FAIL",
            "expected _smb._tcp instance 'Home' was not discovered and could not be resolved by targeted query",
        )
        debug_fields: dict[str, object] = {}

        run = self.run_doctor_with_mocks(
            values,
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([], []), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": mock.Mock(return_value=(None, resolve_error)),
            },
        )

        self.assertTrue(run.fatal)
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: no discovered _smb._tcp instance matched expected device instance 'Home'", messages)
        self.assertIn(
            "Bonjour IPv4: expected _smb._tcp instance 'Home' was not discovered and could not be resolved by targeted query",
            messages,
        )

    OFF_NETWORK = (LocalInterfaceNetwork("en0", "192.168.50.10", ipaddress.ip_network("192.168.50.0/24")),)
    OFF_NETWORK_BONJOUR_SKIP = (
        "Bonjour check skipped; this computer (192.168.50.0/24) is not on the device's network (10.0.0.0/24). "
        "Bonjour only reaches devices on the same network, so SMB is checked by address instead"
    )

    def _run_doctor_bonjour_miss(self, *, local_networks, resolved=None, debug_fields=None, target_host="root@10.0.0.2", **kwargs):
        """Doctor where Bonjour finds nothing for the device unless `resolved` is given."""
        values = self.valid_doctor_values(
            TC_HOST=target_host,
            TC_MDNS_INSTANCE_NAME="Home",
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        resolve_error = CheckResult(
            "FAIL",
            "expected _smb._tcp instance 'Home' was not discovered and could not be resolved by targeted query",
        )
        extra_patches = {
            "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                return_value=(BonjourDiscoverySnapshot([], []), None, None)
            ),
            "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": mock.Mock(
                return_value=(resolved, None) if resolved is not None else (None, resolve_error)
            ),
            **kwargs.pop("extra_patches", {}),
        }
        kwargs.setdefault("ssh_login", mock.Mock(status="PASS", message="ssh ok"))
        return self.run_doctor_with_mocks(
            values,
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            local_networks=local_networks,
            extra_patches=extra_patches,
            **kwargs,
        )

    def test_bonjour_miss_off_the_device_network_is_one_skip(self) -> None:
        debug_fields: dict[str, object] = {}
        nbns = mock.Mock(return_value=self._nbns_query_timeout())

        run = self._run_doctor_bonjour_miss(
            local_networks=self.OFF_NETWORK,
            debug_fields=debug_fields,
            client_source="192.168.50.10",
            extra_patches={"timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns},
        )

        self.assertFalse(run.fatal)
        bonjour = [result for result in run.results if result.message.startswith("Bonjour")]
        self.assertEqual([(result.status, result.message) for result in bonjour], [("SKIP", self.OFF_NETWORK_BONJOUR_SKIP)])
        self.assertEqual(bonjour[0].details["code"], BONJOUR_OFF_LINK_CODE)
        self.assertIn(f"advertised Bonjour instance: unavailable ({self.OFF_NETWORK_BONJOUR_SKIP})",
                      [result.message for result in run.results])
        self.assertEqual(debug_fields["bonjour_link"], {
            "verdict": "separate",
            "source": "device_ifconfig",
            "families": ["ipv4"],
            "device_networks": ["10.0.0.0/24"],
            "local_networks": ["192.168.50.0/24"],
            "detail": None,
            "skipped": ["bonjour"],
        })
        self.assertNotIn("bonjour_discovery", debug_fields)
        # NBNS's off-subnet check reuses the same ifconfig.
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        run.mocks.probe_device_networks_conn.assert_called_once()

    def test_off_network_bonjour_skip_accounts_for_selected_results_in_each_family(self):
        for family in ("ipv4", "ipv6"):
            for scenario in ("wrong_port", "wrong_host", "wrong_address", "foreign", "unresolved", "absent", "query_error", "targeted_query_error", "valid"):
                with self.subTest(family=family, scenario=scenario):
                    address = "10.0.0.2" if family == "ipv4" else "fd00::2"
                    values = self.valid_doctor_values(TC_HOST="root@" + address, TC_MDNS_INSTANCE_NAME="Home", TC_MDNS_HOST_LABEL="home")
                    ip = "10.0.0.99" if family == "ipv4" else "fd00::99"
                    snapshot = self.selected_snapshot(
                        host="foreign.local" if scenario in {"wrong_host", "foreign"} else "home.local",
                        ipv4=((ip if scenario in {"wrong_address", "foreign"} else address),) if family == "ipv4" else (),
                        ipv6=((ip if scenario in {"wrong_address", "foreign"} else address),) if family == "ipv6" else (),
                        port=1445 if scenario == "wrong_port" else 445)
                    if scenario in {"unresolved", "absent", "query_error", "targeted_query_error"}:
                        snapshot.resolved = []
                    if scenario in {"absent", "query_error", "targeted_query_error"}:
                        snapshot.instances = []
                    error = CheckResult("FAIL", "Bonjour query failed") if scenario == "query_error" else None
                    run, _debug, browse, _diagnostics = self.run_selected_bonjour(snapshot, values=values,
                        local_networks=self.OFF_NETWORK, discovery_error=error,
                        extra={"timecapsulesmb.discovery.bonjour.BonjourQuery.resolve": mock.Mock(side_effect=RuntimeError("targeted query failed")),
                               "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": resolve_smb_instance}
                              if scenario == "targeted_query_error" else None)
                    skipped = scenario == "absent"
                    self.assertEqual(run.fatal, scenario != "valid" and not skipped)
                    self.assertEqual(any(r.details.get("code") == BONJOUR_OFF_LINK_CODE for r in run.results), skipped)
                    browse.assert_called_once()

    def test_bonjour_miss_on_the_device_network_still_fails(self) -> None:
        debug_fields: dict[str, object] = {}

        run = self._run_doctor_bonjour_miss(local_networks=SAME_NETWORK_LOCAL_NETWORKS, debug_fields=debug_fields)

        self.assertTrue(run.fatal)
        self.assertIn(
            "Bonjour IPv4: expected _smb._tcp instance 'Home' was not discovered and could not be resolved by targeted query",
            [result.message for result in run.results if result.status == "FAIL"],
        )
        self.assertEqual(debug_fields["bonjour_link"]["verdict"], "shared")
        self.assertEqual(debug_fields["bonjour_link"]["skipped"], [])

    def test_bonjour_miss_still_fails_when_the_networks_cannot_be_compared(self) -> None:
        ipv6_only = (LocalInterfaceNetwork("en0", "2001:db8:9::10", ipaddress.ip_network("2001:db8:9::/64")),)
        debug_fields: dict[str, object] = {}

        run = self._run_doctor_bonjour_miss(local_networks=ipv6_only, debug_fields=debug_fields)

        self.assertTrue(run.fatal)
        self.assertFalse(any(result.status == "SKIP" and result.message.startswith("Bonjour") for result in run.results))
        self.assertEqual(debug_fields["bonjour_link"]["verdict"], "unknown")

    def test_bonjour_record_of_another_device_still_fails_off_the_device_network(self) -> None:
        # Records that were seen but do not match are a real problem wherever this computer is.
        other_device = BonjourResolvedService(
            "Home", "other-home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.99"], fullname="Home._smb._tcp.local.",
        )
        debug_fields: dict[str, object] = {}

        run = self._run_doctor_bonjour_miss(local_networks=self.OFF_NETWORK, resolved=other_device, debug_fields=debug_fields)

        self.assertTrue(run.fatal)
        failures = [result.message for result in run.results if result.status == "FAIL"]
        self.assertTrue(any("belongs to another device" in message for message in failures), failures)
        self.assertNotIn("bonjour_link", debug_fields)

    WAN_SIDE = (LocalInterfaceNetwork("en0", "192.168.1.170", ipaddress.ip_network("192.168.1.0/24")),)
    ROUTER_LAN = (LocalInterfaceNetwork("en0", "10.0.1.20", ipaddress.ip_network("10.0.1.0/24")),)

    def _run_doctor_against_router(self, *, local_networks, plan_text=None, **kwargs):
        """Doctor against a router-mode device whose link plan shares disks on
        10.0.1.0/24 only. Records what ran from this computer."""
        mdns = ReadinessProbeResult(
            ready=True,
            detail="managed mDNS registrant active",
            steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("native_nbns", "pass", "Apple wcifsnd is ready on UDP 137 and 138"),
            ),
            link_plan=_parse_link_plan(router_plan() if plan_text is None else plan_text),
        )
        calls = SimpleNamespace(
            discover=mock.Mock(return_value=(BonjourDiscoverySnapshot([], []), None, None)),
            nbns=mock.Mock(return_value=CheckResult("PASS", "native NBNS resolved")),
            listing=mock.Mock(return_value=self.smb_listing_result()),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            mdns_probe=mdns,
            local_networks=local_networks,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": calls.discover,
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": calls.nbns,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": calls.listing,
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": mock.Mock(
                    return_value=(None, CheckResult("FAIL", "expected _smb._tcp instance was not discovered")),
                ),
            },
            **kwargs,
        )
        return run, calls

    def test_doctor_from_the_wan_side_fails_first_and_checks_nothing_from_here(self) -> None:
        with mock.patch("timecapsulesmb.checks.doctor_steps.sys.platform", "linux"):
            run, calls = self._run_doctor_against_router(local_networks=self.WAN_SIDE)

        self.assertTrue(run.fatal)
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.details.get("code") for result in failures], [DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK])
        self.assertTrue(failures[0].message.startswith(
            "this computer is on the device's internet (WAN) side (192.168.1.0/24), where the device does not share its disks"
        ))
        # Nothing that needs this computer on a disk network ran, and no
        # Bonjour, SMB or NBNS result says it failed.
        calls.discover.assert_not_called()
        calls.nbns.assert_not_called()
        calls.listing.assert_not_called()
        run.mocks.check_smb_port.assert_not_called()
        run.mocks.probe_device_networks_conn.assert_not_called()
        self.assertFalse(any(result.message.startswith(("Bonjour", "SMB", "NBNS", "advertised Bonjour")) for result in run.results))
        # Checks that run on the device still report.
        messages = [result.message for result in run.results]
        self.assertTrue(any(message.startswith("active Samba share names:") for message in messages), messages)
        self.assertTrue(any(message == "discovery process is running" for message in messages), messages)

    def test_doctor_over_a_cable_to_the_wan_port_fails_first_by_the_address_it_reached(self) -> None:
        # The Mac's own network is elsewhere; its cable to the WAN port has
        # only self-assigned addresses, so no network tells the links apart.
        plan = router_plan(
            wan_ipv4=None, wan_link_local="169.254.247.225", lan_link_local=None, session_server="169.254.247.225")
        elsewhere = (LocalInterfaceNetwork("en0", "192.168.50.10", ipaddress.ip_network("192.168.50.0/24")),)
        with mock.patch("timecapsulesmb.checks.doctor_steps.sys.platform", "darwin"):
            run, calls = self._run_doctor_against_router(local_networks=elsewhere, plan_text=plan)

        self.assertTrue(run.fatal)
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.details.get("code") for result in failures], [DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK])
        self.assertEqual(failures[0].details["matched_by"], "session_address")
        self.assertTrue(failures[0].message.startswith(
            "this Mac is plugged straight into the device's internet (WAN) port, where the device does not share its disks"
        ))
        calls.discover.assert_not_called()
        calls.nbns.assert_not_called()
        calls.listing.assert_not_called()
        run.mocks.check_smb_port.assert_not_called()
        self.assertFalse(any(result.message.startswith(("Bonjour", "SMB", "NBNS")) for result in run.results))

    UNREACHABLE = CheckResult(
        "FAIL",
        "The device is not answering at 10.0.0.2: neither SSH port 22 nor AirPort ACP port 5009 is reachable (timed out).",
        {"code": "device_unreachable"},
    )

    def run_doctor_against_a_dead_address(self, airports, *, values=None, skip_bonjour=False):
        query = mock.Mock()
        query.return_value.browse.return_value = (BonjourDiscoverySnapshot([], airports), None)
        smb_browse = mock.Mock()
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=self.UNREACHABLE,
            skip_bonjour=skip_bonjour,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.BonjourQuery": query,
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": smb_browse,
            },
        )
        return run, query, smb_browse

    def airport(self, name: str, address: str, mac: str) -> BonjourResolvedService:
        return BonjourResolvedService(
            name=name, hostname=f"{name.replace(' ', '-')}.local", service_type="_airport._tcp.local.", port=5009,
            ipv4=[address], services={"_airport._tcp.local."}, properties={"waMA": mac}, fullname=f"{name}._airport._tcp.local.",
        )

    def test_doctor_at_a_dead_address_lists_the_airports_instead_of_repeating_the_failure(self) -> None:
        # Telemetry install 079bca09: the device had moved, and Bonjour and SMB
        # checks of its old address failed again without saying where it was.
        values = {**self.valid_doctor_values(), "TC_AIRPORT_MAC": "64-A5-C3-62-74-71"}
        run, query, smb_browse = self.run_doctor_against_a_dead_address([
            self.airport("AirPort Time Capsule 1", "192.168.178.193", "90:72:40:0A:6F:C0"),
            self.airport("AirPort Time Capsule 3", "192.168.178.75", "64:A5:C3:62:74:71"),
        ], values=values)

        self.assertTrue(run.fatal)
        self.assertEqual([r.details.get("code") for r in run.results if r.status == "FAIL"], ["device_unreachable"])
        skipped = next(r for r in run.results if r.details.get("code") == "network_not_checked")
        self.assertEqual((skipped.status, skipped.message), ("SKIP", "Bonjour, SMB and NBNS checks skipped: nothing answers at 10.0.0.2"))
        smb_browse.assert_not_called()
        run.mocks.check_smb_port.assert_not_called()
        query.return_value.browse.assert_called_once_with("_airport", timeout=2.0)
        seen = next(r for r in run.results if r.message.startswith("AirPorts this computer sees:"))
        self.assertEqual(seen.message, (
            "AirPorts this computer sees: AirPort Time Capsule 1 at 192.168.178.193; "
            "AirPort Time Capsule 3 at 192.168.178.75 (this device)"
        ))
        self.assertEqual(seen.details["airports"][1]["airport_mac"], "64:a5:c3:62:74:71")

    def test_doctor_at_a_dead_address_still_finishes_when_the_airport_list_fails(self) -> None:
        # The list is only a hint: a browse that fails says so and doctor goes on.
        query = mock.Mock()
        query.return_value.browse.side_effect = OSError("dns-sd exited with status 1")
        run = self.run_doctor_with_mocks(
            None,
            ssh_login=self.UNREACHABLE,
            extra_patches={"timecapsulesmb.checks.doctor_steps.BonjourQuery": query},
        )
        self.assertEqual([r.details.get("code") for r in run.results if r.status == "FAIL"], ["device_unreachable"])
        self.assertIn("could not list the AirPorts this computer sees: dns-sd exited with status 1",
                      [r.message for r in run.results if r.status == "INFO"])
        self.assertTrue(any(r.details.get("code") == "network_not_checked" for r in run.results))

    def test_doctor_with_a_stuck_acpd_reports_it_and_still_checks_the_network(self) -> None:
        # The device answers on port 22, so its address is not dead: Bonjour
        # and SMB may still work, and the ACPd step, which needs SSH, does not run.
        stuck = CheckResult(
            "FAIL",
            "Apple ACPd is broken: the AirPort service does not answer the network, a bug in Apple's firmware. "
            "AirPort ACP port 5009 does not answer at 10.0.0.2 (timed out), while SSH port 22 refused a connection.",
            {"code": "apple_acpd_broken", "acpd_problem": "acp_port_unanswered", "acp_port_error": "timed out"},
        )
        debug_fields: dict[str, object] = {}
        run = self.run_doctor_with_mocks(
            None,
            ssh_login=stuck,
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            debug_fields=debug_fields,
        )

        self.assertEqual([r.details.get("code") for r in run.results if r.status == "FAIL"], ["apple_acpd_broken"])
        self.assertFalse(any(r.details.get("code") == "network_not_checked" for r in run.results))
        run.mocks.check_smb_port.assert_called()
        self.assertEqual(debug_fields["acpd_problems"], ["acp_port_unanswered"])

    def test_doctor_at_a_dead_address_says_when_no_airport_answers_or_bonjour_is_skipped(self) -> None:
        run, _query, _smb = self.run_doctor_against_a_dead_address([])
        self.assertIn("no AirPort answers Bonjour on this computer's networks", [r.message for r in run.results])

        run, query, _smb = self.run_doctor_against_a_dead_address([], skip_bonjour=True)
        query.assert_not_called()
        self.assertTrue(any(r.details.get("code") == "network_not_checked" for r in run.results))

    def test_doctor_from_the_wan_side_fails_even_when_bonjour_and_smb_are_skipped(self) -> None:
        run, calls = self._run_doctor_against_router(local_networks=self.WAN_SIDE, skip_bonjour=True, skip_smb=True)
        self.assertTrue(run.fatal)
        self.assertTrue(any(result.details.get("code") == DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK for result in run.results))

    def test_doctor_on_the_router_lan_runs_its_network_checks(self) -> None:
        run, calls = self._run_doctor_against_router(local_networks=self.ROUTER_LAN, skip_smb=True)
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK for result in run.results))
        calls.discover.assert_called()

    def test_doctor_uses_the_link_plan_of_the_last_mdns_attempt(self) -> None:
        transient = ReadinessProbeResult(
            ready=False,
            detail="discovery process is not running",
            steps=(ProbeStepResult("mdns_process", "fail", "discovery process is not running"),),
            link_plan=_parse_link_plan(router_plan(status="incomplete")),
        )
        ready = ReadinessProbeResult(
            ready=True,
            detail="managed mDNS registrant active",
            steps=(ProbeStepResult("mdns_process", "pass", "discovery process is running"),),
            link_plan=_parse_link_plan(router_plan()),
        )
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep"):
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                local_networks=self.WAN_SIDE,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mock.Mock(side_effect=[transient, ready])},
            )
        self.assertTrue(any(result.details.get("code") == DOCTOR_CODE_CLIENT_ON_UNSHARED_NETWORK for result in run.results))

    def test_bonjour_miss_without_ssh_compares_the_device_address(self) -> None:
        debug_fields: dict[str, object] = {}

        run = self._run_doctor_bonjour_miss(
            local_networks=self.OFF_NETWORK, debug_fields=debug_fields, skip_ssh=True, ssh_login=None,
        )

        skip = next(result for result in run.results if result.message.startswith("Bonjour"))
        self.assertEqual(skip.status, "SKIP")
        self.assertIn("is not on the device's network (10.0.0.2)", skip.message)
        self.assertEqual(debug_fields["bonjour_link"]["source"], "device_address")
        self.assertEqual(debug_fields["bonjour_link"]["device_networks"], ["10.0.0.2/32"])
        self.assertEqual(debug_fields["bonjour_link"]["detail"], "SSH checks were not run")
        run.mocks.probe_device_networks_conn.assert_not_called()

    def test_run_doctor_checks_uses_selected_native_records_without_fallback(self):
        run, _debug, browse, _diagnostics = self.run_selected_bonjour(self.selected_snapshot())
        self.assertFalse(run.fatal)
        browse.assert_called_once()
        messages = [r.message for r in run.results]
        self.assertIn("Bonjour IPv4: discovered _smb._tcp instance 'Home'", messages)
        self.assertIn("Bonjour IPv4: resolved _smb._tcp instance 'Home' to home.local:445", messages)
        self.assertIn("Bonjour IPv4: resolved Bonjour host home.local to 10.0.0.2 from service record", messages)
        self.assertFalse(any("fallback" in m for m in messages))

    def test_run_doctor_checks_uses_neutral_targeted_resolve_when_browse_misses_expected_smb(self):
        snapshot = self.selected_snapshot()
        smb = snapshot.resolved.pop(0)
        snapshot.instances = [i for i in snapshot.instances if i.service_type != smb.service_type]
        resolver = mock.Mock(return_value=(smb, None))
        run, _debug, browse, _diagnostics = self.run_selected_bonjour(snapshot, resolver=resolver)
        self.assertFalse(run.fatal)
        browse.assert_called_once()
        self.assertEqual({c.kwargs["family"] for c in resolver.call_args_list}, {"ipv4", "ipv6"})
        self.assertTrue(any("targeted resolve succeeded" in r.message for r in run.results))

    def test_run_doctor_checks_uses_selected_records_for_ip_only_bonjour(self):
        run, _debug, browse, _diagnostics = self.run_selected_bonjour(
            self.selected_snapshot(), extra={"timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn": mock.Mock(return_value=None)})
        self.assertFalse(run.fatal)
        self.assertTrue(any("matching target IP 10.0.0.2" in r.message for r in run.results))
        browse.assert_called_once()

    def test_run_doctor_checks_one_selected_provider_supplies_both_families(self):
        for provider in ("dns-sd", "zeroconf"):
            with self.subTest(provider=provider):
                run, _debug, browse, _diagnostics = self.run_selected_bonjour(self.selected_snapshot(), provider=provider)
                self.assertFalse(run.fatal)
                browse.assert_called_once()
                addresses = {r.message for r in run.results if "resolved Bonjour host" in r.message}
                self.assertTrue(any("10.0.0.2" in m for m in addresses))
                self.assertTrue(any("fd00::2" in m for m in addresses))

    def test_run_doctor_checks_keeps_wrong_ip_failure_under_either_provider(self):
        for provider in ("dns-sd", "zeroconf"):
            with self.subTest(provider=provider):
                run, debug, browse, diagnostics = self.run_selected_bonjour(self.selected_snapshot(ipv4=("10.0.0.99",)), provider=provider)
                self.assertTrue(run.fatal)
                self.assertTrue(any(r.status == "FAIL" and "10.0.0.99" in r.message and "expected 10.0.0.2" in r.message for r in run.results))
                self.assertIs(debug["bonjour_discovery"], diagnostics)
                browse.assert_called_once()

    def test_run_doctor_checks_uses_ip_only_bonjour_fallback_when_runtime_name_probe_fails(self) -> None:
        run = self.run_doctor_with_mocks(
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn": mock.Mock(side_effect=RuntimeError("probe failed")),
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "WARN" and "runtime naming identity probe skipped: probe failed" in result.message for result in run.results))
        self.assertTrue(any(result.status == "PASS" and "discovered _smb._tcp service matching target IP 10.0.0.2" in result.message for result in run.results))

    def test_run_doctor_checks_accepts_bonjour_record_with_link_local_ip(self) -> None:
        instance = BonjourServiceInstance(
            service_type="_smb._tcp.local.",
            name="Time Capsule Samba 4",
            fullname="Time Capsule Samba 4._smb._tcp.local.",
        )
        record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            service_type="_smb._tcp.local.",
            port=445,
            ipv4=["10.0.0.2", "169.254.44.9"],
        )
        run = self.run_doctor_with_mocks(
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([instance], [record]), None, None)
                ),
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(
            any(
                result.status == "PASS"
                and "resolved Bonjour host timecapsulesamba4.local to 10.0.0.2" in result.message
                for result in run.results
            )
        )
        self.assertFalse(
            any(
                "also advertised link-local IPv4" in result.message or "stale mDNS cache" in result.message
                for result in run.results
            )
        )

    def test_run_doctor_checks_skips_identity_bonjour_without_probe_or_literal_ip(self) -> None:
        run = self.run_doctor_with_mocks(
            self.valid_doctor_values(TC_HOST="root@capsule.local"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn": mock.Mock(side_effect=RuntimeError("probe failed")),
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(
            any(
                result.status == "SKIP"
                and "Bonjour identity check skipped; device naming probe unavailable and TC_HOST is not a literal IP" in result.message
                for result in run.results
            )
        )

    def test_run_doctor_checks_keeps_query_failure_with_incomplete_diagnostics(self):
        failure = CheckResult("FAIL", "Bonjour check failed: query failed")
        run, debug, browse, diagnostics = self.run_selected_bonjour(BonjourDiscoverySnapshot([], []), discovery_error=failure)
        self.assertTrue(run.fatal)
        self.assertTrue(any("query failed" in r.message for r in run.results))
        self.assertIs(debug["bonjour_discovery"], diagnostics)
        browse.assert_called_once()

    def test_run_doctor_checks_marks_missing_env_as_fatal(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_ssh_login"):
                    with mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_port"):
                        with mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing"):
                                with mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed", return_value=[]):
                                    with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                        results, fatal = run_doctor_checks(self.doctor_config(values, exists=False), repo_root=REPO_ROOT)
        self.assertTrue(fatal)
        self.assertEqual(results[0].status, "FAIL")
        self.assertIn("missing required configuration file", results[0].message)

    def test_check_required_local_tools_reports_each_tool(self) -> None:
        def fake_exists(name: str) -> bool:
            return name == "ssh"

        with mock.patch("timecapsulesmb.checks.local_tools.command_exists", side_effect=fake_exists), \
             mock.patch("timecapsulesmb.checks.local_tools.require_local_ssh", return_value="/tested/ssh"):
            results = check_required_local_tools()
        self.assertEqual([r.status for r in results], ["PASS", "FAIL", "PASS"])
        self.assertEqual(
            [r.message for r in results],
            [
                "local tool ssh is installed",
                "missing local tool smbclient, please install smbclient on your computer",
                "local tool ssh-askpass, the SSH password helper, is installed",
            ],
        )

    def test_check_required_local_tools_fails_missing_ssh(self) -> None:
        # Every device command runs the system ssh.
        with mock.patch("timecapsulesmb.checks.local_tools.command_exists", side_effect=lambda name: name != "ssh"):
            results = check_required_local_tools()
        self.assertEqual(
            results[0], CheckResult("FAIL", "missing local tool ssh, please install ssh on your computer"),
        )
        self.assertEqual([r.status for r in results[1:]], ["PASS", "PASS"])

    def test_discover_smb_services_detailed_returns_snapshot_and_diagnostics(self) -> None:
        snapshot = BonjourDiscoverySnapshot(
            instances=[BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")],
            resolved=[BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", fullname="Home._smb._tcp.local.")],
        )
        diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.0,
            ip_version="V4Only",
            instance_count=1,
            resolved_count=1,
            pending_count=0,
            service_added_count=1,
            service_updated_count=0,
            resolve_attempt_count=1,
            resolve_success_count=1,
            resolve_error_count=0,
            instances=list(snapshot.instances),
            resolved=list(snapshot.resolved),
        )

        with mock.patch("timecapsulesmb.checks.bonjour.discover_snapshot_detailed", return_value=(snapshot, diagnostics)) as discover_mock:
            result, error, result_diagnostics = discover_smb_services_detailed(timeout=3.5)

        discover_mock.assert_called_once_with("_smb", timeout=3.5, target_ip=None, family=None, interfaces=None)
        self.assertIs(result, snapshot)
        self.assertIsNone(error)
        self.assertIs(result_diagnostics, diagnostics)

    def test_discover_smb_services_detailed_can_include_related_bonjour_services(self) -> None:
        snapshot = BonjourDiscoverySnapshot([], [])
        diagnostics = BonjourDiscoveryDiagnostics(
            service=None,
            service_types=["_airport._tcp.local.", "_smb._tcp.local.", "_adisk._tcp.local.", "_device-info._tcp.local."],
            timeout_sec=6.0,
            elapsed_sec=6.0,
            ip_version="V4Only",
            instance_count=0,
            resolved_count=0,
            pending_count=0,
            service_added_count=0,
            service_updated_count=0,
            resolve_attempt_count=0,
            resolve_success_count=0,
            resolve_error_count=0,
        )

        with mock.patch("timecapsulesmb.checks.bonjour.discover_snapshot_detailed", return_value=(snapshot, diagnostics)) as discover_mock:
            result, error, result_diagnostics = discover_smb_services_detailed(timeout=3.5, include_related=True)

        # Every related service but _afpovertcp, whose only extra finds are cached
        # copies of an AFP service the device no longer advertises.
        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip=None, family=None, interfaces=None,
                                              service_types=["_airport._tcp.local.", "_smb._tcp.local.",
                                                             "_adisk._tcp.local.", "_device-info._tcp.local."])
        self.assertIs(result, snapshot)
        self.assertIsNone(error)
        self.assertIs(result_diagnostics, diagnostics)

    def test_discover_smb_services_detailed_passes_target_ip_to_discovery_backend(self) -> None:
        snapshot = BonjourDiscoverySnapshot([], [])
        diagnostics = BonjourDiscoveryDiagnostics(
            service=None,
            service_types=["_smb._tcp.local."],
            timeout_sec=3.5,
            elapsed_sec=3.5,
            ip_version="V4Only",
            instance_count=0,
            resolved_count=0,
            pending_count=0,
            service_added_count=0,
            service_updated_count=0,
            resolve_attempt_count=0,
            resolve_success_count=0,
            resolve_error_count=0,
        )

        with mock.patch("timecapsulesmb.checks.bonjour.discover_snapshot_detailed", return_value=(snapshot, diagnostics)) as discover_mock:
            result, error, result_diagnostics = discover_smb_services_detailed(timeout=3.5, include_related=True, target_ip="10.0.1.77")

        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip="10.0.1.77", family=None, interfaces=None,
                                              service_types=DOCTOR_SERVICE_TYPES)
        self.assertIs(result, snapshot)
        self.assertIsNone(error)
        self.assertIs(result_diagnostics, diagnostics)

    def test_discover_smb_services_detailed_returns_fail_when_discovery_backend_errors(self) -> None:
        with mock.patch("timecapsulesmb.checks.bonjour.discover_snapshot_detailed", side_effect=RuntimeError("zeroconf missing")):
            snapshot, error, diagnostics = discover_smb_services_detailed()
        self.assertIsNone(snapshot)
        self.assertIsNone(diagnostics)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, "FAIL")
        self.assertIn("zeroconf missing", error.message)

    def test_bonjour_checks_discover_expected_instance_and_target(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Time Capsule Samba 4", "Time Capsule Samba 4._smb._tcp.local.")
        record = BonjourResolvedService("Time Capsule Samba 4", "timecapsulesamba4.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"])
        selection = select_smb_instance([instance], expected_instance_name="Time Capsule Samba 4")
        self.assertIsNotNone(selection.instance)
        target = resolve_smb_service_target(record, expected_instance_name="Time Capsule Samba 4")
        self.assertEqual([result.status for result in check_smb_instance(selection)], ["PASS"])
        self.assertEqual(check_smb_service_target(target).status, "PASS")
        self.assertEqual(target.hostname, "timecapsulesamba4.local")

    def test_select_smb_instance_returns_configured_instance_name(self) -> None:
        other = BonjourServiceInstance("_smb._tcp.local.", "Kitchen", "Kitchen._smb._tcp.local.")
        ours = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")

        selection = select_smb_instance([other, ours], expected_instance_name="Home")
        self.assertIs(selection.instance, ours)

    def test_build_expected_smb_instance_constructs_fullname(self) -> None:
        instance = build_expected_smb_instance("Home")

        self.assertEqual(instance.service_type, "_smb._tcp.local.")
        self.assertEqual(instance.name, "Home")
        self.assertEqual(instance.fullname, "Home._smb._tcp.local.")

    def test_resolve_expected_smb_record_uses_browsed_record_before_targeted_query(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", fullname="Home._smb._tcp.local.")
        resolver = mock.Mock()

        result = resolve_expected_smb_record(
            [instance],
            [record],
            expected_instance_name="Home",
            target_ip="10.0.1.77",
            family="ipv4",
            interfaces=["10.0.1.42"],
            resolver=resolver,
        )

        self.assertEqual(result.source, "browse")
        self.assertIs(result.instance, instance)
        self.assertIs(result.record, record)
        self.assertIsNone(result.error)
        resolver.assert_not_called()

    def test_resolve_expected_smb_record_targets_expected_instance_when_browse_misses(self) -> None:
        other = BonjourServiceInstance("_smb._tcp.local.", "Kitchen", "Kitchen._smb._tcp.local.")
        record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.1.77"])
        resolver = mock.Mock(return_value=(record, None))

        result = resolve_expected_smb_record(
            [other],
            [],
            expected_instance_name="Home",
            target_ip="10.0.1.77",
            family="ipv4",
            interfaces=["10.0.1.42"],
            resolver=resolver,
        )

        self.assertEqual(result.source, "targeted_resolve")
        self.assertEqual(result.instance, build_expected_smb_instance("Home"))
        self.assertIs(result.record, record)
        self.assertIsNone(result.error)
        resolver.assert_called_once()
        resolved_instance = resolver.call_args.args[0]
        self.assertEqual(resolved_instance.fullname, "Home._smb._tcp.local.")
        self.assertEqual(resolver.call_args.kwargs["target_ip"], "10.0.1.77")
        self.assertEqual(resolver.call_args.kwargs["family"], "ipv4")
        self.assertEqual(resolver.call_args.kwargs["interfaces"], ["10.0.1.42"])
        self.assertIn("targeted query", resolver.call_args.kwargs["missing_message"])

    def test_select_smb_instance_fails_when_no_record_matches_expected_instance(self) -> None:
        other = BonjourServiceInstance("_smb._tcp.local.", "Kitchen", "Kitchen._smb._tcp.local.")

        selection = select_smb_instance([other], expected_instance_name="Home")
        results = check_smb_instance(selection)
        self.assertEqual([result.status for result in results], ["FAIL", "INFO"])
        self.assertIn("no discovered _smb._tcp instance matched expected device instance 'Home'", results[0].message)
        self.assertIn("'Kitchen'", results[1].message)

    def test_select_resolved_smb_record_prefers_matching_fullname(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        wrong = BonjourResolvedService("Home", "wrong.local", "_smb._tcp.local.", fullname="Home (2)._smb._tcp.local.")
        ours = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", fullname="Home._smb._tcp.local.")

        self.assertIs(select_resolved_smb_record([wrong, ours], instance), ours)

    def test_select_resolved_smb_record_falls_back_to_name_when_fullname_missing(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.")

        self.assertIs(select_resolved_smb_record([record], instance), record)

    def test_resolve_smb_instance_returns_fail_when_service_resolution_fails(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        with mock.patch("timecapsulesmb.checks.bonjour.resolve_service_instance", return_value=None) as resolve_mock:
            record, error = resolve_smb_instance(instance)
        resolve_mock.assert_called_once_with(instance, timeout_ms=3000, target_ip=None, family=None, interfaces=None)
        self.assertIsNone(record)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, "FAIL")
        self.assertIn("could not resolve service target", error.message)

    def test_resolve_smb_instance_passes_target_ip_to_discovery_backend(self) -> None:
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        resolved = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.1.77"])
        with mock.patch("timecapsulesmb.checks.bonjour.resolve_service_instance", return_value=resolved) as resolve_mock:
            record, error = resolve_smb_instance(instance, target_ip="10.0.1.77")

        resolve_mock.assert_called_once_with(instance, timeout_ms=3000, target_ip="10.0.1.77", family=None, interfaces=None)
        self.assertIs(record, resolved)
        self.assertIsNone(error)

    def test_resolve_smb_service_target_uses_resolved_hostname_and_port(self) -> None:
        record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445)
        target = resolve_smb_service_target(record, expected_instance_name="Home")
        self.assertEqual(target.hostname, "home.local")
        self.assertEqual(target.host_label(), "home")
        self.assertEqual(check_smb_service_target(target).status, "PASS")

    def test_resolve_smb_service_target_fails_without_resolved_hostname(self) -> None:
        record = BonjourResolvedService("Home", "", "_smb._tcp.local.", port=445)
        target = resolve_smb_service_target(record, expected_instance_name="Home")
        result = check_smb_service_target(target)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("could not resolve service target", result.message)

    def test_check_bonjour_host_ip_passes_with_dns_resolved_expected_ip(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.1.1", 0))]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            result = check_bonjour_host_ip("home.local", expected_ip="10.0.1.1")
        self.assertEqual(result.status, "PASS")
        self.assertIn("10.0.1.1", result.message)

    def test_check_bonjour_host_ip_passes_with_service_record_ip_when_dns_fails(self) -> None:
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=OSError("no dns")):
            result = check_bonjour_host_ip("home.local", expected_ip="10.0.1.1", record_ips=["10.0.1.1"])
        self.assertEqual(result.status, "PASS")
        self.assertIn("from service record", result.message)

    def test_check_bonjour_host_ip_matches_numeric_and_named_ipv6_scopes(self) -> None:
        with (
            mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=()),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", return_value=17),
        ):
            result = check_bonjour_host_ip(
                "home.local",
                expected_ip="fe80::2%en0",
                record_ips=["fe80::2%17"],
            )

        self.assertEqual(result.status, "PASS")
        self.assertIn("from service record", result.message)

    def test_bonjour_dns_cannot_erase_a_concrete_ipv6_scope_mismatch(self) -> None:
        def resolve(_host, _port, family, _kind):
            return [] if family == socket.AF_INET else [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::2", 0, 0, 18))]

        with (
            mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", side_effect=resolve),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", return_value=17),
            mock.patch("timecapsulesmb.core.net.socket.if_indextoname", side_effect=OSError("use numeric zone")),
        ):
            result = check_bonjour_host_ip("home.local", expected_ip="fe80::2%en0", record_ips=["fe80::2%18"])
        self.assertEqual(result.status, "FAIL")
        self.assertIn("fe80::2%18", result.message)

        with mock.patch("timecapsulesmb.checks.bonjour.resolve_host_ips", return_value=("fe80::2",)):
            result = check_bonjour_host_ip("home.local", expected_ip="fe80::2%17", record_ips=["fe80::2%18"])
        self.assertEqual(result.status, "FAIL")

    def test_check_bonjour_host_ip_fails_when_dns_resolves_wrong_ip(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.1.99", 0))]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            result = check_bonjour_host_ip("home.local", expected_ip="10.0.1.1")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("expected 10.0.1.1", result.message)

    def test_check_authenticated_smb_listing_handles_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbclient"], timeout=20),
            ):
                result = check_authenticated_smb_listing("admin", "pw", "home.local")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("attempt 1 home.local", result.message)
        self.assertIn("timed out", result.message)
        self.assertEqual(result.details["attempts"][0]["outcome"], "timeout")

    def test_run_doctor_checks_respects_skip_flags(self) -> None:
        run = self.run_doctor_with_mocks(
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            skip_ssh=True,
            skip_bonjour=True,
            skip_smb=True,
        )
        run.mocks.check_smb_port.assert_called_once()
        run.mocks.flash_runtime_config_present_conn.assert_not_called()
        run.mocks.read_deployed_version_conn.assert_not_called()
        self.assertFalse(run.fatal)
        self.assertEqual(run.results[0].status, "PASS")
        self.assertIn("configuration file exists", run.results[0].message)

    def test_check_required_local_tools_fails_an_unusable_ssh_password_helper(self) -> None:
        # ssh runs the helper for the device password; a copy that lost its
        # executable bit, or is gone, means a damaged install.
        with tempfile.TemporaryDirectory() as tmp:
            not_executable = Path(tmp) / "ssh-askpass"
            not_executable.write_text("#!/bin/sh\n")
            not_executable.chmod(0o644)
            for helper in (not_executable, Path(tmp) / "missing"):
                with self.subTest(helper=helper.name):
                    with mock.patch("timecapsulesmb.checks.local_tools.command_exists", return_value=True), \
                         mock.patch("timecapsulesmb.checks.local_tools.SSH_ASKPASS_PATH", helper), \
                         mock.patch("timecapsulesmb.checks.local_tools.require_local_ssh", return_value="/tested/ssh"):
                        results = check_required_local_tools()
                    self.assertEqual([r.status for r in results], ["PASS", "PASS", "FAIL"])
                    self.assertIn(f"is missing or not executable at {helper}; reinstall TimeCapsuleSMB", results[2].message)

    def test_check_required_local_tools_checks_the_openssl_config_only_where_openssl_refuses_sha1(self) -> None:
        # ssh needs openssl-sha1.cnf only where OpenSSL refuses the Time
        # Capsule's SHA-1 signature; elsewhere it is not reported at all.
        override = {"OPENSSL_CONF": "/pkg/openssl-sha1.cnf", "TCAPSULE_OPENSSL_BASE_CONF": "/etc/pki/tls/openssl.cnf"}
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "openssl-sha1.cnf"
            present.write_text(".include $ENV::TCAPSULE_OPENSSL_BASE_CONF\n")
            cases = (
                ("not needed", {}, present, None),
                ("installed", override, present, "PASS"),
                ("missing", override, Path(tmp) / "gone.cnf", "FAIL"),
            )
            for name, env, config, status in cases:
                with self.subTest(name):
                    with mock.patch("timecapsulesmb.checks.local_tools.command_exists", return_value=True), \
                         mock.patch("timecapsulesmb.checks.local_tools.require_local_ssh", return_value="/tested/ssh"), \
                         mock.patch("timecapsulesmb.checks.local_tools.sha1_signature_env", return_value=env), \
                         mock.patch("timecapsulesmb.checks.local_tools.SHA1_OPENSSL_CONF_PATH", config):
                        results = check_required_local_tools()
                    openssl = [r for r in results if "OpenSSL config" in r.message]
                    if status is None:
                        self.assertEqual(openssl, [])
                        continue
                    self.assertEqual([r.status for r in openssl], [status])
                    if status == "FAIL":
                        self.assertIn(f"is missing at {config}; reinstall TimeCapsuleSMB", openssl[0].message)
                        self.assertTrue(any(r.status == "FAIL" for r in results))

    def test_run_doctor_checks_reports_local_tools_after_a_valid_config(self) -> None:
        tools = [CheckResult("FAIL", "missing local tool smbclient, please install smbclient on your computer")]
        run = self.run_doctor_with_mocks(
            local_tools=tools,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            mdns_probe=mock.Mock(ready=True, detail="ok"),
            read_active_smb_conf="",
            xattr_result=mock.Mock(status="WARN", message="xattr skipped"),
            smb_port=mock.Mock(status="SKIP", message="port skipped"),
            skip_bonjour=True,
            skip_smb=True,
        )

        self.assertTrue(run.fatal)
        run.mocks.check_required_local_tools.assert_called_once_with()
        self.assertEqual([r for r in run.results if "local tool" in r.message], tools)
        self.assertIn("contains all required settings", run.results[1].message)
        self.assertIs(run.results[2], tools[0])

    def _doctor_without_usable_config(self, config: AppConfig, tool_present):
        ssh_login = mock.Mock()
        with mock.patch("timecapsulesmb.checks.local_tools.command_exists", side_effect=tool_present), \
             mock.patch("timecapsulesmb.checks.doctor_steps.check_nsmb_conf", return_value=([], None)):
            with mock.patch("timecapsulesmb.checks.doctor_steps.check_ssh_login", ssh_login):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts") as artifacts:
                    results, fatal = run_doctor_checks(config, repo_root=REPO_ROOT)
        ssh_login.assert_not_called()
        artifacts.assert_not_called()
        return results, fatal

    def test_run_doctor_checks_missing_env_suggests_configure_and_reports_missing_tools(self) -> None:
        results, fatal = self._doctor_without_usable_config(
            self.doctor_config(self.valid_doctor_values(), exists=False),
            lambda name: name == "ssh",
        )

        self.assertTrue(fatal)
        self.assertEqual(
            [(r.status, r.message) for r in results],
            [
                ("FAIL", f"missing required configuration file: {REPO_ROOT / '.env'}; run the `configure` command before running `doctor`"),
                ("PASS", "local tool ssh is installed"),
                ("FAIL", "missing local tool smbclient, please install smbclient on your computer"),
                ("PASS", "local tool ssh-askpass, the SSH password helper, is installed"),
            ],
        )

    def test_run_doctor_checks_missing_env_reports_present_tools(self) -> None:
        results, fatal = self._doctor_without_usable_config(
            self.doctor_config(self.valid_doctor_values(), exists=False),
            lambda name: True,
        )

        self.assertTrue(fatal)
        self.assertEqual([r.status for r in results], ["FAIL", "PASS", "PASS", "PASS"])
        self.assertEqual(results[1].message, "local tool ssh is installed")

    def test_run_doctor_checks_invalid_env_reports_errors_then_tools(self) -> None:
        values = self.valid_doctor_values()
        del values["TC_HOST"]
        results, fatal = self._doctor_without_usable_config(self.doctor_config(values), lambda name: name != "smbclient")

        self.assertTrue(fatal)
        self.assertEqual(results[0].message, f"configuration file exists: {REPO_ROOT / '.env'}")
        tool_results = [r for r in results if "local tool" in r.message]
        self.assertEqual([r.status for r in tool_results], ["PASS", "FAIL", "PASS"])
        self.assertEqual(tool_results[1].message, "missing local tool smbclient, please install smbclient on your computer")
        config_errors = results[1:results.index(tool_results[0])]
        self.assertTrue(config_errors)
        self.assertTrue(all(r.status == "FAIL" for r in config_errors))

    def run_doctor_with_password_answer(self, matches: bool | None, *, device_reading=None):
        debug_fields: dict[str, object] = {}
        compare = acp_password_answer(matches)
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
            run = self.run_doctor_with_mocks(
                device_reading=device_reading,
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                mdns_probe=mock.Mock(ready=True, detail="ok"),
                read_active_smb_conf="",
                xattr_result=mock.Mock(status="WARN", message="xattr skipped"),
                smb_port=mock.Mock(status="SKIP", message="port skipped"),
                skip_bonjour=True,
                skip_smb=True,
                debug_fields=debug_fields,
            )
        lines = [result for result in run.results if "AirPort admin password" in result.message]
        run.compare = compare
        return run, lines, debug_fields

    def test_doctor_uses_the_apps_earlier_read_instead_of_reading_again(self) -> None:
        expected = {True: "PASS", False: "WARN", None: "INFO"}
        for matches, status in expected.items():
            with self.subTest(matches=matches):
                # The device would answer the opposite way if it were asked.
                run, lines, debug_fields = self.run_doctor_with_password_answer(
                    not matches, device_reading=acp_reading(matches),
                )
                run.compare.assert_not_called()
                self.assertEqual([line.status for line in lines], [status])
                self.assertEqual(debug_fields["sypw_check"], {True: "match", False: "mismatch", None: "unknown"}[matches])

    def test_doctor_passes_a_saved_password_that_matches_the_device(self) -> None:
        _run, lines, debug_fields = self.run_doctor_with_password_answer(True)
        self.assertEqual([(line.status, line.message) for line in lines],
                         [("PASS", "the saved password is the AirPort admin password")])
        self.assertEqual(debug_fields["sypw_check"], "match")

    def test_doctor_warns_about_a_saved_password_acp_would_reject_and_keeps_checking(self) -> None:
        # A password right in its first 8 characters logs in over SSH, so file
        # sharing still works: a warning, and the other checks still run.
        run, lines, debug_fields = self.run_doctor_with_password_answer(False)
        self.assertEqual([line.status for line in lines], ["WARN"])
        self.assertIn("tcapsule configure", lines[0].message)
        self.assertEqual(debug_fields["sypw_check"], "mismatch")
        self.assertTrue(any(result.message == "xattr skipped" for result in run.results))

    def test_doctor_reports_a_password_it_cannot_compare_as_info(self) -> None:
        _run, lines, debug_fields = self.run_doctor_with_password_answer(None)
        self.assertEqual([line.status for line in lines], ["INFO"])
        self.assertEqual(debug_fields["sypw_check"], "unknown")

    def test_doctor_has_no_password_line_for_key_authentication(self) -> None:
        sink = DoctorSink(on_result=None, debug_fields={})
        compare = mock.Mock()
        with mock.patch("timecapsulesmb.device.probe.read_airport_acp", compare):
            _add_admin_password_result(SshConnection("root@10.0.0.2", "", "-o foo"), sink)
        self.assertEqual(sink.results, [])
        compare.assert_not_called()

    def test_run_doctor_checks_ignores_legacy_name_env_values(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "bad host label",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[]):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor_steps.check_smb_port",
                        return_value=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                    ):
                        results, fatal = run_doctor_checks(
                            self.doctor_config(values),
                            repo_root=REPO_ROOT,
                            skip_ssh=True,
                            skip_bonjour=True,
                            skip_smb=True,
                        )
        self.assertFalse(fatal)
        self.assertFalse(any("TC_MDNS_HOST_LABEL is invalid" in result.message for result in results))

    def test_run_doctor_checks_does_not_require_saved_airport_syap(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig.from_values(
                values,
                path=Path(tmp) / ".env",
                exists=True,
                file_values=values,
            )
            with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[]):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor_steps.check_smb_port",
                        return_value=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                    ):
                        results, fatal = run_doctor_checks(
                            config,
                            repo_root=REPO_ROOT,
                            skip_ssh=True,
                            skip_bonjour=True,
                            skip_smb=True,
                        )
        self.assertFalse(fatal)
        self.assertFalse(any(
            "Missing required setting" in result.message and "TC_AIRPORT_SYAP" in result.message
            for result in results
        ))

    def test_run_doctor_checks_ignores_stale_net_iface(self) -> None:
        run = self.run_doctor_with_mocks(
            self.valid_doctor_values(TC_NET_IFACE="bridge9"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
        )
        self.assertFalse(run.fatal)
        self.assertFalse(any("TC_NET_IFACE is invalid" in result.message for result in run.results))

    def test_run_doctor_checks_uses_precomputed_connection(self) -> None:
        connection = SshConnection("root@10.0.0.9", "pw", "-o injected")
        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            read_active_smb_conf="",
            xattr_result=CheckResult("WARN", "xattr skipped"),
            smb_port=CheckResult("PASS", "445 ok"),
            connection=connection,
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "PASS" and result.message == "ssh ok" for result in run.results))
        run.mocks.check_ssh_login.assert_called_once_with(connection)

    def test_run_doctor_checks_reports_managed_mdns_takeover_state(self) -> None:
        debug_fields: dict[str, object] = {}
        log_tail_mock = mock.Mock(return_value={
            "remote_rc_local_log_tail": "rc log",
            "remote_discovery_log_tail": "mdns log",
        })
        ram_diagnostics_mock = mock.Mock(return_value="df /mnt/Memory:\nruntime paths:\nmissing /mnt/Memory/samba4/sbin/smbd")
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": log_tail_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": ram_diagnostics_mock,
            },
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any("managed mDNS registrant is not active" in result.message for result in run.results))
        self.assertEqual(debug_fields["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(debug_fields["remote_discovery_log_tail"], "mdns log")
        self.assertEqual(debug_fields["remote_runtime_ram_diagnostics"], ram_diagnostics_mock.return_value)
        log_tail_mock.assert_called_once()
        ram_diagnostics_mock.assert_called_once()

    @staticmethod
    def data_disk_log_tails(**overrides: object) -> dict[str, object]:
        timeout_text = (
            "(unavailable: Timed out waiting for ssh command to finish: "
            "/bin/sh -c 'tail -n 80 /Volumes/dk2/.samba4/logs/log.smbd')"
        )
        logs: dict[str, object] = {
            "remote_rc_local_log_tail": "rc log",
            "remote_manager_log_tail": "manager log",
            "remote_payload_log_dir": "/Volumes/dk2/.samba4",
            "remote_smbd_log_tail": timeout_text,
            "remote_discovery_log_tail": timeout_text,
        }
        logs.update(overrides)
        return logs

    def test_data_disk_unresponsive_result_flags_payload_timeouts_when_ramdisk_reads_succeed(self) -> None:
        result = _data_disk_unresponsive_result(self.data_disk_log_tails())

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("data disk appears unresponsive", result.message)
        self.assertIn("discovery.log, log.smbd", result.message)
        self.assertIn("/Volumes/dk2/.samba4/logs", result.message)
        self.assertIn("ramdisk reads succeeded", result.message)
        self.assertEqual(result.details["data_disk_timed_out_logs"], ["discovery.log", "log.smbd"])

    def test_data_disk_unresponsive_result_flags_single_payload_log_timeout(self) -> None:
        result = _data_disk_unresponsive_result(
            self.data_disk_log_tails(
                remote_discovery_log_tail="discovery log",
            )
        )

        self.assertIsNotNone(result)
        self.assertIn("log.smbd", result.message)
        self.assertNotIn("mdns.log", result.message)
        self.assertEqual(result.details["data_disk_timed_out_logs"], ["log.smbd"])

    def test_data_disk_unresponsive_result_skips_when_ramdisk_reads_also_time_out(self) -> None:
        timeout_text = "(unavailable: Timed out waiting for ssh command to finish: tail)"
        result = _data_disk_unresponsive_result(
            self.data_disk_log_tails(
                remote_rc_local_log_tail=timeout_text,
                remote_manager_log_tail=timeout_text,
            )
        )

        self.assertIsNone(result)

    def test_data_disk_unresponsive_result_skips_without_payload_log_dir(self) -> None:
        # Without a payload dir the mdns/nbns tails come from ramdisk fallback
        # paths, so timeouts there say nothing about the data disk.
        result = _data_disk_unresponsive_result(
            self.data_disk_log_tails(remote_payload_log_dir="(unavailable from active smb.conf)")
        )

        self.assertIsNone(result)

    def test_data_disk_unresponsive_result_skips_when_payload_reads_fail_without_timeout(self) -> None:
        error_text = "(unavailable: SshError: ssh command failed with rc=255)"
        result = _data_disk_unresponsive_result(
            self.data_disk_log_tails(
                remote_smbd_log_tail=error_text,
                remote_discovery_log_tail=error_text,
            )
        )

        self.assertIsNone(result)

    def test_data_disk_unresponsive_result_skips_when_payload_reads_succeed(self) -> None:
        result = _data_disk_unresponsive_result(
            self.data_disk_log_tails(
                remote_smbd_log_tail="smbd log",
                remote_discovery_log_tail="discovery log",
            )
        )

        self.assertIsNone(result)

    def test_run_doctor_checks_promotes_data_disk_timeouts_to_fail_result(self) -> None:
        debug_fields: dict[str, object] = {}
        log_tail_mock = mock.Mock(return_value=self.data_disk_log_tails())
        run = self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[CheckResult("FAIL", "SMB file create failed: NT_STATUS_UNSUCCESSFUL opening remote file")],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": log_tail_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        self.assertTrue(run.fatal)
        disk_results = [result for result in run.results if "data disk appears unresponsive" in result.message]
        self.assertEqual(len(disk_results), 1)
        self.assertEqual(disk_results[0].status, "FAIL")
        self.assertEqual(debug_fields["remote_payload_log_dir"], "/Volumes/dk2/.samba4")
        log_tail_mock.assert_called_once()

    def test_run_doctor_checks_does_not_add_data_disk_fail_when_log_tails_read_fine(self) -> None:
        debug_fields: dict[str, object] = {}
        log_tail_mock = mock.Mock(
            return_value=self.data_disk_log_tails(
                remote_smbd_log_tail="smbd log",
                remote_discovery_log_tail="discovery log",
            )
        )
        run = self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[CheckResult("FAIL", "SMB file create failed: NT_STATUS_UNSUCCESSFUL opening remote file")],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": log_tail_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        self.assertTrue(run.fatal)
        self.assertFalse(any("data disk appears unresponsive" in result.message for result in run.results))

    STUCK_SMBD_ROW = " 3166 457 457 D 127 biowait 0:00.00 smbd"
    HEALTHY_SMBD_ROW = "  457 146 457 I 20 select 0:00.00 smbd"

    def run_doctor_with_process_snapshot(
        self,
        snapshot: object,
        *,
        smb_listing: CheckResult | None = None,
        manager_started_seconds_ago: float = 400.0,
        ssh_login: object | None = None,
        file_locks: object = "",
        disk_file_locks: object = "",
        lock_waiters: object = "",
        kern_limits: object = "",
        process_table: tuple[str, str] = ("", ""),
        device_reading=None,
    ):
        debug_fields: dict[str, object] = {}
        snapshot_mock = mock.Mock(side_effect=snapshot) if isinstance(snapshot, Exception) else mock.Mock(return_value=snapshot)
        order: list[str] = []
        def read_locks(connection, patterns):
            order.append("disk locks" if patterns == FILE_LOCKS_DISK_TDBS else "locks")
            answer = disk_file_locks if patterns == FILE_LOCKS_DISK_TDBS else file_locks
            if isinstance(answer, Exception):
                raise answer
            return answer

        def read_waiters(connection, command):
            order.append("waiters" if "fstat" in command else "waiters without fstat")
            if isinstance(lock_waiters, Exception):
                raise lock_waiters
            return lock_waiters

        locks_mock = mock.Mock(side_effect=read_locks)
        waiters_mock = mock.Mock(side_effect=read_waiters)
        kern_mock = mock.Mock(side_effect=kern_limits) if isinstance(kern_limits, Exception) else mock.Mock(return_value=kern_limits)
        log_tail_mock = mock.Mock(return_value={})
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                # The device's own SMB check is what a short process table
                # must protect, so these cases run it.
                real_device_smb=True,
                ssh_login=ssh_login or mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                smb_instance=[],
                smb_listing=smb_listing or self.smb_listing_result(),
                smb_file_ops=[],
                smbd_probe=mock.Mock(ready=True, detail="managed smbd is ready"),
                mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
                device_reading=device_reading,
                debug_fields=debug_fields,
                extra_patches={
                    "timecapsulesmb.checks.doctor_steps.read_process_snapshot_conn": snapshot_mock,
                    "timecapsulesmb.checks.doctor_steps.read_uninterruptible_processes_conn": mock.Mock(
                        return_value=process_table
                    ),
                    "timecapsulesmb.checks.doctor_steps.read_file_locks_conn": locks_mock,
                    "timecapsulesmb.checks.doctor_steps.read_remote_command_conn": waiters_mock,
                    "timecapsulesmb.checks.doctor_steps.read_kern_limits_conn": kern_mock,
                    "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                        return_value=ManagerStartupAgeProbeResult(
                            manager_started_seconds_ago, f"manager started {int(manager_started_seconds_ago)}s ago"
                        )
                    ),
                    "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": log_tail_mock,
                    "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                },
            )
        run.debug_fields = debug_fields
        run.snapshot = snapshot_mock
        run.file_locks = locks_mock
        run.lock_waiters = waiters_mock
        run.lock_reads = order
        run.kern_limits = kern_mock
        run.log_tails = log_tail_mock
        run.sleep = sleep_mock
        return run

    def connection_shaped_smb_failure(self) -> CheckResult:
        return CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 home.local: NT_STATUS_IO_TIMEOUT",
            {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]},
        )

    def test_run_doctor_checks_reports_stuck_smbd_and_skips_authenticated_smb(self) -> None:
        run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, self.STUCK_SMBD_ROW]))

        self.assertTrue(run.fatal)
        # biowait is a wait for the disk's own I/O: the disk stopped responding.
        stuck = [result for result in run.results if result.details.get("code") == "disk_not_responding"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0].status, "FAIL")
        self.assertTrue(stuck[0].message.startswith("A disk on the device has stopped responding"))
        self.assertIn("smbd (pid 3166) waiting on biowait for 127+ s", stuck[0].message)
        self.assertIn("If the disk stops responding again, it is probably failing", stuck[0].message)
        self.assertEqual(
            stuck[0].details["stuck_processes"],
            [{"pid": 3166, "name": "smbd", "wchan": "biowait", "sleep_seconds": 127}],
        )
        # The parent smbd still serves, so doctor goes on past the disk, but a
        # Mac's login would wait behind the stuck child: the SMB check says
        # why it does not run.
        skipped = [result for result in run.results if result.status == "SKIP" and "authenticated SMB" in result.message]
        self.assertEqual(len(skipped), 1)
        run.mocks.check_authenticated_smb_listing.assert_not_called()
        run.mocks.probe_managed_smbd_conn.assert_called()
        self.assertIn(self.STUCK_SMBD_ROW.strip(), run.debug_fields["remote_process_snapshot"])
        # The snapshot answered, so the data-disk logs are still worth reading.
        run.log_tails.assert_called_once()
        self.assertIsNone(run.log_tails.call_args.kwargs["skip_data_disk"])

    LOCKF_SMBD_ROW = " 8893 457 457 I 127 lockf 0:00.00 smbd"

    def test_run_doctor_checks_lists_tdb_lock_holders_when_an_smbd_waits_in_lockf(self) -> None:
        locks = "/mnt/Locks/locking.tdb pid 457 read 4+1\n/mnt/Locks/locking.tdb pid 9001 write 1460+1\n"
        disk = "/Volumes/dk2/.samba4/private/xattr.tdb pid 9001 write 3100+1\n"
        run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW]),
                                                    file_locks=locks, disk_file_locks=disk)

        # RAM TDBs first, then the data disk's.
        self.assertEqual([c.args[1] for c in run.file_locks.call_args_list], [FILE_LOCKS_RAM_TDBS, FILE_LOCKS_DISK_TDBS])
        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())
        self.assertEqual(run.debug_fields["remote_file_locks_disk"], disk.rstrip())
        # An interruptible wait: not reported as blocked in the kernel, and SMB is still checked.
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))
        run.mocks.check_authenticated_smb_listing.assert_called()
        # Asleep 127 s, past the two minutes: one waiter, named as one.
        self.assertIn("for a file lock (PID 8893); ", "\n".join(result.message for result in run.results))

    def test_run_doctor_checks_lists_no_locks_without_an_smbd_in_lockf(self) -> None:
        # Another program waiting for a lock is not Samba's TDB lock.
        other = "  600 1 600 I 127 lockf 0:00.00 cron"
        run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, other]))

        run.file_locks.assert_not_called()
        run.lock_waiters.assert_not_called()
        self.assertNotIn("remote_file_locks", run.debug_fields)
        self.assertNotIn("remote_file_locks_error", run.debug_fields)
        self.assertNotIn("remote_lock_waiters", run.debug_fields)
        self.assertFalse(run.fatal)

    LOCK_WAITERS_OUTPUT = "\n".join([
        "8893 wchan=lockf spsr=60000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=0012abcd",
        "8894 wchan=lockf spsr=60000010 r0=00000007 r1=00000009 r2=7fffd000 r3=00000000 pc=00034560",
        "USER     CMD          PID   FD MOUNT       INUM MODE         SZ|DV R/W",
        "root     smbd        8893   wd /              2 drwxr-xr-x     512 r",
        "root     smbd        8893   12 /Volumes/dk2 48811 -rw-rw-rw-  921344 r",
        "USER     CMD          PID   FD MOUNT       INUM MODE         SZ|DV R/W",
        "root     smbd        8894    7 /mnt/Locks     5 -rw-r--r--   12288 rw",
        "root     smbd        8894    8 /mnt/Locks     6 -rw-r--r--   12288 rw",
    ]) + "\n"

    def test_run_doctor_checks_reads_each_long_lock_waiters_call_before_listing_tdb_locks(self) -> None:
        # Waiters asleep LOCK_WAIT_READ_SECONDS or more: a flock() on a data
        # file and a TDB record lock. One that has waited less is not read.
        ten = " 8894 457 457 I 10 lockf 0:00.00 smbd"
        nine = " 8895 457 457 I 9 lockf 0:00.00 smbd"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW, ten, nine]), lock_waiters=self.LOCK_WAITERS_OUTPUT,
        )

        run.lock_waiters.assert_called_once()
        self.assertEqual(run.lock_waiters.call_args.args[1], lock_waiters_command([8893, 8894]))
        # Kernel structures first: they never wait on a disk, the disk's TDB listing can.
        self.assertEqual(run.lock_reads, ["waiters", "locks", "disk locks"])
        # The registers, and fstat's rows for the two descriptors they name.
        self.assertEqual(run.debug_fields["remote_lock_waiters"], "\n".join([
            "8893 wchan=lockf spsr=60000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=0012abcd",
            "8894 wchan=lockf spsr=60000010 r0=00000007 r1=00000009 r2=7fffd000 r3=00000000 pc=00034560",
            "USER     CMD          PID   FD MOUNT       INUM MODE         SZ|DV R/W",
            "root     smbd        8893   12 /Volumes/dk2 48811 -rw-rw-rw-  921344 r",
            "root     smbd        8894    7 /mnt/Locks     5 -rw-r--r--   12288 rw",
        ]))
        self.assertEqual(run.debug_fields["smbd_lock_wait_calls"], [
            {"pid": 8893, "call": "flock LOCK_EX", "fd": 12,
             "file": "/Volumes/dk2 48811 -rw-rw-rw- 921344 r"},
            {"pid": 8894, "call": "fcntl F_SETLKW", "fd": 7,
             "file": "/mnt/Locks 5 -rw-r--r-- 12288 rw"},
        ])
        self.assertNotIn("remote_lock_waiters_fstat_skipped", run.debug_fields)
        # Only the two-minute waiter fails, as a file lock rather than a database's.
        waits = [result for result in run.results if "file lock" in result.message]
        self.assertEqual([(result.status, result.details) for result in waits], [("FAIL", {"smbd_lock_waits": [8893]})])
        self.assertIn("backups and open files hang, until the lock is released", waits[0].message)

    def test_run_doctor_checks_reads_no_registers_of_short_lock_waits_but_still_lists_tdb_locks(self) -> None:
        nine = " 8893 457 457 I 9 lockf 0:00.00 smbd"
        locks = "/mnt/Locks/locking.tdb pid 9001 write 1460+1\n"
        run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, nine]), file_locks=locks)

        run.lock_waiters.assert_not_called()
        self.assertNotIn("remote_lock_waiters", run.debug_fields)
        self.assertEqual(run.lock_reads, ["locks", "disk locks"])
        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())

    def test_run_doctor_checks_reads_lock_waiters_without_fstat_while_a_process_is_stuck(self) -> None:
        # fstat is not known to stay off a disk that stopped answering.
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW, self.STUCK_SMBD_ROW]),
            lock_waiters="8893 wchan=lockf spsr=60000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=0012abcd\n",
        )

        self.assertEqual(run.lock_reads, ["waiters without fstat", "locks"])
        self.assertEqual(run.debug_fields["remote_lock_waiters_fstat_skipped"], "a process is stuck in the kernel")
        self.assertEqual(run.debug_fields["smbd_lock_wait_calls"], [
            {"pid": 8893, "call": "flock LOCK_EX", "fd": 12, "file": None},
        ])

    def test_run_doctor_checks_lists_tdb_locks_when_the_lock_waiters_cannot_be_read(self) -> None:
        locks = "/mnt/Locks/brlock.tdb pid 457 read 4+1 (TDB open marker)\n"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW]), file_locks=locks,
            lock_waiters=SshCommandTimeout("Timed out waiting for ssh command to finish: service"),
        )

        self.assertIn("SshCommandTimeout", run.debug_fields["remote_lock_waiters_error"])
        self.assertNotIn("remote_lock_waiters", run.debug_fields)
        self.assertNotIn("smbd_lock_wait_calls", run.debug_fields)
        self.assertEqual(run.lock_reads, ["waiters", "locks", "disk locks"])
        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())

    def test_run_doctor_checks_records_lock_waiters_output_with_no_readable_call(self) -> None:
        # A service without the option, a kernel that refused /dev/kmem, or a
        # waiter that woke while it was read: the text is kept, no call guessed.
        for output, recorded in (
            ("", "(empty)"),
            ("/dev/kmem: Permission denied\n", "/dev/kmem: Permission denied"),
            ("8893: changed during the read\n", "8893: changed during the read"),
        ):
            with self.subTest(output=output):
                run = self.run_doctor_with_process_snapshot(
                    "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW]), lock_waiters=output,
                )
                self.assertEqual(run.debug_fields["remote_lock_waiters"], recorded)
                self.assertNotIn("smbd_lock_wait_calls", run.debug_fields)

    def test_run_doctor_checks_fails_smbds_that_waited_two_minutes_for_a_lock(self) -> None:
        # ps counts the seconds asleep up to 127; 120 is two minutes.
        second = " 8894 457 457 I 120 lockf 0:00.00 smbd"
        locks = "/mnt/Locks/locking.tdb pid 9001 write 1460+1\n"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW, second]), file_locks=locks,
        )

        waits = [result for result in run.results if "file lock" in result.message]
        self.assertEqual([result.status for result in waits], ["FAIL"])
        self.assertTrue(waits[0].message.startswith(
            "Samba has waited at least 2 minutes for a file lock (PIDs 8893, 8894); "))
        self.assertTrue(waits[0].message.endswith("Restart the device to clear it"))
        self.assertEqual(waits[0].details, {"smbd_lock_waits": [8893, 8894]})
        self.assertTrue(run.fatal)
        # The holder's PID travels with the failure; new connections still work.
        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))
        run.mocks.check_authenticated_smb_listing.assert_called()

    def test_run_doctor_checks_lists_the_locks_of_a_shorter_lock_wait_without_failing(self) -> None:
        # A wait that ends within two minutes is a busy lock, not a stuck one.
        short = " 8893 457 457 I 119 lockf 0:00.00 smbd"
        locks = "/mnt/Locks/locking.tdb pid 9001 write 1460+1\n"
        run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, short]), file_locks=locks)

        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())
        self.assertFalse(any("file lock" in result.message for result in run.results))
        self.assertFalse(run.fatal)

    def test_run_doctor_checks_records_a_failed_lock_listing_and_carries_on(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW]),
            file_locks=SshCommandTimeout("Timed out waiting for ssh command to finish: service"),
        )

        self.assertIn("SshCommandTimeout", run.debug_fields["remote_file_locks_error"])
        self.assertNotIn("remote_file_locks", run.debug_fields)
        # A device that cannot list its RAM TDBs is not asked about the disk's.
        run.file_locks.assert_called_once()
        self.assertNotIn("remote_file_locks_disk", run.debug_fields)
        self.assertIn(self.LOCKF_SMBD_ROW.strip(), run.debug_fields["remote_process_snapshot"])
        run.mocks.check_authenticated_smb_listing.assert_called()

    def test_run_doctor_checks_lists_no_data_disk_locks_while_a_process_is_stuck_in_the_kernel(self) -> None:
        # The disk may be what the stuck process waits on; listing its TDBs could hang too.
        locks = "/mnt/Locks/brlock.tdb pid 457 read 4+1\n"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW, self.STUCK_SMBD_ROW]), file_locks=locks,
        )

        self.assertEqual([c.args[1] for c in run.file_locks.call_args_list], [FILE_LOCKS_RAM_TDBS])
        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())
        self.assertNotIn("remote_file_locks_disk", run.debug_fields)
        self.assertEqual(run.debug_fields["remote_file_locks_disk_skipped"], "a process is stuck in the kernel")

    def test_run_doctor_checks_keeps_the_ram_locks_when_the_data_disk_listing_times_out(self) -> None:
        locks = "/mnt/Locks/brlock.tdb pid 457 read 4+1\n"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.LOCKF_SMBD_ROW]), file_locks=locks,
            disk_file_locks=SshCommandTimeout("Timed out waiting for ssh command to finish: service"),
        )

        self.assertEqual(run.debug_fields["remote_file_locks"], locks.rstrip())
        self.assertIn("SshCommandTimeout", run.debug_fields["remote_file_locks_disk_error"])
        self.assertNotIn("remote_file_locks_disk", run.debug_fields)

    def test_run_doctor_checks_records_the_kernel_limits_the_manager_raises(self) -> None:
        limits = "kern.sbmax = 2359296\nkern.maxfiles = 4096\nkern.maxproc = 84\n"
        run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, kern_limits=limits)

        run.kern_limits.assert_called_once()
        self.assertEqual(run.debug_fields["remote_kern_limits"], limits.rstrip())
        # A limit left at Apple's value is diagnostic data, not a failure.
        self.assertFalse(run.fatal)

    def test_run_doctor_checks_records_a_failed_kernel_limit_read_and_carries_on(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            self.HEALTHY_SMBD_ROW, kern_limits=SshCommandTimeout("Timed out waiting for ssh command to finish: sysctl"),
        )

        self.assertIn("SshCommandTimeout", run.debug_fields["remote_kern_limits_error"])
        self.assertNotIn("remote_kern_limits", run.debug_fields)
        run.mocks.check_authenticated_smb_listing.assert_called()

    def test_run_doctor_checks_reads_no_kernel_limits_when_the_process_list_times_out(self) -> None:
        # A device out of processes has none to spare for sysctl either.
        run = self.run_doctor_with_process_snapshot(SshCommandTimeout("Timed out waiting for ssh command to finish: ps"))

        run.kern_limits.assert_not_called()
        self.assertNotIn("remote_kern_limits", run.debug_fields)

    def test_run_doctor_checks_reads_retrying_waits_from_the_manager_title(self) -> None:
        # ps alone shows needbuf with a sleep time near 0: it wakes to retry.
        snapshot = "\n".join(
            [
                "  146 1 2 S 1 select 0:00.00 service  service: role=manager stuck=3166:smbd:needbuf:600",
                " 3166 457 457 D 0 needbuf 0:00.00 smbd",
            ]
        )
        run = self.run_doctor_with_process_snapshot(snapshot)

        stuck = [result for result in run.results if "blocked in the kernel" in result.message]
        self.assertEqual(len(stuck), 1)
        self.assertIn("smbd (pid 3166) waiting on needbuf for 600 s", stuck[0].message)
        run.mocks.check_authenticated_smb_listing.assert_not_called()

    def test_run_doctor_checks_counts_stuck_processes_the_manager_title_does_not_name(self) -> None:
        snapshot = "  146 1 2 S 1 select 0:00.00 service  service: role=manager stuck=3166:smbd:needbuf:600,+5"
        run = self.run_doctor_with_process_snapshot(snapshot)

        stuck = [result for result in run.results if "blocked in the kernel" in result.message]
        self.assertEqual(len(stuck), 1)
        self.assertIn("smbd (pid 3166) waiting on needbuf for 600 s; and 5 more", stuck[0].message)
        self.assertEqual(stuck[0].details["stuck_processes_unnamed"], 5)

    def test_run_doctor_checks_reports_stuck_non_smbd_process_but_still_checks_smb(self) -> None:
        stuck_mdns = "  359 119 2 D 125 tstile 0:00.00 mDNSResponder"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, stuck_mdns]),
            smb_listing=self.connection_shaped_smb_failure(),
        )

        stuck = [result for result in run.results if "blocked in the kernel" in result.message]
        self.assertEqual(len(stuck), 1)
        self.assertIn("mDNSResponder (pid 359) waiting on tstile for 125 s", stuck[0].message)
        # Retried as usual, on the device and then over the LAN.
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 6)
        self.assertEqual([call.args[0] for call in run.sleep.call_args_list], [10, 15, 10, 15])

    def test_run_doctor_checks_ignores_short_uninterruptible_sleeps_and_kernel_threads(self) -> None:
        snapshot = "\n".join(
            [
                self.HEALTHY_SMBD_ROW,
                " 3166 457 457 D 119 biowait 0:00.00 smbd",
                "    0 0 0 DKl 127 uvm 0:00.00 system",
            ]
        )
        run = self.run_doctor_with_process_snapshot(snapshot)

        self.assertFalse(run.fatal)
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))
        run.mocks.check_authenticated_smb_listing.assert_called()
        self.assertIn("uvm", run.debug_fields["remote_process_snapshot"])

    def test_run_doctor_checks_tries_smb_once_and_skips_data_disk_logs_when_snapshot_times_out(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
            smb_listing=self.connection_shaped_smb_failure(),
        )

        self.assertTrue(run.fatal)
        warnings = [result for result in run.results if "listing the device's processes timed out" in result.message]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].status, "WARN")
        # Once on the device, once over the LAN: neither retries.
        self.assertEqual(
            [call.args[2] for call in run.mocks.check_authenticated_smb_listing.call_args_list][0], "127.0.0.1",
        )
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 2)
        run.sleep.assert_not_called()
        self.assertEqual(
            run.log_tails.call_args.kwargs["skip_data_disk"],
            "the device's process list timed out",
        )
        self.assertIn("SshCommandTimeout", run.debug_fields["remote_process_snapshot_error"])
        self.assertNotIn("remote_process_snapshot", run.debug_fields)
        self.assertEqual(run.debug_fields["remote_process_table"], "(unavailable)")
        self.assertEqual(warnings[0].details, {})

    def test_run_doctor_checks_names_what_a_timed_out_snapshot_waited_for(self) -> None:
        # The table without command lines never waits, and shows the
        # processes asleep in the kernel that the listing may have waited for.
        table = "  812 700 812 D 127 vmmapbsy 0:00.00 ps\n"
        asleep = "ps (pid 812) on vmmapbsy, service (pid 700) on biowait"
        run = self.run_doctor_with_process_snapshot(
            SshCommandTimeout("Timed out waiting for ssh command to finish: ps"),
            smb_listing=self.connection_shaped_smb_failure(),
            process_table=(table, asleep),
        )

        warnings = [result for result in run.results if "listing the device's processes timed out" in result.message]
        self.assertEqual(len(warnings), 1)
        # The warning keeps one text; the sleepers ride in its details.
        self.assertNotIn("asleep", warnings[0].message)
        self.assertEqual(warnings[0].details, {"asleep_in_kernel": asleep})
        self.assertEqual(run.debug_fields["remote_process_table"], table.rstrip())

    def test_doctor_names_what_a_timed_out_mdns_process_listing_waited_for(self) -> None:
        table = "  812 700 812 D 127 vmmapbsy 0:00.00 ps\n"
        asleep = "ps (pid 812) on vmmapbsy"
        detail = "mDNS process table probe timed out after 30s"
        for step, expected_info, expected_table in (
            (ProbeStepResult("mdns_process_table_probe", "timeout", detail, stdout=table, asleep_in_kernel=asleep),
             [("INFO", f"processes asleep in the kernel: {asleep}")], table.rstrip()),
            # The follow-up read failed too: nothing to name.
            (ProbeStepResult("mdns_process_table_probe", "timeout", detail), [], None),
        ):
            with self.subTest(asleep=step.asleep_in_kernel):
                debug_fields: dict[str, object] = {}
                probe = ReadinessProbeResult(ready=False, detail=detail, steps=(step,))
                with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep"):
                    run = self.run_doctor_with_mocks(
                        ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                        skip_smb=True,
                        debug_fields=debug_fields,
                        extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mock.Mock(return_value=probe)},
                    )
                self.assertTrue(any(result.status == "FAIL" and result.message == detail for result in run.results))
                self.assertEqual(
                    [(result.status, result.message) for result in run.results
                     if str(result.message).startswith("processes asleep in the kernel")],
                    expected_info,
                )
                self.assertEqual(debug_fields.get("mdns_process_table"), expected_table)

    def test_run_doctor_checks_keeps_normal_flow_when_snapshot_fails_without_timeout(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            SshError("ssh command failed with rc=255"),
            smb_listing=self.connection_shaped_smb_failure(),
        )

        self.assertFalse(any(result.status == "WARN" and "processes" in result.message for result in run.results))
        # Three tries on the device, three over the LAN.
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 6)
        self.assertIsNone(run.log_tails.call_args.kwargs["skip_data_disk"])
        self.assertIn("ssh command failed", run.debug_fields["remote_process_snapshot_error"])

    def test_run_doctor_checks_does_not_list_processes_when_ssh_login_fails(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            self.STUCK_SMBD_ROW,
            ssh_login=mock.Mock(status="FAIL", message="ssh failed"),
        )

        run.snapshot.assert_not_called()
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))

    def test_run_doctor_checks_reports_stuck_processes_while_the_device_starts(self) -> None:
        stuck_mdns = "  359 119 2 D 125 tstile 0:00.00 mDNSResponder"
        for row, code in ((self.STUCK_SMBD_ROW, "disk_not_responding"), (stuck_mdns, None)):
            with self.subTest(row=row):
                run = self.run_doctor_with_process_snapshot(row, manager_started_seconds_ago=41.0)

                failures = [result for result in run.results if result.status == "FAIL"]
                stuck = [result for result in failures if "stuck_processes" in result.details]
                self.assertEqual(len(stuck), 1)
                self.assertEqual(stuck[0].details.get("code"), code)

    STUCK_ACPD_ROWS = "\n".join((
        "  119 1 2 Sl 0 kqueue 0:00.00 ACPd",
        "  352 119 2 S 1 nanoslp 0:00.00 ACPd",
    ))

    def apple_acpd_failures(self, run) -> list[CheckResult]:
        return [result for result in run.results if result.details.get("code") == "apple_acpd_broken"]

    def test_run_doctor_checks_fails_a_stuck_fork_of_apple_acpd_while_the_device_starts(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, self.STUCK_ACPD_ROWS]), manager_started_seconds_ago=41.0,
        )

        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.status for result in failures], ["FAIL"])
        self.assertTrue(failures[0].message.startswith("Apple ACPd is broken: its helper process (pid 352) is stuck"))
        self.assertIn("bug in Apple's firmware", failures[0].message)
        self.assertEqual(failures[0].domain, "device")
        self.assertEqual(failures[0].details["acpd_stuck_children"], [352])
        self.assertEqual(run.debug_fields["acpd_problems"], ["stuck_child"])
        # A problem's details reach telemetry, not only the check's details.
        self.assertEqual(run.debug_fields["acpd_stuck_children"], [352])
        self.assertTrue(run.fatal)

    def test_run_doctor_checks_fails_a_restart_that_never_happened_while_the_device_starts(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=True) as marker:
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, manager_started_seconds_ago=41.0)

        marker.assert_called_once()
        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["restart_never_happened"])
        # The app reads this flag, not the problem's name, to say ACPd is gone.
        self.assertIs(failures[0].details["acpd_gone"], True)
        self.assertTrue(failures[0].message.startswith("Apple ACPd is broken: the device did not restart"))
        self.assertIn("then check again", failures[0].message)
        self.assertEqual(run.debug_fields["acpd_problems"], ["restart_never_happened"])
        self.assertTrue(run.fatal)

    def test_run_doctor_checks_gives_a_hung_restart_one_failure(self) -> None:
        # The torn-down ACPd also fails syUT and may leave its fork: one cause,
        # one fix, so one FAIL.
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=True), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False):
            run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, self.STUCK_ACPD_ROWS]))

        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["restart_never_happened"])
        self.assertEqual(run.debug_fields["acpd_problems"], ["restart_never_happened"])

    def test_run_doctor_checks_fails_an_apple_acpd_that_does_not_answer_syut(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False):
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW)

        failures = self.apple_acpd_failures(run)
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].message.startswith("Apple ACPd is broken: the AirPort service on this device is stuck"))
        self.assertEqual(failures[0].details["acpd_problem"], "syut_silent")
        self.assertIs(failures[0].details["acpd_gone"], False)
        self.assertEqual(run.debug_fields["acpd_problems"], ["syut_silent"])

    # A finished listing without ACPd, as doctor's own listing and the recheck read it.
    QUIT_LISTING = "\n".join(("  107 1 15 S 0 select 0:00.32 sshd", "tcapsule-ps: end"))

    def test_run_doctor_checks_fails_an_apple_acpd_that_quit_once_and_stops(self) -> None:
        # One cause, one fix: its silent syUT is not a second failure, and the
        # runtime, Samba and Bonjour checks after it would only restate it.
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False) as syut, \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.QUIT_LISTING) as recheck:
            run = self.run_doctor_with_process_snapshot(f"{self.HEALTHY_SMBD_ROW}\n{self.QUIT_LISTING}")

        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["quit"])
        self.assertIs(failures[0].details["acpd_gone"], True)
        self.assertEqual(
            failures[0].message,
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "File sharing stays off until the device restarts. Unplug the device, wait 10 seconds, plug it back "
            "in and wait 2 minutes, then check again. On NetBSD 4 devices, run Activate if file sharing does not "
            "start.",
        )
        self.assertEqual(run.debug_fields["acpd_problems"], ["quit"])
        recheck.assert_called_once()
        syut.assert_not_called()
        self.assertTrue(run.fatal)
        # The whole verdict: nothing after it ran, and the manager's log was read for it.
        self.assertEqual([result for result in run.results if result.status == "FAIL"], failures)
        run.mocks.check_authenticated_smb_listing.assert_not_called()
        run.log_tails.assert_called_once()

    def test_run_doctor_checks_goes_on_past_an_acpd_that_quit_while_samba_serves(self) -> None:
        # Samba started before ACPd quit keeps serving: the FAIL says sharing
        # may work, and the checks after it say how it is.
        serving = f"  107 1 15 S 0 select 0:00.32 sshd\n{self.HEALTHY_SMBD_ROW}\ntcapsule-ps: end"
        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=serving) as recheck:
            run = self.run_doctor_with_process_snapshot(serving)

        recheck.assert_called_once()
        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["quit"])
        self.assertIs(failures[0].details["acpd_gone"], True)
        self.assertEqual(
            failures[0].message,
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "Sharing may work now, but until the device is unplugged it cannot be updated or restarted over the "
            "network, and AirPort Utility cannot reach it. Unplug the device, wait 10 seconds, plug it back in and "
            "wait 2 minutes, then check again. On NetBSD 4 devices, run Activate if file sharing does not start.",
        )
        self.assertTrue(failures[0].details["acpd_quit_smbd_running"])
        self.assertTrue(run.debug_fields["acpd_quit_smbd_running"])
        run.mocks.check_authenticated_smb_listing.assert_called()

    def test_acpd_exit_while_samba_serves_does_not_blame_old_disk_errors(self) -> None:
        serving = f"{self.HEALTHY_SMBD_ROW}\n{self.QUIT_LISTING}"
        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=serving), \
                mock.patch("timecapsulesmb.checks.doctor_steps.read_disk_errors_conn", return_value=self.DISK_ERRORS) as read:
            run = self.run_doctor_with_process_snapshot(serving)

        read.assert_not_called()
        self.assertNotIn("disk_failing", self.fail_codes(run))
        run.mocks.check_authenticated_smb_listing.assert_called()

    # A finished listing with neither ACPd nor smbd: a restart tore ACPd down
    # after the runtime stopped for it.
    TORN_DOWN_LISTING = QUIT_LISTING

    def test_acpd_gone_does_not_hide_this_boots_disk_errors(self) -> None:
        # ACPd's exit explains the absent runtime, but cannot explain away
        # failed disk transfers. Keep both diagnoses without probing Samba.
        for restart in (False, True):
            for errors in (self.DISK_ERRORS, (), None):
                with self.subTest(restart=restart, errors=errors), \
                        mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=restart), \
                        mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.QUIT_LISTING), \
                        mock.patch("timecapsulesmb.checks.doctor_steps.read_disk_errors_conn", return_value=errors) as read:
                    run = self.run_doctor_with_process_snapshot(self.QUIT_LISTING)

                    read.assert_called_once()
                    expected = ["apple_acpd_broken"] + (["disk_failing"] if errors else [])
                    self.assertEqual(self.fail_codes(run), expected)
                    if errors:
                        self.assertEqual(run.debug_fields["disk_error_lines"], list(errors))
                    run.mocks.check_authenticated_smb_listing.assert_not_called()
                    run.log_tails.assert_called_once()

    def test_run_doctor_checks_stops_at_a_restart_that_never_happened_while_samba_is_off(self) -> None:
        # ACPd is gone as when it quit: one FAIL is the whole verdict.
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=True):
            run = self.run_doctor_with_process_snapshot(self.TORN_DOWN_LISTING)

        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["restart_never_happened"])
        self.assertEqual(
            failures[0].message,
            "Apple ACPd is broken: the device did not restart when TimeCapsuleSMB last asked it to, a bug in "
            "Apple's firmware. Restarting it again would fail the same way. Unplug the device, wait 10 seconds, "
            "plug it back in and wait 2 minutes, then check again. On NetBSD 4 devices, run Activate if file "
            "sharing does not start.",
        )
        self.assertTrue(run.fatal)
        self.assertEqual([result for result in run.results if result.status == "FAIL"], failures)
        run.mocks.check_authenticated_smb_listing.assert_not_called()
        run.log_tails.assert_called_once()

    def test_run_doctor_checks_goes_on_past_a_restart_that_never_happened_while_samba_serves(self) -> None:
        # smbd still runs, or the listing cannot say: the checks after it run.
        serving = f"  107 1 15 S 0 select 0:00.32 sshd\n{self.HEALTHY_SMBD_ROW}\ntcapsule-ps: end"
        for name, listing in (("smbd running", serving), ("listing cut short", "  107 1 15 S 0 select 0:00.32 sshd")):
            with self.subTest(name), mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=True):
                run = self.run_doctor_with_process_snapshot(listing)

                failures = self.apple_acpd_failures(run)
                self.assertEqual([result.details["acpd_problem"] for result in failures], ["restart_never_happened"])
                run.mocks.check_authenticated_smb_listing.assert_called()

    def test_run_doctor_checks_goes_on_past_an_acpd_that_is_only_stuck(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False):
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW)

        self.assertEqual([result.details["acpd_problem"] for result in self.apple_acpd_failures(run)], ["syut_silent"])
        run.mocks.check_authenticated_smb_listing.assert_called()

    # The field case: Apple's mount_hfs asleep on a disk buffer at every boot,
    # diskd waiting on it, and ACPd (parked) behind diskd.
    STALLED_DISK_ROWS = "\n".join((
        "  119 1 2 Sl 127 parked 0:00.83 ACPd",
        "   39 144 39 I 127 wait 0:00.02 diskd /sbin/diskd -i lo0 -d local.",
        "  421 379 39 D 127 biolock 0:00.00 mount_hfs",
    ))

    def test_run_doctor_checks_blames_a_stuck_disk_not_the_acpd_waiting_behind_it(self) -> None:
        # ACPd answers neither its own acp nor the network: that is the disk,
        # reported once as the disk, with no ACPd failure.
        evidence = {"remote_kern_limits": "kern.maxfiles = 4096", "remote_kernel_table_full": []}
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.network_silent_details", return_value=evidence):
            run = self.run_doctor_with_process_snapshot(self.STALLED_DISK_ROWS)

        disk = [result for result in run.results if result.details.get("code") == "disk_not_responding"]
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0].status, "FAIL")
        self.assertIn("mount_hfs (pid 421) waiting on biolock for 127+ s", disk[0].message)
        self.assertIn("Unplug the device", disk[0].message)
        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))
        # What ACPd did is still in the diagnostics.
        self.assertEqual(run.debug_fields["acpd_problems"], ["syut_silent"])

    def test_run_doctor_checks_reads_the_list_again_before_blaming_a_silent_acpd(self) -> None:
        # The snapshot came too early to show mount_hfs as stuck; ACPd's slow
        # reads let it pass the threshold, and the second listing says so.
        young = self.STALLED_DISK_ROWS.replace(" D 127 biolock", " D 100 biolock")
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.STALLED_DISK_ROWS) as listing:
            run = self.run_doctor_with_process_snapshot(young)

        disk = [result for result in run.results if result.details.get("code") == "disk_not_responding"]
        self.assertEqual(len(disk), 1)
        self.assertIn("mount_hfs (pid 421) waiting on biolock for 127+ s", disk[0].message)
        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertIn("biolock", run.debug_fields["remote_process_snapshot_after_acpd"])
        listing.assert_called_once()

    def test_run_doctor_checks_blames_a_silent_acpd_when_the_second_listing_shows_no_stall(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.HEALTHY_SMBD_ROW):
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW)

        self.assertEqual([result.details["acpd_problem"] for result in self.apple_acpd_failures(run)], ["syut_silent"])
        self.assertEqual([r for r in run.results if r.details.get("code") == "disk_not_responding"], [])

    SSHD_ROW = "  107 1 15 S 0 select 0:00.32 sshd"
    LISTING_END = "tcapsule-ps: end"

    def finished_listing(self, *rows: str) -> str:
        """A listing that can say a process is gone (processes.listing_complete)."""
        return "\n".join((self.SSHD_ROW, *rows, self.LISTING_END))

    def test_run_doctor_checks_stops_at_a_stuck_disk_while_samba_is_off(self) -> None:
        # No smbd, or an smbd itself stuck: the disk is the whole verdict, and
        # doctor neither waits for a startup nor checks the services it stops.
        cases = {
            "no smbd": self.finished_listing(self.STALLED_DISK_ROWS),
            "smbd stuck beside it": self.finished_listing(self.STALLED_DISK_ROWS, self.STUCK_SMBD_ROW),
        }
        for name, listing in cases.items():
            with self.subTest(name):
                run = self.run_doctor_with_process_snapshot(listing)

                self.assertTrue(run.fatal)
                self.assertEqual(
                    [r.details.get("code") for r in run.results if r.status == "FAIL"], ["disk_not_responding"])
                run.mocks.probe_managed_smbd_conn.assert_not_called()
                run.mocks.check_authenticated_smb_listing.assert_not_called()
                # What the manager logged still reaches the diagnostics.
                run.log_tails.assert_called_once()

    def test_run_doctor_checks_goes_on_past_a_stuck_disk_while_smbd_serves(self) -> None:
        # A USB disk stuck while the internal disk's Samba serves: the checks
        # after the verdict still say how sharing is.
        run = self.run_doctor_with_process_snapshot(self.finished_listing(self.STALLED_DISK_ROWS, self.HEALTHY_SMBD_ROW))

        self.assertEqual(len([r for r in run.results if r.details.get("code") == "disk_not_responding"]), 1)
        run.mocks.probe_managed_smbd_conn.assert_called()

    def test_run_doctor_checks_adds_the_disk_line_when_the_second_listing_finds_the_disk(self) -> None:
        # The first listing showed only a stuck name lookup; the second shows
        # mount_hfs stuck too. The first line stays, and the disk's line names
        # what waits for the disk in the second.
        stuck_mdns = "  359 119 2 D 125 tstile 0:00.00 mDNSResponder"
        young = self.STALLED_DISK_ROWS.replace(" D 127 biolock", " D 100 biolock")
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.read_processes",
                           return_value="\n".join([stuck_mdns, self.STALLED_DISK_ROWS])):
            run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, stuck_mdns, young]))

        stuck_lines = [r for r in run.results if isinstance(r.details, dict) and "stuck_processes" in r.details]
        self.assertEqual([r.details.get("code") for r in stuck_lines], [None, "disk_not_responding"])
        self.assertIn("mDNSResponder (pid 359) waiting on tstile for 125 s", stuck_lines[0].message)
        self.assertEqual([p["pid"] for p in stuck_lines[1].details["stuck_processes"]], [421])
        self.assertIn("mount_hfs (pid 421) waiting on biolock for 127+ s", stuck_lines[1].message)
        self.assertEqual(self.apple_acpd_failures(run), [])

    def test_run_doctor_checks_reads_the_list_again_when_acpd_is_silent_beside_a_stuck_fork(self) -> None:
        # The stuck fork is ACPd's own and stays reported; the silence is the
        # disk's, which passed the stall threshold during ACPd's reads.
        fork = "  352 119 2 S 1 nanoslp 0:00.00 ACPd"
        young = self.STALLED_DISK_ROWS.replace(" D 127 biolock", " D 100 biolock")
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.read_processes",
                           return_value="\n".join([fork, self.STALLED_DISK_ROWS])) as listing:
            run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, fork, young]))

        listing.assert_called_once()
        disk = [r for r in run.results if r.details.get("code") == "disk_not_responding"]
        self.assertEqual(len(disk), 1)
        self.assertIn("mount_hfs (pid 421) waiting on biolock for 127+ s", disk[0].message)
        self.assertEqual([r.details["acpd_problem"] for r in self.apple_acpd_failures(run)], ["stuck_child"])

    def test_run_doctor_checks_stops_at_a_known_stall_when_acpd_cannot_be_read(self) -> None:
        # SSH failed while ACPd was read: the stuck-process step's disk verdict
        # still decides whether doctor goes on, by whether Samba serves.
        cases = {
            "no smbd": (self.finished_listing(self.STALLED_DISK_ROWS), True),
            "smbd serves": (self.finished_listing(self.STALLED_DISK_ROWS, self.HEALTHY_SMBD_ROW), False),
        }
        for name, (listing, stops) in cases.items():
            with self.subTest(name), \
                    mock.patch("timecapsulesmb.device.acpd.acpd_problems", side_effect=SshError("ssh failed")):
                run = self.run_doctor_with_process_snapshot(listing)

                self.assertEqual(
                    [r.details.get("code") for r in run.results if r.details.get("code") == "disk_not_responding"],
                    ["disk_not_responding"])
                self.assertEqual(run.mocks.probe_managed_smbd_conn.called, not stops)

    def test_run_doctor_checks_stops_when_every_smbd_is_stuck_on_the_disk(self) -> None:
        # The parent and its only child both wait for the disk: nothing serves.
        stuck_parent = "  457 146 457 D 127 biowait 0:00.00 smbd"
        run = self.run_doctor_with_process_snapshot(self.finished_listing(stuck_parent, self.STUCK_SMBD_ROW))

        self.assertTrue(run.fatal)
        run.mocks.probe_managed_smbd_conn.assert_not_called()

    def test_run_doctor_checks_still_reports_an_acpd_problem_a_stuck_disk_does_not_cause(self) -> None:
        # A restart that never happened is ACPd's own, stuck disk or not.
        with mock.patch("timecapsulesmb.device.acpd.restart_never_happened", return_value=True):
            run = self.run_doctor_with_process_snapshot(self.STALLED_DISK_ROWS)

        self.assertEqual([result.details["acpd_problem"] for result in self.apple_acpd_failures(run)],
                         ["restart_never_happened"])
        self.assertEqual(len([r for r in run.results if r.details.get("code") == "disk_not_responding"]), 1)

    def test_run_doctor_checks_does_not_call_a_listing_cut_short_a_quit_acpd(self) -> None:
        run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW)

        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertEqual(run.debug_fields["acpd_problems"], [])

    UNANSWERED_ACP_READ = AirportAcpReading(password_matches=None, error="ACP receive failed: timed out")

    def test_run_doctor_checks_fails_an_apple_acpd_that_answers_its_own_acp_but_not_the_network(self) -> None:
        evidence = {"remote_kern_limits": "kern.maxfiles = 4096", "remote_kernel_table_full": []}
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=False) as network, \
                mock.patch("timecapsulesmb.device.acpd.network_silent_details", return_value=evidence):
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, device_reading=self.UNANSWERED_ACP_READ)

        network.assert_called_once()
        failures = self.apple_acpd_failures(run)
        self.assertEqual([result.details["acpd_problem"] for result in failures], ["network_silent"])
        self.assertTrue(failures[0].message.startswith("Apple ACPd is broken: the AirPort service stopped answering the network"))
        self.assertIn("then check again", failures[0].message)
        self.assertEqual(failures[0].domain, "device")
        self.assertEqual(run.debug_fields["acpd_problems"], ["network_silent"])
        # The evidence reaches telemetry, not only the check's details.
        self.assertEqual(failures[0].details["remote_kernel_table_full"], [])
        self.assertEqual(run.debug_fields["remote_kernel_table_full"], [])
        self.assertTrue(run.fatal)

    def test_run_doctor_checks_passes_an_apple_acpd_that_answers_the_network_through_ssh(self) -> None:
        # Only the path from this computer is blocked: not ACPd's problem.
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=True) as network:
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, device_reading=self.UNANSWERED_ACP_READ)

        network.assert_called_once()
        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertEqual(run.debug_fields["acpd_problems"], [])

    def test_run_doctor_checks_reads_apple_acpd_through_ssh_only_after_an_unanswered_read(self) -> None:
        for reading in (None, AirportAcpReading(password_matches=True)):
            with self.subTest(reading=reading):
                with mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", return_value=False) as network:
                    run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, device_reading=reading)
                network.assert_not_called()
                self.assertEqual(self.apple_acpd_failures(run), [])

    def test_run_doctor_checks_skips_apple_acpd_when_the_forward_fails(self) -> None:
        # An SSH failure is the device's or the network's, not ACPd's.
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers_network", side_effect=SshError("tunnel failed")):
            run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW, device_reading=self.UNANSWERED_ACP_READ)

        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertNotIn("acpd_problems", run.debug_fields)

    def test_run_doctor_checks_passes_a_healthy_apple_acpd(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.read_processes") as processes:
            run = self.run_doctor_with_process_snapshot("\n".join([self.HEALTHY_SMBD_ROW, self.STUCK_ACPD_ROWS.splitlines()[0]]))

        # The stuck-process step's listing is used, not read again.
        processes.assert_not_called()
        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertEqual(run.debug_fields["acpd_problems"], [])

    def test_run_doctor_checks_asks_apple_acpd_nothing_when_ps_did_not_answer(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers") as answer, \
                mock.patch("timecapsulesmb.device.acpd.restart_never_happened") as marker:
            run = self.run_doctor_with_process_snapshot(SshCommandTimeout("Timed out waiting for ssh command to finish: /bin/ps"))

        answer.assert_not_called()
        marker.assert_not_called()
        self.assertEqual(self.apple_acpd_failures(run), [])
        self.assertNotIn("acpd_problems", run.debug_fields)

    def test_run_doctor_checks_skips_apple_acpd_when_ssh_cannot_ask(self) -> None:
        from timecapsulesmb.transport.errors import SshError

        for read in ("acpd_answers", "restart_never_happened"):
            with self.subTest(read=read):
                with mock.patch(f"timecapsulesmb.device.acpd.{read}", side_effect=SshError("Connection reset")):
                    run = self.run_doctor_with_process_snapshot(self.HEALTHY_SMBD_ROW)

                self.assertEqual(self.apple_acpd_failures(run), [])
                self.assertNotIn("acpd_problems", run.debug_fields)

    def run_doctor_with_hostname(self, probe: DeviceHostnameProbeResult, *, started_seconds_ago: float = 3600.0):
        return self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_device_hostname_conn": mock.Mock(return_value=probe),
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(started_seconds_ago, f"manager started {int(started_seconds_ago)}s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

    def run_doctor_with_manager(
        self, manager: ManagerStartupAgeProbeResult, *, debug_fields=None, log_tails=None, extra_patches=None,
    ):
        smbd = mock.Mock(side_effect=AssertionError("the startup wait probed smbd"))
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(return_value=manager),
                "timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": smbd,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value=log_tails or {}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                **(extra_patches or {}),
            },
        )
        return run, smbd

    def test_doctor_stops_at_a_payload_missing_or_damaged_without_waiting_for_startup(self) -> None:
        # An Erase in AirPort Utility, or a binary on the disk that fails its
        # deployed checksum: Samba stays off until a reinstall, so doctor
        # neither waits out the startup window nor lists what follows.
        logs = {
            "payload=missing": "umount: /Volumes/dk2: Device busy\nInitialized /dev/rdk2 as a 1861 GB HFS Plus volume",
            "payload=damaged": "copy verify failed: /Volumes/dk2/.samba4/smbd -> /mnt/Memory/samba4/sbin/smbd: "
                               "CRC-32 read da7a298c, deployed 961339d2",
            # A disk that could not read smbd: no kernel line here (NetBSD 4's
            # wraps), so the verdict alone says the disk is failing, with its
            # code and way out instead of a reinstall. Its manager is past the
            # startup window: a younger one may still clear it, so doctor
            # waits for that (below).
            "payload=unreadable": "copy read failed: /Volumes/dk2/.samba4/smbd -> /mnt/Memory/samba4/sbin/smbd: "
                                  "Operation timed out (copied 3211264 of 10307500 bytes in 180 s)",
        }
        for problem in (problem for problem in DISK_PAYLOAD_PROBLEMS if problem.samba_off):
            log = logs[problem.token]
            with self.subTest(problem.token):
                debug_fields: dict[str, object] = {}
                age = 600.0 if problem.replaces_disk else 12.0
                run, smbd = self.run_doctor_with_manager(
                    ManagerStartupAgeProbeResult(age, f"manager started {int(age)}s ago", payload_problems=(problem,)),
                    debug_fields=debug_fields,
                    log_tails={"remote_manager_log_tail": log},
                )

                failure = run.results[-1]
                self.assertEqual((failure.status, failure.message), ("FAIL", problem.message))
                self.assertEqual(failure.details["code"], problem.code)
                self.assertTrue(run.fatal)
                smbd.assert_not_called()
                self.assertEqual(sum(result.status == "FAIL" for result in run.results), 1)
                # One flag per cause, so a query reads the same key in every
                # release's telemetry.
                for other in DISK_PAYLOAD_PROBLEMS:
                    self.assertEqual(debug_fields["manager_startup_age"][other.debug_key], other is problem)
                self.assertEqual({other.debug_key for other in DISK_PAYLOAD_PROBLEMS},
                                 {"payload_missing", "payload_damaged", "payload_unreadable",
                                  "rsync_missing", "rsync_damaged", "rsync_unreadable"})
                self.assertEqual(problem.code == ErrorCode.PAYLOAD_UNREADABLE_ON_DISK, problem.token == "payload=unreadable")
                self.assertEqual("replace the disk" in failure.message, problem.token == "payload=unreadable")
                # The manager's log reaches telemetry: it shows what removed
                # or damaged the payload.
                self.assertEqual(debug_fields["remote_manager_log_tail"], log)
                self.assertEqual(debug_fields["remote_runtime_ram_diagnostics"], "ram ok")

    def test_doctor_reports_a_verdict_that_leaves_samba_on_in_the_rsync_check(self) -> None:
        # rsync=missing or rsync=damaged keeps only rsync off: doctor goes on
        # through the Samba checks, and the rsync check fails with why instead
        # of waiting for rsync. The manager's log reaches telemetry, as for
        # any verdict.
        logs = {
            "rsync=missing": "stage: /Volumes/dk2/.samba4/rsync is missing",
            "rsync=damaged": "copy verify failed: /Volumes/dk2/.samba4/rsync -> /mnt/Memory/samba4/sbin/rsync: "
                             "CRC-32 read 1, deployed 2",
            "rsync=unreadable": "copy read failed: /Volumes/dk2/.samba4/rsync -> /mnt/Memory/samba4/sbin/rsync: "
                                "Input/output error (copied 32768 of 1037900 bytes in 4 s)",
        }
        problems = [problem for problem in DISK_PAYLOAD_PROBLEMS if not problem.samba_off]
        self.assertEqual({problem.token for problem in problems}, set(logs))
        for problem in problems:
            with self.subTest(problem.token):
                debug_fields: dict[str, object] = {}
                rsync = mock.Mock(side_effect=AssertionError("doctor probed rsync"))
                smbd = mock.Mock(return_value=mock.Mock(ready=True, detail="managed smbd ready",
                                                        lines=("PASS:managed smbd parent process is running",)))
                run, _ = self.run_doctor_with_manager(
                    ManagerStartupAgeProbeResult(400.0, "manager started 400s ago", payload_problems=(problem,)),
                    debug_fields=debug_fields,
                    log_tails={"remote_manager_log_tail": logs[problem.token]},
                    extra_patches={
                        "timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": smbd,
                        "timecapsulesmb.checks.doctor_steps.probe_managed_rsync_conn": rsync,
                    },
                )
                failures = [result for result in run.results if result.status == "FAIL"]
                self.assertEqual([(result.message, result.details.get("code")) for result in failures],
                                 [(problem.message, problem.code)])
                smbd.assert_called()
                rsync.assert_not_called()
                self.assertTrue(debug_fields["manager_startup_age"][problem.debug_key])
                self.assertEqual(debug_fields["remote_manager_log_tail"], logs[problem.token])

    # This boot's disk errors (device.disk_errors).
    DISK_ERRORS = ("NetBSD IAL (ERROR)  [0 0] device error occured",)
    SMBD_DOWN = mock.Mock(ready=False, detail="managed smbd parent process is not running",
                          lines=("FAIL:managed smbd parent process is not running",))

    def run_doctor_with_disk_errors(self, manager, errors, *, smbd=None, debug_fields=None, extra=None):
        read = mock.Mock(return_value=errors)
        patches = {"timecapsulesmb.checks.doctor_steps.read_disk_errors_conn": read, **(extra or {})}
        if smbd is not None:
            patches["timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn"] = mock.Mock(return_value=smbd)
        # A Samba that stays down is retried on the doctor's clock, which these
        # runs never wait out in real time.
        with mock.patch("time.sleep"):
            run, _ = self.run_doctor_with_manager(manager, debug_fields=debug_fields, extra_patches=patches)
        return run, read

    @staticmethod
    def fail_codes(run) -> list[str]:
        return [result.details.get("code") or result.message for result in run.results if result.status == "FAIL"]

    def test_disk_errors_replace_a_payload_verdict_whose_fix_they_would_undo(self) -> None:
        # The manager says smbd is damaged; the disk says why, and a reinstall
        # would not help. One line, and doctor stops there as before.
        damaged = next(problem for problem in DISK_PAYLOAD_PROBLEMS if problem.token == "payload=damaged")
        debug_fields: dict[str, object] = {}
        run, _read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(12.0, "manager started 12s ago", payload_problems=(damaged,)),
            self.DISK_ERRORS, debug_fields=debug_fields,
        )

        self.assertEqual(self.fail_codes(run), ["disk_failing"])
        [failure] = [result for result in run.results if result.status == "FAIL"]
        # Checkup's line also says the way out, which an error leaves to its next steps.
        self.assertEqual(failure.message, f"{disk_failing_message(self.DISK_ERRORS)} {DISK_FAILING_WAY_OUT}")
        self.assertEqual(failure.domain, "device")
        self.assertEqual(debug_fields["disk_error_lines"], list(self.DISK_ERRORS))

    def test_disk_errors_come_before_a_disk_that_will_not_mount(self) -> None:
        # Its fix (a restart) still applies, so both lines stay.
        run, _read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(14400.0, "manager started 14400s ago", unmounted_since=1260),
            self.DISK_ERRORS,
        )

        self.assertEqual(self.fail_codes(run), ["disk_failing", "disk_not_mounting"])

    def test_an_erased_disk_is_not_blamed_on_errors_logged_before_the_erase(self) -> None:
        missing = next(problem for problem in DISK_PAYLOAD_PROBLEMS if problem.token == "payload=missing")
        run, _read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(12.0, "manager started 12s ago", payload_problems=(missing,)),
            self.DISK_ERRORS,
        )

        self.assertEqual(self.fail_codes(run), [missing.code])

    def test_disk_errors_explain_a_failed_run_without_a_verdict(self) -> None:
        # diskd keeps exiting, nothing is staged, and the title says nothing:
        # the failed run reports the disk.
        run, read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(400.0, "manager started 400s ago"), self.DISK_ERRORS, smbd=self.SMBD_DOWN,
        )

        codes = self.fail_codes(run)
        self.assertIn("managed smbd parent process is not running", codes)
        self.assertEqual(codes.count("disk_failing"), 1)
        read.assert_called_once()

    def test_a_failed_run_reuses_the_kernel_lines_a_disk_probe_already_read(self) -> None:
        run, read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(400.0, "manager started 400s ago"), None, smbd=self.SMBD_DOWN,
            debug_fields={"remote_kernel_disk_messages": "\n".join(("ahcisata0: port 1 reset failed",) + self.DISK_ERRORS)},
        )

        self.assertIn("disk_failing", self.fail_codes(run))
        read.assert_not_called()

    def test_a_run_that_failed_elsewhere_while_samba_serves_is_not_blamed_on_the_disk(self) -> None:
        # Something else failed (here diskd's RPC names) while smbd serves:
        # the disk's errors do not explain it, and "file sharing is off"
        # would be false.
        def diskd_fails(_target, _remote, sink):
            sink.add(CheckResult("FAIL", "diskd RPC names are missing"))

        smbd_up = mock.Mock(ready=True, detail="managed smbd ready", lines=("PASS:managed smbd parent process is running",))
        run, read = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(400.0, "manager started 400s ago"), self.DISK_ERRORS, smbd=smbd_up,
            extra={"timecapsulesmb.checks.doctor._doctor_check_diskd_rpc": diskd_fails},
        )

        self.assertTrue(run.fatal)
        self.assertIn("diskd RPC names are missing", self.fail_codes(run))
        self.assertNotIn("disk_failing", self.fail_codes(run))
        read.assert_not_called()

    def test_disk_errors_say_nothing_on_a_run_that_passed_or_could_not_read_them(self) -> None:
        manager = ManagerStartupAgeProbeResult(400.0, "manager started 400s ago")
        smbd_up = mock.Mock(ready=True, detail="managed smbd ready", lines=("PASS:managed smbd parent process is running",))
        for errors, smbd in ((self.DISK_ERRORS, smbd_up), (None, self.SMBD_DOWN), ((), self.SMBD_DOWN)):
            with self.subTest(errors=errors, failing=smbd is self.SMBD_DOWN):
                run, read = self.run_doctor_with_disk_errors(manager, errors, smbd=smbd)
                self.assertNotIn("disk_failing", self.fail_codes(run))
                if smbd is smbd_up:
                    self.assertFalse(run.fatal)
                    read.assert_not_called()

    def test_doctor_goes_on_to_the_samba_checks_when_the_payload_is_present(self) -> None:
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))

        self.assertFalse({problem.message for problem in DISK_PAYLOAD_PROBLEMS} & {result.message for result in run.results})
        self.assertNotIn(DISK_NOT_MOUNTING_MESSAGE, [result.message for result in run.results])
        self.assertTrue(any("smbd" in result.message for result in run.results))

    # diskd takes every claim and never mounts the disk: its second thread
    # waits on a disk that stopped answering.
    HUNG_DISK_KERNEL = "wd0: <APPLE HDD ST2000DM001, AP54>"
    HUNG_DISK_THREADS = (
        "  PID   LID STAT WCHAN     SL UCOMM\n"
        "  211     1 S    kqueue     9 diskd\n"
        "  211     2 D    biowait  127 diskd"
    )

    @staticmethod
    def disk_evidence(*, kernel, threads):
        """read_remote_command_conn for DISK_EVIDENCE_COMMANDS: each result is
        the output, or an exception it raises."""
        from timecapsulesmb.device.probe import DISK_EVIDENCE_COMMANDS

        outputs = {
            DISK_EVIDENCE_COMMANDS["remote_kernel_disk_messages"]: kernel,
            DISK_EVIDENCE_COMMANDS["remote_diskd_threads"]: threads,
        }

        def read(_connection, command):
            result = outputs[command]
            if isinstance(result, Exception):
                raise result
            return result

        return mock.Mock(side_effect=read)

    def run_doctor_with_unmounted_disk(self, *, evidence, debug_fields):
        mast = mock.Mock(return_value=self.mast_probe_diagnostics())
        run, smbd = self.run_doctor_with_manager(
            ManagerStartupAgeProbeResult(14400.0, "manager started 14400s ago", unmounted_since=1260),
            debug_fields=debug_fields,
            log_tails={"remote_manager_log_tail": "storage: retry uuid=b035 root=/Volumes/dk2 stage=activate"},
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast,
                "timecapsulesmb.checks.doctor_debug.read_remote_command_conn": evidence,
            },
        )
        return run, smbd, mast

    def test_doctor_stops_at_a_disk_that_has_not_mounted_and_says_to_restart(self) -> None:
        # Samba stays off until a restart, so doctor gives that one verdict
        # instead of the smbd, rsync and Bonjour failures that follow from it,
        # and keeps what shows why: MaSt, the kernel's disk lines, diskd's
        # threads and the manager's log.
        debug_fields: dict[str, object] = {}
        run, smbd, mast = self.run_doctor_with_unmounted_disk(
            evidence=self.disk_evidence(kernel=self.HUNG_DISK_KERNEL + "\n", threads=self.HUNG_DISK_THREADS + "\n"),
            debug_fields=debug_fields,
        )

        failure = run.results[-1]
        self.assertEqual((failure.status, failure.message), ("FAIL", DISK_NOT_MOUNTING_MESSAGE))
        self.assertEqual(failure.details["code"], DOCTOR_CODE_DISK_NOT_MOUNTING)
        self.assertTrue(run.fatal)
        smbd.assert_not_called()
        self.assertEqual(sum(result.status == "FAIL" for result in run.results), 1)
        mast.assert_called_once()
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)
        self.assertEqual(debug_fields["remote_kernel_disk_messages"], self.HUNG_DISK_KERNEL)
        self.assertEqual(debug_fields["remote_diskd_threads"], self.HUNG_DISK_THREADS)
        self.assertEqual(debug_fields["manager_startup_age"]["unmounted_since"], 1260)
        self.assertIn("stage=activate", debug_fields["remote_manager_log_tail"])

    def test_doctor_leaves_the_disk_that_does_not_mount_to_the_stuck_disk_verdict(self) -> None:
        # mount_hfs stuck on the disk is why it does not mount: one verdict,
        # the disk's, without "run Disk Repair", which would wait on it too.
        stuck = "  421 379 39 D 127 biolock 0:00.00 mount_hfs"
        run, smbd = self.run_doctor_with_manager(
            ManagerStartupAgeProbeResult(14400.0, "manager started 14400s ago", unmounted_since=1260),
            debug_fields={},
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.read_process_snapshot_conn": mock.Mock(return_value=stuck),
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn":
                    mock.Mock(return_value=self.mast_probe_diagnostics()),
                "timecapsulesmb.checks.doctor_debug.read_remote_command_conn":
                    self.disk_evidence(kernel=self.HUNG_DISK_KERNEL, threads=self.HUNG_DISK_THREADS),
            },
        )

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.details.get("code") for result in failures], ["disk_not_responding"])
        self.assertNotIn(DISK_NOT_MOUNTING_MESSAGE, [result.message for result in run.results])
        self.assertTrue(run.fatal)
        # Still the whole verdict: nothing after it restates the disk.
        smbd.assert_not_called()

    def test_doctor_keeps_the_verdict_and_the_kernel_lines_when_the_thread_listing_hangs(self) -> None:
        # ps can wait on a process that holds its map lock; the kernel's disk
        # lines, read by a command of their own, are kept.
        debug_fields: dict[str, object] = {}
        run, _, _ = self.run_doctor_with_unmounted_disk(
            evidence=self.disk_evidence(
                kernel=self.HUNG_DISK_KERNEL,
                threads=SshCommandTimeout("Timed out waiting for ssh command to finish: /bin/ps -axs"),
            ),
            debug_fields=debug_fields,
        )

        self.assertEqual(run.results[-1].details["code"], DOCTOR_CODE_DISK_NOT_MOUNTING)
        self.assertEqual(debug_fields["remote_kernel_disk_messages"], self.HUNG_DISK_KERNEL)
        self.assertNotIn("remote_diskd_threads", debug_fields)
        self.assertIn("Timed out", debug_fields["remote_diskd_threads_error"])
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)

    MAPPED = ("127.0.0.1\tcapsule capsule.local",)
    WAITING_MESSAGE = (
        "Samba is waiting for the device hostname (ACPd has not set it); "
        "Samba cannot start or restage until it is set, and the Samba and "
        "Time Machine checks below may fail because of it"
    )

    def test_doctor_passes_a_mapped_hostname_ahead_of_the_samba_checks(self) -> None:
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))

        messages = [result.message for result in run.results]
        passed = messages.index("device hostname capsule is mapped in /etc/hosts")
        self.assertEqual(run.results[passed].status, "PASS")
        samba = [index for index, message in enumerate(messages) if "smbd" in message]
        self.assertTrue(samba and passed < min(samba))
        self.assertFalse(any(result.status == "INFO" and "hostname" in result.message for result in run.results))

    def test_doctor_fails_while_samba_waits_for_the_hostname(self) -> None:
        for probe in (
            DeviceHostnameProbeResult("", (), manager_waiting=True),
            DeviceHostnameProbeResult("", ()),  # unset, whatever the manager shows
            DeviceHostnameProbeResult("capsule", self.MAPPED, manager_waiting=True),
        ):
            with self.subTest(probe=probe):
                run = self.run_doctor_with_hostname(probe)
                failure = next(result for result in run.results if result.message == self.WAITING_MESSAGE)
                self.assertEqual(failure.status, "FAIL")
                self.assertEqual(failure.details["code"], "hostname_waiting")
                self.assertTrue(run.fatal)

    def test_doctor_reports_a_hostname_lost_after_boot_only_as_waiting(self) -> None:
        # ACPd cleared the name while Samba runs: the manager waits to restage.
        # The old name's line is not "earlier" and the name is not "unmapped".
        run = self.run_doctor_with_hostname(
            DeviceHostnameProbeResult("", self.MAPPED, manager_waiting=True, boot_wait_ms=4950)
        )

        hostname_results = [result for result in run.results if "hostname" in result.message]
        self.assertEqual(
            [(result.status, result.message) for result in hostname_results],
            [("FAIL", self.WAITING_MESSAGE), ("INFO", "the manager waited 4950 ms for the device hostname at boot")],
        )
        self.assertFalse(any(result.details.get("code") == "hostname_unmapped" for result in run.results))

    def test_doctor_fails_an_unmapped_hostname(self) -> None:
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", ("127.0.0.1\tlocalhost",)))

        failure = next(result for result in run.results if result.details.get("code") == "hostname_unmapped")
        self.assertEqual(failure.status, "FAIL")
        self.assertEqual(
            failure.message,
            "device hostname capsule is not mapped in /etc/hosts; Samba logins stall until it is (issue #54)",
        )

    def test_doctor_fails_hostname_problems_while_the_device_starts(self) -> None:
        # Samba starts only once the hostname is mapped, so the startup wait
        # has already waited for it: what is left is a real failure.
        for probe, code in (
            (DeviceHostnameProbeResult("", (), manager_waiting=True), "hostname_waiting"),
            (DeviceHostnameProbeResult("capsule", ()), "hostname_unmapped"),
        ):
            with self.subTest(code=code):
                run = self.run_doctor_with_hostname(probe, started_seconds_ago=20.0)
                failure = next(result for result in run.results if result.details.get("code") == code)
                self.assertEqual(failure.status, "FAIL")
                self.assertTrue(run.fatal)

    def test_doctor_reports_the_boot_wait_and_stale_mappings_as_info(self) -> None:
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult(
            "capsule",
            self.MAPPED + ("127.0.0.1\told old.local",),
            boot_wait_ms=4330,
        ))

        info = {result.message: result for result in run.results if result.status == "INFO"}
        self.assertEqual(info["the manager waited 4330 ms for the device hostname at boot"].details["boot_wait_ms"], 4330)
        self.assertEqual(info["/etc/hosts still maps an earlier hostname: old"].details["stale_names"], ["old"])
        self.assertTrue(any(result.status == "PASS" and result.message == "device hostname capsule is mapped in /etc/hosts"
                            for result in run.results))

    def test_doctor_fails_when_the_hostname_cannot_be_read(self) -> None:
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("", error="device hostname probe timed out"))

        self.assertTrue(any(
            result.status == "FAIL" and result.message == "could not read the device hostname: device hostname probe timed out"
            for result in run.results
        ))

    def test_doctor_skips_the_hostname_probe_without_ssh(self) -> None:
        run = self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, skip_smb=True)

        self._device_hostname_probe.assert_not_called()
        self.assertFalse(any("hostname" in result.message for result in run.results))

    def test_doctor_reports_diskd_rpc_routing_only_as_information(self) -> None:
        baseline = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))
        cases = [
            ("answered", "diskd RPC: getVolumeCounts answered"),
            ("-6727", "diskd RPC: getVolumeCounts failed: -6727 "
                      "(ACPd lost diskd's RPC names; restarting the device restores them)"),
            ("?", "diskd RPC: getVolumeCounts failed: ?"),
        ]
        for status, message in cases:
            with self.subTest(status=status):
                self._diskd_rpc_probe.return_value = status
                run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))
                diskd = [(result.status, result.message) for result in run.results if result.message.startswith("diskd RPC")]
                self.assertEqual(diskd, [("INFO", message)])
                self.assertEqual(run.fatal, baseline.fatal)

    def test_doctor_reports_an_unavailable_diskd_rpc_check_as_information(self) -> None:
        baseline = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))
        self._diskd_rpc_probe.side_effect = RuntimeError("ssh dropped")
        run = self.run_doctor_with_hostname(DeviceHostnameProbeResult("capsule", self.MAPPED))

        diskd = [(result.status, result.message) for result in run.results if result.message.startswith("diskd RPC")]
        self.assertEqual(diskd, [("INFO", "diskd RPC check unavailable: ssh dropped")])
        self.assertEqual(run.fatal, baseline.fatal)

    def test_doctor_skips_the_diskd_rpc_check_without_ssh(self) -> None:
        self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, skip_smb=True)

        self._diskd_rpc_probe.assert_not_called()

    def test_run_doctor_checks_reports_device_failures_as_themselves_at_any_manager_age(self) -> None:
        for age in (41.0, 400.0):
            with self.subTest(age=age):
                debug_fields: dict[str, object] = {}
                streamed: list[CheckResult] = []
                run = self.run_doctor_with_mocks(
                    ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                    smb_port=mock.Mock(status="PASS", message="445 ok"),
                    smb_instance=[],
                    smb_listing=self.smb_listing_result(),
                    smb_file_ops=[],
                    mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
                    run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
                    debug_fields=debug_fields,
                    on_result=streamed.append,
                    extra_patches={
                        "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                            return_value=ManagerStartupAgeProbeResult(age, f"manager started {int(age)}s ago")
                        ),
                        "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                        "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                    },
                )

                self.assertTrue(run.fatal)
                failure = next(result for result in run.results if result.status == "FAIL")
                self.assertIn("managed mDNS registrant is not active", failure.message)
                self.assertEqual(failure.domain, "device")
                # Streamed as found, with nothing added after the checks.
                self.assertEqual(streamed, run.results)
                self.assertEqual(debug_fields["manager_startup_age"]["seconds_ago"], age)

    def test_run_doctor_checks_reports_smb_auth_failure_while_the_device_starts(self) -> None:
        debug_fields: dict[str, object] = {}
        auth_failure = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 home.local: NT_STATUS_LOGON_FAILURE",
            {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_LOGON_FAILURE"}]},
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=auth_failure,
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            debug_fields=debug_fields,
            # The device's own login fails the same way: waiting cannot fix it.
            real_device_smb=True,
            # ACP did not answer, so whether the password is right is unknown.
            device_reading=acp_reading(None),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        # Samba rejecting the saved password is this computer's setup.
        rejected = [result for result in run.results if result.details.get("code") == "smb_login_rejected"]
        self.assertEqual([(result.status, result.domain) for result in rejected], [("FAIL", "computer")])
        self.assertIn("NT_STATUS_LOGON_FAILURE", rejected[0].message)

    def test_run_doctor_checks_reports_an_smb_timeout_after_its_retries_while_the_device_starts(self) -> None:
        connection_failure = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 home.local: NT_STATUS_IO_TIMEOUT",
            {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]},
        )
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                smb_instance=[],
                smb_listing=connection_failure,
                smbd_probe=mock.Mock(ready=True, detail="managed smbd is ready"),
                mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
                run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
                extra_patches={
                    "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                        return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                    ),
                    "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                    "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                },
            )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 15])

        # Samba was ready, so a timeout from here is a failure, not startup.
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual(len(failures), 1)
        self.assertIn("NT_STATUS_IO_TIMEOUT", failures[0].message)

    def test_run_doctor_checks_reports_compatibility_failure_while_the_device_starts(self) -> None:
        unsupported_state = mock.Mock(
            probe_result=mock.Mock(
                ssh_authenticated=True,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="big",
            ),
            compatibility=DeviceCompatibility(
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="big",
                payload_family=None,
                device_generation="unknown",
                supported=False,
                reason_code="unsupported_netbsd6_endianness",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            precomputed_probe_state=unsupported_state,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertTrue(
            any("not supported by the current Samba payload" in result.message for result in failures)
        )

    def test_run_doctor_checks_passing_run_is_untouched_by_recent_startup(self) -> None:
        debug_fields: dict[str, object] = {}
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_bonjour=True,
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
            },
        )

        self.assertFalse(run.fatal)
        self.assertFalse(any(result.status == "FAIL" for result in run.results))

    def test_run_doctor_checks_adds_mast_probe_for_xattr_parent_missing(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())
        smbd_probe = mock.Mock(
            ready=False,
            detail="xattr parent missing",
            lines=("FAIL:active smb.conf xattr_tdb:file parent is missing",),
        )

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            smbd_probe=smbd_probe,
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb\n[Data]\n",
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
            },
        )

        self.assertTrue(run.fatal)
        mast_probe_mock.assert_called_once()
        self.assertEqual(debug_fields["mast_probe_command"], MAST_PROBE_COMMAND)
        failures = [result for result in run.results if result.status == "FAIL"]
        payload_missing = [
            result
            for result in failures
            if result.message == "active smb.conf xattr_tdb:file parent is missing"
        ]
        self.assertEqual(len(payload_missing), 1)
        self.assertEqual(payload_missing[0].details["code"], DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK)
        self.assertEqual(debug_fields["mast_probe_returncode"], 0)
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)
        self.assertEqual(debug_fields["mast_probe_candidates"][0]["part"], "dk2")

    def test_run_doctor_checks_adds_mast_probe_for_unmounted_share_volume(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())
        evidence = self.disk_evidence(kernel="sd0: drive offline\n", threads="(not listed on NetBSD 4)\n")
        smbd_probe = mock.Mock(
            ready=False,
            detail="share volume missing",
            lines=("FAIL:one or more managed share volumes are not mounted",),
        )

        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            smbd_probe=smbd_probe,
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_remote_command_conn": evidence,
            },
        )

        mast_probe_mock.assert_called_once()
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)
        # The kernel's disk lines and diskd's threads go with every MaSt capture.
        self.assertEqual(debug_fields["remote_kernel_disk_messages"], "sd0: drive offline")
        self.assertEqual(debug_fields["remote_diskd_threads"], "(not listed on NetBSD 4)")

    def test_run_doctor_checks_adds_mast_probe_for_bad_network_name_file_ops(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())

        self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[CheckResult("FAIL", "SMB directory create failed: tree connect failed: NT_STATUS_BAD_NETWORK_NAME")],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
            },
        )

        mast_probe_mock.assert_called_once()
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)

    def test_run_doctor_checks_skips_mast_probe_for_unrelated_fatal(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())

        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb\n[Data]\n",
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
            },
        )

        mast_probe_mock.assert_not_called()
        self.assertNotIn("mast_probe_command", debug_fields)
        self.assertNotIn("remote_kernel_disk_messages", debug_fields)
        self.assertNotIn("remote_diskd_threads", debug_fields)

    def test_run_doctor_checks_records_mast_probe_exception_without_replacing_failure(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(side_effect=RuntimeError("boom"))
        smbd_probe = mock.Mock(
            ready=False,
            detail="share volume missing",
            lines=("FAIL:one or more managed share volumes are not mounted",),
        )

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            smbd_probe=smbd_probe,
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock,
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
            },
        )

        self.assertTrue(any(result.message == "one or more managed share volumes are not mounted" for result in run.results))
        self.assertEqual(debug_fields["mast_probe_error"], "RuntimeError: boom")

    def test_run_doctor_checks_skips_mast_probe_when_ssh_login_fails(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())

        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="FAIL", message="ssh failed"),
            smb_listing=CheckResult("FAIL", "mock authenticated SMB listing failure"),
            debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock},
        )

        mast_probe_mock.assert_not_called()
        self.assertNotIn("mast_probe_command", debug_fields)

    def test_run_doctor_checks_skips_mast_probe_when_ssh_is_skipped(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())

        self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn": mast_probe_mock},
        )

        mast_probe_mock.assert_not_called()
        self.assertNotIn("mast_probe_command", debug_fields)

    def test_run_doctor_checks_reports_managed_smbd_subchecks(self) -> None:
        smbd_probe = mock.Mock(
            ready=False,
            detail="smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            lines=(
                "PASS:managed runtime smb.conf present",
                "PASS:managed smbd parent process is running",
                "FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            ),
        )
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                smb_instance=[],
                smb_listing=self.smb_listing_result(),
                smb_file_ops=[],
                smbd_probe=smbd_probe,
                mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
                run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            )
        self.assertTrue(run.fatal)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [10, 15])
        self.assertTrue(any(result.status == "PASS" and result.message == "managed smbd parent process is running" for result in run.results))
        self.assertTrue(any(result.status == "FAIL" and result.message == "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener" for result in run.results))
        self.assertFalse(any(result.message.startswith("managed smbd is not ready") for result in run.results))

    def test_run_doctor_checks_retries_transient_smbd_parent_failure_before_streaming_result(self) -> None:
        transient = mock.Mock(
            ready=False,
            detail="managed smbd parent process is not running",
            lines=("FAIL:managed smbd parent process is not running",),
        )
        ready = mock.Mock(
            ready=True,
            detail="managed smbd ready",
            lines=("PASS:managed smbd parent process is running", "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners"),
        )
        smbd_mock = mock.Mock(side_effect=[transient, ready])
        streamed: list[CheckResult] = []

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                on_result=streamed.append,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": smbd_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(smbd_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        self.assertFalse(any(result.message == "managed smbd parent process is not running" for result in run.results))
        self.assertFalse(any(result.message == "managed smbd parent process is not running" for result in streamed))
        self.assertTrue(any(result.status == "PASS" and result.message == "smbd owns IPv4 and IPv6 wildcard TCP 445 listeners" for result in run.results))

    def test_run_doctor_checks_retries_transient_missing_wildcard_listener(self) -> None:
        transient = mock.Mock(
            ready=False,
            detail="smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            lines=(
                "PASS:managed smbd parent process is running",
                "FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            ),
        )
        ready = mock.Mock(
            ready=True,
            detail="managed smbd ready",
            lines=(
                "PASS:managed smbd parent process is running",
                "PASS:smbd owns IPv4 and IPv6 wildcard TCP 445 listeners",
            ),
        )
        smbd_mock = mock.Mock(side_effect=[transient, ready])

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": smbd_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(smbd_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        self.assertFalse(any(result.message == "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener" for result in run.results))
        self.assertTrue(any(result.status == "PASS" and result.message == "smbd owns IPv4 and IPv6 wildcard TCP 445 listeners" for result in run.results))

    def test_run_doctor_checks_does_not_retry_structural_smbd_failure_mixed_with_transient_failure(self) -> None:
        smbd_probe = mock.Mock(
            ready=False,
            detail="managed runtime smbd binary missing; smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            lines=(
                "FAIL:managed runtime smbd binary missing",
                "FAIL:smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            ),
        )
        smbd_mock = mock.Mock(return_value=smbd_probe)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn": smbd_mock},
            )

        self.assertTrue(run.fatal)
        smbd_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_run_doctor_checks_retries_transient_mdns_process_failure(self) -> None:
        transient = mock.Mock(
            ready=False,
            detail="discovery process is not running",
            lines=("FAIL:discovery process is not running",),
        )
        ready = mock.Mock(
            ready=True,
            detail="managed mDNS registrant active",
            lines=("PASS:discovery process is running", "PASS:mdns bound to required UDP 5353 listeners"),
        )
        mdns_mock = mock.Mock(side_effect=[transient, ready])

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mdns_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(mdns_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        self.assertFalse(any(result.message == "discovery process is not running" for result in run.results))
        self.assertTrue(any(result.status == "PASS" and result.message == "mdns bound to required UDP 5353 listeners" for result in run.results))

    def test_run_doctor_checks_retries_native_nbns_startup(self) -> None:
        transient = mock.Mock(
            ready=False,
            detail="discovery native NBNS is still starting",
            lines=(
                "PASS:discovery process is running",
                "FAIL:discovery native NBNS is still starting",
            ),
        )
        ready = mock.Mock(
            ready=True,
            detail="managed mDNS registrant active",
            lines=(
                "PASS:discovery process is running",
                "PASS:mdns bound to required UDP 5353 listeners",
            ),
        )
        mdns_mock = mock.Mock(side_effect=[transient, ready])

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mdns_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(mdns_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        self.assertFalse(any(result.message == "discovery native NBNS is still starting" for result in run.results))
        self.assertTrue(any(result.status == "PASS" and result.message == "mdns bound to required UDP 5353 listeners" for result in run.results))

    @staticmethod
    def _native_nbns_probe(status: str, detail: str) -> ReadinessProbeResult:
        return ReadinessProbeResult(
            ready=status == "pass",
            detail=detail,
            steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("native_nbns", status, detail),
            ),
        )

    @staticmethod
    def _nbns_query_timeout() -> CheckResult:
        return CheckResult("FAIL", "NBNS query for 'TimeCapsule' timed out against 10.0.0.2:137",
                           {"code": NBNS_QUERY_TIMEOUT_CODE})

    def _run_doctor_nbns(
        self,
        mdns_mock,
        nbns_mock,
        *,
        startup_age: float = 3600.0,
        client_source: str | None = "10.0.0.50",
        device_probe_mock=None,
        socket_debug_mock=None,
        debug_fields: dict[str, object] | None = None,
        startup_age_mock=None,
        on_stage=None,
        probe_seconds: float = 0.0,
        smbd_mock=None,
        extra_patches: dict[str, object] | None = None,
    ):
        """Run doctor on a fake clock that only sleeps and simulated work move:
        `probe_seconds` per mDNS probe. `smbd_mock` replaces the ready smbd probe."""
        clock = FakeClock()

        def timed_mdns(*args, **kwargs):
            clock.mdns_at.append(clock.now)
            clock.advance(probe_seconds)
            return mdns_mock(*args, **kwargs)

        early_checks = dict(extra_patches or {})
        if smbd_mock is not None:
            early_checks["timecapsulesmb.checks.doctor_steps.probe_managed_smbd_conn"] = smbd_mock
        # This computer defaults to 10.0.0.50, on the harness device's 10.0.0.0/24.
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_bonjour=True,
            skip_smb=True,
            client_source=client_source,
            debug_fields=debug_fields,
            on_stage=on_stage,
            clock=clock,
            extra_patches={
                **early_checks,
                "timecapsulesmb.checks.doctor_steps.probe_device_networks_conn": device_networks_probe(
                    device_probe_mock or mock.Mock(return_value=SAME_SUBNET_DEVICE_PROBE)
                ),
                "timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": timed_mdns,
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": startup_age_mock or mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(startup_age, f"manager started {int(startup_age)}s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                "timecapsulesmb.checks.doctor_debug.read_remote_service_socket_diagnostics_conn": (
                    socket_debug_mock or mock.Mock(return_value="")
                ),
            },
        )
        run.clock = clock
        return run, clock.sleeps

    def test_run_doctor_checks_retries_a_bonjour_registration_waiting_for_discoverys_retry(self) -> None:
        degraded = ReadinessProbeResult(
            ready=False,
            detail=MDNS_REGISTRATIONS_DEGRADED,
            steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("mdns_registered", "fail", MDNS_REGISTRATIONS_DEGRADED),
            ),
        )
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        mdns_mock = mock.Mock(side_effect=[degraded, ready])
        nbns_mock = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

        run, sleeps = self._run_doctor_nbns(mdns_mock, nbns_mock)

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10])
        self.assertEqual(mdns_mock.call_count, 2)

    def test_run_doctor_checks_does_not_blame_this_computer_for_nbns_the_device_reports_not_ready(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        nbns_mock = mock.Mock(return_value=self._nbns_query_timeout())

        run, _sleeps = self._run_doctor_nbns(mock.Mock(return_value=not_ready), nbns_mock)

        query = next(result for result in run.results if result.details.get("code") == NBNS_QUERY_TIMEOUT_CODE)
        self.assertEqual((query.status, query.domain), ("SKIP", "computer"))
        self.assertIn("not counted against this computer: NetBIOS (NBNS) is not ready on the device", query.message)
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["device"]["status"], "fail")
        self.assertNotIn(NBNS_QUERY_TIMEOUT_CODE, sections["computer"]["fail_codes"])

    def test_run_doctor_checks_polls_native_nbns_through_startup_instead_of_failing(self) -> None:
        # After a boot Bonjour registers at once, but discovery's first NBNS
        # add can go unanswered: NBNS is "still starting", then "not ready"
        # between retries, then ready. A doctor run 42 s in passes once it is.
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        mdns_mock = mock.Mock(side_effect=[starting] * 7 + [not_ready] + [ready])
        nbns_mock = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))
        stages: list[tuple[str, str | None]] = []
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mdns_mock, nbns_mock, startup_age=42.0, on_stage=lambda *stage: stages.append(stage), debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10] * 8)
        self.assertEqual(mdns_mock.call_count, 9)
        nbns_mock.assert_called_once()
        # The wait is announced once, with how long it may take, then checks resume.
        self.assertEqual(stages, [
            ("wait_for_device_startup",
             "device services started 42s ago; waiting up to 138s for NetBIOS to finish starting..."),
            ("finish_checks", None),
        ])
        self.assertEqual(debug_fields["startup_wait"], {
            "manager_age_sec": 42, "window_left_sec": 138.0, "waited_sec": 80.0, "probes": 9, "outcome": "ready",
        })

    def test_run_doctor_checks_does_not_announce_a_wait_when_mdns_is_ready_at_once(self) -> None:
        stages: list[tuple[str, str | None]] = []
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=42.0, on_stage=lambda *stage: stages.append(stage), debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual((sleeps, stages), ([], []))
        self.assertEqual(debug_fields["startup_wait"], {
            "manager_age_sec": 42, "window_left_sec": 138.0, "waited_sec": 0.0, "probes": 1, "outcome": "ready",
        })

    def test_run_doctor_checks_startup_window_never_waits_on_a_structural_mdns_failure(self) -> None:
        # Waiting fixes only the states discovery passes through while starting.
        broken = ReadinessProbeResult(
            ready=False,
            detail="discovery native NBNS state does not match the active plan",
            steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("native_nbns", "fail", "discovery native NBNS state does not match the active plan"),
            ),
        )
        mdns_mock = mock.Mock(return_value=broken)
        stages: list[tuple[str, str | None]] = []
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mdns_mock, mock.Mock(return_value=self._nbns_query_timeout()), startup_age=42.0,
            on_stage=lambda *stage: stages.append(stage), debug_fields=debug_fields,
        )

        mdns_mock.assert_called_once()
        self.assertEqual(sleeps[:1], [10])  # The query's own retry, not a startup poll.
        self.assertEqual(stages, [])
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "structural")
        self.assertTrue(any(result.status == "FAIL"
                            and result.message == "discovery native NBNS state does not match the active plan"
                            for result in run.results))

    @staticmethod
    def _smbd_probe(ready: bool, failure: str = "managed smbd parent process is not running") -> mock.Mock:
        lines = ("PASS:managed smbd parent process is running",) if ready else (f"FAIL:{failure}",)
        return mock.Mock(ready=ready, detail="smbd ok" if ready else failure, lines=lines, steps=())

    def test_run_doctor_checks_waits_for_samba_starting_before_the_device_checks(self) -> None:
        # smbd not yet listening at age 45 is waited for; its check reports
        # the wait's last probe.
        smbd_mock = mock.Mock(side_effect=[self._smbd_probe(False)] * 3 + [self._smbd_probe(True)])
        stages: list[tuple[str, str | None]] = []

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=45.0, smbd_mock=smbd_mock, on_stage=lambda *stage: stages.append(stage),
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10, 10, 10])
        self.assertEqual(smbd_mock.call_count, 4)
        self.assertEqual(stages[0], ("wait_for_device_startup",
                                     "device services started 45s ago; waiting up to 135s for Samba to finish starting..."))
        self.assertFalse(any(result.status == "FAIL" for result in run.results))

    def test_run_doctor_checks_startup_wait_does_not_probe_a_ready_service_again(self) -> None:
        # Samba is ready at once; only NBNS is still starting, so only it is
        # probed each round. Samba's check reports the wait's first probe.
        smbd_mock = mock.Mock(return_value=self._smbd_probe(True))
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        mdns_mock = mock.Mock(side_effect=[starting] * 4 + [self._nbns_ready_probe()])

        run, sleeps = self._run_doctor_nbns(
            mdns_mock,
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=42.0, smbd_mock=smbd_mock,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10] * 4)
        smbd_mock.assert_called_once()
        self.assertEqual(mdns_mock.call_count, 5)

    def test_run_doctor_checks_reports_samba_that_never_starts_after_the_window(self) -> None:
        smbd_mock = mock.Mock(return_value=self._smbd_probe(False))
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=100.0, smbd_mock=smbd_mock, debug_fields=debug_fields,
        )

        self.assertEqual(sleeps[:8], [10] * 8)
        self.assertEqual(smbd_mock.call_count, 9)  # The wait's rounds; no retries after it.
        self.assertTrue(run.fatal)
        self.assertIn("managed smbd parent process is not running",
                      [result.message for result in run.results if result.status == "FAIL"])
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "outlasted")

    def test_run_doctor_checks_names_every_service_it_waits_for(self) -> None:
        nbns = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        bonjour = ReadinessProbeResult(
            ready=False, detail=MDNS_REGISTRATIONS_STILL_STARTING,
            steps=(ProbeStepResult("mdns_registered", "fail", MDNS_REGISTRATIONS_STILL_STARTING),),
        )
        cases = (
            (self._smbd_probe(False), nbns, "Samba and NetBIOS"),
            (self._smbd_probe(True), bonjour, "Bonjour"),
        )
        for smbd, mdns, named in cases:
            with self.subTest(named):
                stages: list[tuple[str, str | None]] = []
                self._run_doctor_nbns(
                    mock.Mock(side_effect=[mdns, self._nbns_ready_probe()]),
                    mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
                    startup_age=42.0, smbd_mock=mock.Mock(side_effect=[smbd, self._smbd_probe(True)]),
                    on_stage=lambda *stage: stages.append(stage),
                )
                self.assertEqual(stages[0][1], f"device services started 42s ago; waiting up to 138s for {named} to finish starting...")

    def test_run_doctor_checks_startup_wait_ends_at_a_verdict_that_turns_samba_off(self) -> None:
        # The first staging run after a boot finds smbd damaged on the disk
        # while doctor waits for Samba: waiting cannot help, so doctor stops
        # and says to reinstall, as it would have had the verdict come first.
        smbd_mock = mock.Mock(side_effect=lambda *a, **k: self._smbd_not_staged_probe())
        damaged = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "payload=damaged")
        reads: list[int] = []

        def verdicts(*args, **kwargs):
            reads.append(1)
            return (damaged,) if len(reads) > 1 else ()

        debug_fields: dict[str, object] = {}
        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=smbd_mock, debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems": verdicts},
        )
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "verdict")
        failure = run.results[-1]
        self.assertEqual((failure.status, failure.details.get("code")), ("FAIL", damaged.code))
        self.assertTrue(run.fatal)

    def test_run_doctor_checks_keeps_the_verdict_that_ended_its_wait(self) -> None:
        # The wait stops at payload=damaged; the read after it fails. The
        # verdict the wait saw stands, so doctor still says to reinstall.
        damaged = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "payload=damaged")
        reads = iter([(damaged,), None])
        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=mock.Mock(side_effect=lambda *a, **k: self._smbd_not_staged_probe()),
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems":
                           mock.Mock(side_effect=lambda *a, **k: next(reads))},
        )
        failure = run.results[-1]
        self.assertEqual((failure.status, failure.details.get("code")), ("FAIL", damaged.code))
        self.assertTrue(run.fatal)

    def _run_doctor_on_a_young_manager_that_could_not_read_smbd(self, later_verdicts, smbd_mock, debug_fields):
        unreadable = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "payload=unreadable")
        return unreadable, self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=smbd_mock, debug_fields=debug_fields,
            startup_age_mock=mock.Mock(return_value=ManagerStartupAgeProbeResult(
                8.0, "manager started 8s ago", payload_problems=(unreadable,))),
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems":
                           mock.Mock(return_value=later_verdicts(unreadable))},
        )

    def test_run_doctor_checks_waits_for_a_young_manager_to_read_smbd_again(self) -> None:
        # One slow read at a cold start: the manager says payload=unreadable,
        # reads smbd again seconds later and starts Samba. Doctor waits
        # through the verdict instead of telling the user to replace the disk.
        probes = iter([self._smbd_not_staged_probe()])
        smbd = mock.Mock(side_effect=lambda *a, **k: next(probes, self._smbd_probe(True)))
        debug_fields: dict[str, object] = {}
        unreadable, (run, sleeps) = self._run_doctor_on_a_young_manager_that_could_not_read_smbd(
            lambda unreadable: (), smbd, debug_fields)
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "ready")
        self.assertEqual(len(sleeps), 1)
        self.assertNotIn(unreadable.code, [result.details.get("code") for result in run.results])
        self.assertFalse(run.fatal)

    def test_run_doctor_checks_reports_files_the_disk_could_not_read_after_the_window(self) -> None:
        # The verdict holds through the whole startup window: the disk is
        # failing, and doctor says so once the manager is past it.
        smbd = mock.Mock(side_effect=lambda *a, **k: self._smbd_not_staged_probe())
        debug_fields: dict[str, object] = {}
        unreadable, (run, sleeps) = self._run_doctor_on_a_young_manager_that_could_not_read_smbd(
            lambda unreadable: (unreadable,), smbd, debug_fields)
        self.assertAlmostEqual(sum(sleeps), DOCTOR_STARTUP_WINDOW_SECONDS - 8.0)
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "outlasted")
        failure = run.results[-1]
        self.assertEqual((failure.status, failure.details.get("code")), ("FAIL", unreadable.code))
        self.assertEqual(sum(result.status == "FAIL" for result in run.results), 1)
        self.assertTrue(run.fatal)

    @staticmethod
    def _rsync_probe(ready: bool) -> ReadinessProbeResult:
        if ready:
            return ReadinessProbeResult(ready=True, detail="managed rsync is ready", steps=(
                ProbeStepResult("rsync_process", "pass", "managed rsync process is running"),))
        return ReadinessProbeResult(ready=False, detail="managed rsync process is not running", steps=(
            ProbeStepResult("rsync_process", "fail", "managed rsync process is not running"),))

    def test_run_doctor_checks_reads_rsync_damage_found_while_it_waited(self) -> None:
        # rsync was still starting when the wait began; its first run found
        # its disk copy damaged meanwhile. The verdict ends the wait for rsync
        # and is what the rsync check reports, not a process not running.
        problem = next(p for p in DISK_PAYLOAD_PROBLEMS if p.token == "rsync=damaged")
        rsync = mock.Mock(return_value=self._rsync_probe(False))
        debug_fields: dict[str, object] = {}
        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=mock.Mock(return_value=self._smbd_probe(True)), debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems": mock.Mock(return_value=(problem,)),
                           "timecapsulesmb.checks.doctor_steps.probe_managed_rsync_conn": rsync},
        )
        failures = [(result.message, result.details.get("code")) for result in run.results if result.status == "FAIL"]
        self.assertEqual(failures, [(problem.message, problem.code)])
        # The wait ended at a verdict, as at Samba's: rsync is not ready.
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "verdict")
        self.assertEqual(len(sleeps), 1)
        rsync.assert_called_once()

    def test_run_doctor_checks_waits_for_rsync_as_for_samba(self) -> None:
        # Right after a boot rsync starts beside Samba: doctor waits for it,
        # names it, and reports the rsync it found once it runs.
        stages: list[tuple[str, str | None]] = []
        rsync = mock.Mock(side_effect=[self._rsync_probe(False), self._rsync_probe(False), self._rsync_probe(True)])
        debug_fields: dict[str, object] = {}
        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=mock.Mock(return_value=self._smbd_probe(True)), debug_fields=debug_fields,
            on_stage=lambda *stage: stages.append(stage),
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems": mock.Mock(return_value=()),
                           "timecapsulesmb.checks.doctor_steps.probe_managed_rsync_conn": rsync},
        )
        self.assertEqual(len(sleeps), 2)
        self.assertIn("for rsync to finish starting", stages[0][1])
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "ready")
        self.assertEqual(rsync.call_count, 3)
        self.assertNotIn("managed rsync process is not running",
                         [result.message for result in run.results if result.status == "FAIL"])

    def test_run_doctor_checks_keeps_its_verdict_when_the_re_read_fails(self) -> None:
        # ps failed after the startup wait: that says nothing about rsync, so
        # the verdict read before the wait stands.
        problem = next(p for p in DISK_PAYLOAD_PROBLEMS if not p.samba_off)
        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=mock.Mock(return_value=self._smbd_probe(True)),
            startup_age_mock=mock.Mock(return_value=ManagerStartupAgeProbeResult(
                8.0, "manager started 8s ago", payload_problems=(problem,))),
            extra_patches={"timecapsulesmb.checks.doctor_steps.manager_payload_problems": mock.Mock(return_value=None),
                           "timecapsulesmb.checks.doctor_steps.probe_managed_rsync_conn":
                               mock.Mock(side_effect=AssertionError("doctor probed rsync"))},
        )
        codes = [result.details.get("code") for result in run.results if result.status == "FAIL"]
        self.assertIn(problem.code, codes)

    def test_run_doctor_checks_startup_wait_stops_on_a_structural_samba_failure(self) -> None:
        # NBNS still starting is not worth waiting for when Samba is broken.
        smbd_mock = mock.Mock(return_value=self._smbd_probe(False, "native service binary missing at /mnt/Flash/service"))
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=starting),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=42.0, smbd_mock=smbd_mock, debug_fields=debug_fields,
        )

        self.assertEqual(sleeps, [])
        smbd_mock.assert_called_once()
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "structural")
        self.assertTrue(run.fatal)

    @staticmethod
    def _smbd_not_staged_probe() -> mock.Mock:
        # What the probe reports after a boot before the manager first stages
        # Samba: the RAM runtime is empty and the data disk is not mounted yet.
        failures = (
            "managed runtime smbd binary missing",
            "managed runtime smb.conf missing",
            "active smb.conf passdb backend is not staged in RAM",
            "active smb.conf username map is not staged in RAM",
            "active smb.conf xattr_tdb:file is not persistent disk storage",
            "one or more managed share volumes are not mounted",
            "manager is not running for managed runtime",
            "managed smbd parent process is not running",
            "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
            "device Samba version unavailable (managed runtime smbd binary missing)",
        )
        return mock.Mock(ready=False, detail="; ".join(failures), lines=tuple(f"FAIL:{f}" for f in failures), steps=())

    def test_run_doctor_checks_waits_for_samba_staging_after_a_boot(self) -> None:
        # A boot empties the RAM runtime; the manager stages it once the disk
        # mounts and the hostname is set, then starts smbd. All of that is
        # waited for, and nothing seen before it is reported.
        timed_out = mock.Mock(ready=False, detail="managed smbd readiness probe timed out",
                              lines=("FAIL:managed smbd readiness probe timed out",), steps=())
        smbd_mock = mock.Mock(side_effect=[
            self._smbd_not_staged_probe(), timed_out, self._smbd_not_staged_probe(),
            self._smbd_probe(False), self._smbd_probe(True),
        ])
        stages: list[tuple[str, str | None]] = []
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=8.0, smbd_mock=smbd_mock, on_stage=lambda *stage: stages.append(stage),
            debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10] * 4)
        self.assertEqual(smbd_mock.call_count, 5)
        self.assertEqual(stages[0], ("wait_for_device_startup",
                                     "device services started 8s ago; waiting up to 172s for Samba to finish starting..."))
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "ready")
        self.assertEqual([result for result in run.results if result.status == "FAIL"], [])

    def test_run_doctor_checks_reports_samba_never_staged_in_the_window_as_itself(self) -> None:
        # A disk that never mounts keeps Samba unstaged: once the window ends,
        # each missing piece is its own failure.
        smbd_mock = mock.Mock(return_value=self._smbd_not_staged_probe())
        debug_fields: dict[str, object] = {}

        # An unmounted share volume is a disk failure, so its MaSt is read too.
        with mock.patch("timecapsulesmb.checks.doctor_debug.probe_mast_diagnostics_conn",
                        return_value=self.mast_probe_diagnostics()) as mast:
            run, sleeps = self._run_doctor_nbns(
                mock.Mock(return_value=self._nbns_ready_probe()),
                mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
                startup_age=150.0, smbd_mock=smbd_mock, debug_fields=debug_fields,
            )

        mast.assert_called_once()

        self.assertEqual(sleeps, [10, 10, 10])
        self.assertEqual(smbd_mock.call_count, 4)
        self.assertTrue(run.fatal)
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "outlasted")
        failures = {result.message for result in run.results if result.status == "FAIL"}
        self.assertLessEqual({"managed runtime smb.conf missing", "one or more managed share volumes are not mounted"}, failures)

    def test_run_doctor_checks_does_not_retry_unstaged_samba_after_the_window(self) -> None:
        # Outside the window, an unstaged runtime is not a smbd still binding
        # its listener: it is reported at once, without the usual retries.
        smbd_mock = mock.Mock(return_value=self._smbd_not_staged_probe())

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=400.0, smbd_mock=smbd_mock,
        )

        self.assertEqual(sleeps, [])
        smbd_mock.assert_called_once()
        self.assertTrue(run.fatal)
        self.assertIn("managed runtime smb.conf missing",
                      [result.message for result in run.results if result.status == "FAIL"])

    def test_run_doctor_checks_reports_failures_seen_after_the_window_as_themselves(self) -> None:
        # After the window, a hostname still not mapped is the device's real state.
        unmapped = mock.Mock(return_value=DeviceHostnameProbeResult("timecapsule", ()))
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=starting),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=100.0,
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_device_hostname_conn": unmapped},
        )

        failures = {result.details.get("code") or result.message for result in run.results if result.status == "FAIL"}
        self.assertEqual(failures, {DOCTOR_CODE_HOSTNAME_UNMAPPED, "discovery native NBNS is still starting"})

    def test_run_doctor_checks_reports_failures_inside_the_window_when_the_wait_ends_early(self) -> None:
        # Everything the wait polls was ready at once. Samba starts only once
        # the hostname is mapped, so an unmapped one is a real failure even
        # though the window is still open.
        unmapped = mock.Mock(return_value=DeviceHostnameProbeResult("timecapsule", ()))

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=42.0,
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_device_hostname_conn": unmapped},
        )

        self.assertEqual(sleeps, [])
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.details.get("code") for result in failures], [DOCTOR_CODE_HOSTNAME_UNMAPPED])

    def test_run_doctor_checks_startup_window_ends_on_time_with_slow_probes(self) -> None:
        # Each probe takes 7 s of an 80 s window: the last sleep is cut to the
        # deadline, one last probe runs there, and nothing waits after it.
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        mdns_mock = mock.Mock(return_value=not_ready)

        run, sleeps = self._run_doctor_nbns(
            mdns_mock,
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=100.0, probe_seconds=7.0,
        )

        self.assertEqual(sleeps, [10, 10, 10, 10, 5])
        self.assertEqual(run.clock.mdns_at[-1], 1080.0)
        self.assertEqual(mdns_mock.call_count, 6)

    def test_run_doctor_checks_startup_window_replaces_apples_budget_for_still_starting(self) -> None:
        # The window already outlasts Apple's 100 s registration budget, so no
        # 20/25/30 s retries follow it; NBNS still starting at the end of it is
        # a failure of its own.
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=starting),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=41.0, debug_fields=debug_fields,
        )

        self.assertEqual(sleeps, [10] * 13 + [9])
        self.assertTrue(run.fatal)
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.message for result in failures], ["discovery native NBNS is still starting"])
        self.assertEqual(debug_fields["startup_wait"], {
            "manager_age_sec": 41, "window_left_sec": 139.0, "waited_sec": 139.0, "probes": 15, "outcome": "outlasted",
        })

    def test_run_doctor_checks_waits_out_a_window_shorter_than_a_poll(self) -> None:
        # Any time left in the window is waited out; only after it do the
        # checks' own retries run.
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        cases = (
            # age: (sleeps, probes, startup_wait)
            (170.0, [10], 2, {"manager_age_sec": 170, "window_left_sec": 10.0, "waited_sec": 10.0, "probes": 2, "outcome": "outlasted"}),
            (175.0, [5], 2, {"manager_age_sec": 175, "window_left_sec": 5.0, "waited_sec": 5.0, "probes": 2, "outcome": "outlasted"}),
            (180.0, [10, 15], 3, {"outcome": "no_window"}),
            (400.0, [10, 15], 3, {"outcome": "no_window"}),
        )
        for startup_age, expected_sleeps, probes, startup_wait in cases:
            with self.subTest(startup_age=startup_age):
                mdns_mock = mock.Mock(return_value=not_ready)
                debug_fields: dict[str, object] = {}

                run, sleeps = self._run_doctor_nbns(
                    mdns_mock,
                    mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
                    startup_age=startup_age, debug_fields=debug_fields,
                )

                self.assertEqual(sleeps, expected_sleeps)
                self.assertEqual(mdns_mock.call_count, probes)
                self.assertEqual(debug_fields["startup_wait"], startup_wait)
                failures = [result for result in run.results if result.status == "FAIL"]
                self.assertEqual([result.message for result in failures], ["discovery native NBNS is not ready"])

    def test_run_doctor_checks_waits_up_to_apple_budget_while_native_nbns_starts(self) -> None:
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        mdns_mock = mock.Mock(side_effect=[starting, starting, starting, ready])
        nbns_mock = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

        run, sleeps = self._run_doctor_nbns(mdns_mock, nbns_mock)

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10, 15, 20])
        self.assertEqual(mdns_mock.call_count, 4)
        nbns_mock.assert_called_once()

    def test_run_doctor_checks_fails_after_apple_budget_when_native_nbns_never_finishes_starting(self) -> None:
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        mdns_mock = mock.Mock(return_value=starting)
        nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout()] * 3)

        run, sleeps = self._run_doctor_nbns(mdns_mock, nbns_mock)

        self.assertTrue(run.fatal)
        # 100 s for readiness, then the unregistered name's own two retries.
        self.assertEqual(sleeps, [10, 15, 20, 25, 30, 10, 15])
        self.assertEqual(mdns_mock.call_count, 6)
        self.assertTrue(any(result.status == "FAIL" and result.message == "discovery native NBNS is still starting"
                            for result in run.results))

    def test_run_doctor_checks_keeps_short_retries_when_native_nbns_is_not_ready(self) -> None:
        # "Not ready" is discovery backing off after a failed registration;
        # only "still starting" earns Apple's longer registration budget.
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        mdns_mock = mock.Mock(return_value=not_ready)
        nbns_mock = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

        run, sleeps = self._run_doctor_nbns(mdns_mock, nbns_mock)

        self.assertTrue(run.fatal)
        self.assertEqual(sleeps, [10, 15])
        self.assertEqual(mdns_mock.call_count, 3)

    def test_run_doctor_checks_reports_query_timeout_at_once_when_native_nbns_is_ready(self) -> None:
        # Native NBNS registered its name, so a timeout is between this host
        # and the device; waiting cannot fix it, even right after a boot.
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        nbns_mock = mock.Mock(return_value=self._nbns_query_timeout())

        run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=ready), nbns_mock, startup_age=41.0)

        self.assertTrue(run.fatal)
        self.assertEqual(sleeps, [])
        nbns_mock.assert_called_once()
        query = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(query.status, "FAIL")

    def test_run_doctor_checks_retries_query_while_native_nbns_is_not_ready(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        resolved = CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")
        nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout(), resolved])

        run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=not_ready), nbns_mock)

        self.assertEqual(sleeps, [10, 15, 10])
        self.assertEqual(nbns_mock.call_count, 2)
        self.assertIn(resolved, run.results)

    def test_run_doctor_checks_waits_out_startup_then_reports_unregistered_nbns_as_itself(self) -> None:
        # Doctor polls through the startup window; NBNS still unregistered
        # after it is a real failure.
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        # 41 s old: polled every 10 s until the window's 139 s are spent. Older:
        # the usual two retries. The query's own two retries follow either way.
        for startup_age, mdns_sleeps in ((41.0, [10] * 13 + [9]), (3600.0, [10, 15])):
            with self.subTest(startup_age=startup_age):
                nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout()] * 3)

                run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=not_ready), nbns_mock, startup_age=startup_age)

                self.assertEqual(sleeps, mdns_sleeps + [10, 15])
                self.assertTrue(run.fatal)
                self.assertEqual(nbns_mock.call_count, 3)
                failures = [result for result in run.results if result.status == "FAIL"]
                self.assertEqual({result.message for result in failures}, {"discovery native NBNS is not ready"})
                # The device reported NBNS not ready, so this computer's query
                # timing out is not counted against it.
                query = next(result for result in run.results if result.details.get("code") == NBNS_QUERY_TIMEOUT_CODE)
                self.assertEqual(query.status, "SKIP")

    @staticmethod
    def _device_startup_age(output: str) -> mock.Mock:
        # The real startup-age probe, reading this output from the device.
        def probe_device(connection):
            proc = subprocess.CompletedProcess(["ssh"], 0, stdout=output)
            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=proc):
                return probe_manager_startup_age_conn(connection)
        return mock.Mock(side_effect=probe_device)

    def test_run_doctor_checks_startup_window_survives_a_wall_clock_step_after_boot(self) -> None:
        # v3.3.0 field case: 55 s after boot, with the clock stepped a day by
        # sntpd, ps said the manager was 86577 s old; the startup window was
        # skipped and native NBNS still starting failed doctor. The monotonic
        # start keeps doctor waiting through the window. A manager that is old
        # on that clock, or that has no start (an older release), gets only
        # the usual retries.
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        # This computer's query timing out is the device's NBNS, not counted as a failure here.
        nbns_failures = {"discovery native NBNS is not ready"}
        cases = (
            ("stepped", "role=manager started=5", "60000", [10] * 12 + [5], 55.0),
            ("old", "role=manager started=5", "4000000", [10, 15], 3995.0),
            ("older release", "role=manager", "60000", [10, 15], None),
        )
        for name, title, now_ms, mdns_sleeps, age in cases:
            with self.subTest(name):
                debug_fields: dict[str, object] = {}
                output = f"ps=  142 1 142 S 0 - 1-00:02:57 service service: {title}\nnow_ms={now_ms}\n"
                run, sleeps = self._run_doctor_nbns(
                    mock.Mock(return_value=not_ready),
                    mock.Mock(side_effect=[self._nbns_query_timeout()] * 3),
                    debug_fields=debug_fields,
                    startup_age_mock=self._device_startup_age(output),
                )

                self.assertEqual(sleeps[:len(mdns_sleeps)], mdns_sleeps)
                self.assertTrue(run.fatal)
                failures = [result for result in run.results if result.status == "FAIL"]
                self.assertEqual({result.message for result in failures}, nbns_failures)
                startup = debug_fields["manager_startup_age"]
                self.assertEqual(startup["seconds_ago"], age)
                # The clock is read whether or not the title has a start.
                self.assertEqual(startup["now_monotonic_ms"], int(now_ms))
                self.assertEqual(startup["started_monotonic_s"], 5 if age is not None else None)

    def test_run_doctor_checks_reports_query_timeout_at_once_when_native_nbns_was_not_probed(self) -> None:
        nbns_mock = mock.Mock(return_value=self._nbns_query_timeout())

        run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=mock.Mock(ready=True, detail="ok")), nbns_mock)

        self.assertEqual(sleeps, [])
        nbns_mock.assert_called_once()

    # The field case: a Mac on 192.168.24.0/24 querying a device on 192.168.28.0/24.
    OFF_SUBNET_DEVICE_PROBE = DeviceIpv4SubnetsProbeResult(
        (DeviceIpv4Entry("bridge0", "192.168.28.229", "255.255.255.0", "192.168.28.255"),)
    )

    def _nbns_ready_probe(self) -> ReadinessProbeResult:
        return self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")

    def test_run_doctor_checks_skips_nbns_timeout_from_off_subnet_client(self) -> None:
        debug_fields: dict[str, object] = {}
        device_probe = mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE)
        socket_debug = mock.Mock(return_value="sockets")

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_query_timeout()),
            client_source="192.168.24.102",
            device_probe_mock=device_probe,
            socket_debug_mock=socket_debug,
            debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [])
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        self.assertEqual(
            nbns_result.message,
            "NBNS query for 'TimeCapsule' got no answer; this computer (192.168.24.102) "
            "is outside the device's subnet 192.168.28.0/24",
        )
        self.assertEqual(nbns_result.details["code"], NBNS_OFF_SUBNET_CODE)
        self.assertEqual(nbns_result.details["device_subnets"], ["192.168.28.0/24"])
        device_probe.assert_called_once()
        self.assertEqual(debug_fields["nbns_subnet"], {
            "client_source": "192.168.24.102",
            "device_subnets": ["192.168.28.0/24"],
            "result": "timeout",
            "outcome": "off_subnet",
            "detail": None,
        })
        # A skipped check is not a failure, so no socket diagnostics are read.
        socket_debug.assert_not_called()
        self.assertNotIn("remote_service_sockets", debug_fields)

    @staticmethod
    def _nbns_negative_response() -> CheckResult:
        return CheckResult("FAIL", "NBNS query for 'TimeCapsule' returned a negative response (rcode 3)",
                           {"code": NBNS_NEGATIVE_RESPONSE_CODE, "rcode": 3})

    def test_run_doctor_checks_skips_nbns_refusal_from_off_subnet_client(self) -> None:
        debug_fields: dict[str, object] = {}

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_negative_response()),
            client_source="192.168.24.102",
            device_probe_mock=mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE),
            debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        self.assertEqual(
            nbns_result.message,
            "NBNS query for 'TimeCapsule' was refused (rcode 3); this computer (192.168.24.102) "
            "is outside the device's subnet 192.168.28.0/24",
        )
        self.assertEqual(nbns_result.details["result"], "negative_response")
        self.assertEqual(debug_fields["nbns_subnet"]["result"], "negative_response")
        self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "off_subnet")

    def test_run_doctor_checks_fails_nbns_refusal_from_same_subnet_client(self) -> None:
        debug_fields: dict[str, object] = {}

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_negative_response()),
            debug_fields=debug_fields,
        )

        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "FAIL")
        self.assertEqual(nbns_result.message, "NBNS query for 'TimeCapsule' returned a negative response (rcode 3)")
        self.assertEqual(debug_fields["nbns_subnet"]["result"], "negative_response")
        self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "on_subnet")

    def test_run_doctor_checks_does_not_retry_an_nbns_refusal_during_startup(self) -> None:
        # The startup retry waits for a name to be registered; a refusal is an answer.
        starting = self._native_nbns_probe("fail", "discovery native NBNS is still starting")
        nbns = mock.Mock(return_value=self._nbns_negative_response())

        self._run_doctor_nbns(
            mock.Mock(return_value=starting),
            nbns,
            client_source="192.168.24.102",
            device_probe_mock=mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE),
        )

        nbns.assert_called_once()

    def test_run_doctor_checks_lists_every_device_subnet_when_client_is_off_all_of_them(self) -> None:
        router = DeviceIpv4SubnetsProbeResult((
            DeviceIpv4Entry("bcmeth1", "203.0.113.7", "255.255.255.0", "203.0.113.255"),
            DeviceIpv4Entry("bridge0", "10.0.1.1", "255.255.255.0", "10.0.1.255"),
        ))

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_query_timeout()),
            client_source="192.168.24.102",
            device_probe_mock=mock.Mock(return_value=router),
        )

        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        self.assertTrue(nbns_result.message.endswith("outside the device's subnets 203.0.113.0/24, 10.0.1.0/24"))

    def test_run_doctor_checks_fails_nbns_timeout_from_same_subnet_client(self) -> None:
        debug_fields: dict[str, object] = {}
        device_probe = mock.Mock(return_value=SAME_SUBNET_DEVICE_PROBE)
        socket_debug = mock.Mock(return_value="sockets")

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_query_timeout()),
            device_probe_mock=device_probe,
            socket_debug_mock=socket_debug,
            debug_fields=debug_fields,
        )

        self.assertTrue(run.fatal)
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "FAIL")
        self.assertEqual(nbns_result.details["code"], NBNS_QUERY_TIMEOUT_CODE)
        device_probe.assert_called_once()
        self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "on_subnet")
        self.assertEqual(debug_fields["nbns_subnet"]["client_source"], "10.0.0.50")
        socket_debug.assert_called_once()
        self.assertEqual(debug_fields["remote_service_sockets"], "sockets")

    def test_run_doctor_checks_fails_nbns_timeout_when_device_subnets_are_unknown(self) -> None:
        for probe in (
            mock.Mock(return_value=DeviceIpv4SubnetsProbeResult(error="ifconfig timed out")),
            mock.Mock(side_effect=SshError("connection reset")),
        ):
            with self.subTest(probe=probe):
                debug_fields: dict[str, object] = {}

                run, _ = self._run_doctor_nbns(
                    mock.Mock(return_value=self._nbns_ready_probe()),
                    mock.Mock(return_value=self._nbns_query_timeout()),
                    client_source="192.168.24.102",
                    device_probe_mock=probe,
                    debug_fields=debug_fields,
                )

                self.assertTrue(run.fatal)
                nbns_result = next(result for result in run.results if "NBNS query" in result.message)
                self.assertEqual(nbns_result.status, "FAIL")
                self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "unknown")
                self.assertTrue(debug_fields["nbns_subnet"]["detail"])

    def test_run_doctor_checks_fails_nbns_timeout_without_a_client_source(self) -> None:
        debug_fields: dict[str, object] = {}
        device_probe = mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE)

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=self._nbns_query_timeout()),
            client_source=None,
            device_probe_mock=device_probe,
            debug_fields=debug_fields,
        )

        self.assertTrue(run.fatal)
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "FAIL")
        # ifconfig ran once, for device_ipv4; without a source address it
        # still cannot place this computer on or off the device's subnet.
        device_probe.assert_called_once()
        self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "unknown")

    def test_run_doctor_checks_ignores_a_route_without_a_source_address(self) -> None:
        debug_fields: dict[str, object] = {}
        device_probe = mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep"):
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                xattr_result=CheckResult("PASS", "xattr ok"),
                read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
                skip_bonjour=True,
                skip_smb=True,
                debug_fields=debug_fields,
                extra_patches={
                    "timecapsulesmb.checks.doctor_steps.select_route_to_address": mock.Mock(
                        return_value=RouteSelection("available", source=None)
                    ),
                    "timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mock.Mock(
                        return_value=self._nbns_ready_probe()
                    ),
                    "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": mock.Mock(
                        return_value=self._nbns_query_timeout()
                    ),
                    "timecapsulesmb.checks.doctor_steps.probe_device_networks_conn": device_networks_probe(device_probe),
                    "timecapsulesmb.checks.doctor_debug.read_remote_service_socket_diagnostics_conn": mock.Mock(return_value=""),
                },
            )

        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "FAIL")
        # ifconfig ran once, for device_ipv4; without a source address it
        # still cannot place this computer on or off the device's subnet.
        device_probe.assert_called_once()
        self.assertEqual(debug_fields["nbns_subnet"]["outcome"], "unknown")
        self.assertEqual(debug_fields["smb_connectivity"]["routes"]["10.0.0.2"]["state"], "available")

    def test_run_doctor_checks_does_not_probe_device_subnets_unless_nbns_timed_out(self) -> None:
        cases = (
            (CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"), "PASS", False),
            (CheckResult("FAIL", "NBNS query for 'TimeCapsule' resolved to 10.0.0.9, expected 10.0.0.2"), "FAIL", True),
        )
        for query_result, status, sockets_read in cases:
            with self.subTest(status=status):
                debug_fields: dict[str, object] = {}
                device_probe = mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE)
                socket_debug = mock.Mock(return_value="sockets")

                run, _ = self._run_doctor_nbns(
                    mock.Mock(return_value=self._nbns_ready_probe()),
                    mock.Mock(return_value=query_result),
                    client_source="192.168.24.102",
                    device_probe_mock=device_probe,
                    socket_debug_mock=socket_debug,
                    debug_fields=debug_fields,
                )

                nbns_result = next(result for result in run.results if "NBNS query" in result.message)
                self.assertEqual(nbns_result.status, status)
                # ifconfig runs once for telemetry's device_ipv4; only a
                # timeout turns it into a subnet verdict.
                device_probe.assert_called_once()
                self.assertIn("ifconfig", debug_fields["device_ipv4"])
                self.assertNotIn("nbns_subnet", debug_fields)
                self.assertEqual(socket_debug.called, sockets_read)

    def test_run_doctor_checks_records_device_ipv4_kinds_from_ifconfig_and_the_plan(self) -> None:
        debug_fields: dict[str, object] = {}
        ifconfig = DeviceNetworksProbeResult(
            SAME_SUBNET_DEVICE_PROBE.entries,
            ("10.0.0.0/24",),
            interfaces=(DeviceInterfaceIpv4("bridge0", "0xffffe043", ("private", "link_local")),
                        DeviceInterfaceIpv4("mgi1", "0xe843", ("public",))),
        )
        ready = self._nbns_ready_probe()
        mdns = ReadinessProbeResult(ready.ready, ready.detail, ready.steps,
                                    link_plan=_parse_link_plan(router_plan(wan_ipv4="203.0.113.7")))

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=mdns),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            device_probe_mock=mock.Mock(return_value=ifconfig),
            debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(debug_fields["device_ipv4"], {
            "plan": {"bridge0": ["private", "link_local"]},
            "ifconfig": {
                "bridge0": {"flags": "0xffffe043", "kinds": ["private", "link_local"]},
                "mgi1": {"flags": "0xe843", "kinds": ["public"]},
            },
        })
        serialized = json.dumps(debug_fields["device_ipv4"])
        for address in ("10.0.1.1", "169.254", "203.0.113", "10.0.0.2"):
            self.assertNotIn(address, serialized)

    def test_run_doctor_checks_records_the_ifconfig_error_in_device_ipv4(self) -> None:
        debug_fields: dict[str, object] = {}

        self._run_doctor_nbns(
            mock.Mock(return_value=self._nbns_ready_probe()),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            device_probe_mock=mock.Mock(return_value=DeviceNetworksProbeResult(error="ifconfig timed out")),
            debug_fields=debug_fields,
        )

        # The readiness probe here carries no link plan, so only ifconfig reports.
        self.assertEqual(debug_fields["device_ipv4"], {"ifconfig": {"error": "ifconfig timed out"}})

    def test_run_doctor_checks_skips_nbns_on_a_device_whose_wcifsnd_has_no_address(self) -> None:
        # A Mac cabled to a Time Capsule with no DHCP server:
        # bridge0 holds 0.0.0.0 beside 169.254, so discovery runs no wcifsnd.
        debug_fields: dict[str, object] = {}
        no_lease_plan = router_plan().replace("mode=nat", "mode=bridge").replace(
            "addr: link=9 family=inet addr=10.0.1.1 prefix=24", "addr: link=9 family=inet addr=0.0.0.0 prefix=0")
        mdns = ReadinessProbeResult(
            ready=True,
            detail="managed mDNS registrant active",
            steps=(
                ProbeStepResult("mdns_process", "pass", "discovery process is running"),
                ProbeStepResult("native_nbns", "pass", NATIVE_NBNS_OFF_DETAIL),
            ),
            link_plan=_parse_link_plan(no_lease_plan),
            nbns_state="none",
        )
        ifconfig = DeviceNetworksProbeResult(
            interfaces=(DeviceInterfaceIpv4("bridge0", "0xffffe043", ("zero", "link_local")),),
        )
        nbns = mock.Mock(return_value=self._nbns_query_timeout())

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=mdns), nbns, device_probe_mock=mock.Mock(return_value=ifconfig),
            debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal, [(r.status, r.message) for r in run.results if r.status == "FAIL"])
        nbns.assert_not_called()
        native = next(result for result in run.results if result.message == NATIVE_NBNS_OFF_DETAIL)
        self.assertEqual(native.status, "PASS")
        skip = next(result for result in run.results if result.message.startswith("NBNS check skipped"))
        self.assertEqual((skip.status, skip.details.get("code")), ("SKIP", NBNS_NOT_SERVED_CODE))
        self.assertEqual(debug_fields["device_ipv4"], {
            "plan": {"bridge0": ["zero", "link_local"]},
            "native_nbns": "none",
            "ifconfig": {"bridge0": {"flags": "0xffffe043", "kinds": ["zero", "link_local"]}},
        })

    def test_run_doctor_checks_queries_nbns_only_when_the_device_registers_a_name(self) -> None:
        # A passing native check that registers nothing (diskless, or a plan
        # that gives NBNS no link yet) skips the query; only "ready" asks.
        cases = (
            ("disabled", "native NBNS is disabled", False),
            ("waiting", "native NBNS is waiting", False),
            ("none", NATIVE_NBNS_OFF_DETAIL, False),
            ("ready", "Apple wcifsnd is ready on UDP 137 and 138", True),
        )
        for state, detail, queried in cases:
            with self.subTest(state=state):
                ready = self._native_nbns_probe("pass", detail)
                mdns = ReadinessProbeResult(ready.ready, ready.detail, ready.steps, nbns_state=state)
                nbns = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

                run, _ = self._run_doctor_nbns(mock.Mock(return_value=mdns), nbns)

                self.assertFalse(run.fatal)
                self.assertEqual(nbns.called, queried)
                if not queried:
                    skip = next(result for result in run.results if result.message.startswith("NBNS check skipped"))
                    self.assertEqual((skip.status, skip.details.get("code")), ("SKIP", NBNS_NOT_SERVED_CODE))

    def test_run_doctor_checks_waits_for_the_manager_to_stop_a_foreign_wcifsnd(self) -> None:
        foreign = self._native_nbns_probe("fail", FOREIGN_WCIFSND_RUNNING)
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        nbns = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

        # The manager stops it within seconds; one that ignores SIGTERM lasts
        # until the SIGKILL 10 s later, inside the two short retries.
        run, sleeps = self._run_doctor_nbns(mock.Mock(side_effect=[foreign, foreign, ready]), nbns)

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10, 15])
        self.assertFalse(any(result.message == FOREIGN_WCIFSND_RUNNING for result in run.results))

    def test_run_doctor_checks_waits_for_registration_after_a_foreign_wcifsnd_is_stopped(self) -> None:
        foreign = self._native_nbns_probe("fail", FOREIGN_WCIFSND_RUNNING)
        starting = self._native_nbns_probe("fail", NATIVE_NBNS_STILL_STARTING)
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        nbns = mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2"))

        # Once the other daemon is gone, discovery registers again; that can
        # take its 30 s first-reply wait, so the longer retries still apply.
        run, sleeps = self._run_doctor_nbns(
            mock.Mock(side_effect=[foreign, foreign, starting, starting, ready]), nbns)

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10, 15, 20, 25])

    def test_run_doctor_checks_fails_a_foreign_wcifsnd_the_manager_never_stops(self) -> None:
        foreign = self._native_nbns_probe("fail", FOREIGN_WCIFSND_RUNNING)

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(return_value=foreign),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
        )

        # A foreign wcifsnd that outlives the manager's SIGKILL is not going
        # away: doctor fails after the short retries instead of waiting longer.
        self.assertEqual(sleeps, [10, 15])
        self.assertTrue(run.fatal)
        self.assertEqual([result.message for result in run.results if result.status == "FAIL"], [FOREIGN_WCIFSND_RUNNING])

    def test_run_doctor_checks_startup_wait_polls_while_a_foreign_wcifsnd_runs(self) -> None:
        foreign = self._native_nbns_probe("fail", FOREIGN_WCIFSND_RUNNING)
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        debug_fields: dict[str, object] = {}

        run, sleeps = self._run_doctor_nbns(
            mock.Mock(side_effect=[foreign, foreign, ready]),
            mock.Mock(return_value=CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")),
            startup_age=30.0, debug_fields=debug_fields,
        )

        self.assertFalse(run.fatal)
        self.assertEqual(sleeps, [10, 10])
        self.assertEqual(debug_fields["startup_wait"]["outcome"], "ready")

    def test_run_doctor_checks_skips_off_subnet_timeout_after_native_nbns_retries(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout()] * 3)
        device_probe = mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE)

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=not_ready),
            nbns_mock,
            client_source="192.168.24.102",
            device_probe_mock=device_probe,
        )

        # The query keeps its retries; the device is probed once, after the last.
        self.assertEqual(nbns_mock.call_count, 3)
        device_probe.assert_called_once()
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        # The device-side readiness failure still fails the run.
        self.assertTrue(run.fatal)
        self.assertEqual(
            [result.message for result in run.results if result.status == "FAIL"],
            ["discovery native NBNS is not ready"],
        )

    def test_run_doctor_checks_keeps_off_subnet_skip_out_of_startup_failures(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=not_ready),
            mock.Mock(side_effect=[self._nbns_query_timeout()] * 3),
            startup_age=41.0,
            client_source="192.168.24.102",
            device_probe_mock=mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE),
        )

        # NBNS outlasted the startup window, so it is the only failure; this
        # computer's off-subnet query is neither a failure nor folded into it.
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.message for result in failures], ["discovery native NBNS is not ready"])
        nbns_result = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")

    def test_run_doctor_checks_exhausts_transient_mdns_process_retries(self) -> None:
        mdns_probe = mock.Mock(
            ready=False,
            detail="discovery process is not running",
            lines=("FAIL:discovery process is not running",),
        )
        mdns_mock = mock.Mock(return_value=mdns_probe)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mdns_mock},
            )

        self.assertTrue(run.fatal)
        self.assertEqual(mdns_mock.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [10, 15])
        self.assertTrue(any(result.status == "FAIL" and result.message == "discovery process is not running" for result in run.results))

    def test_run_doctor_checks_does_not_retry_structural_mdns_failure_mixed_with_transient_failure(self) -> None:
        mdns_probe = mock.Mock(
            ready=False,
            detail="native service binary missing at /mnt/Flash/service; discovery process is not running",
            lines=(
                "FAIL:native service binary missing at /mnt/Flash/service",
                "FAIL:discovery process is not running",
            ),
        )
        mdns_mock = mock.Mock(return_value=mdns_probe)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                skip_bonjour=True,
                skip_smb=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mdns_mock},
            )

        self.assertTrue(run.fatal)
        mdns_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_run_doctor_checks_reports_supported_device_compatibility(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
        )
        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "PASS" and "Detected supported device: NetBSD 6.0" in result.message for result in run.results))

    def test_run_doctor_checks_uses_precomputed_probe_state_without_reprobing(self) -> None:
        precomputed = mock.Mock(
            probe_result=mock.Mock(
                ssh_authenticated=True,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="little",
            ),
            compatibility=DeviceCompatibility(
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="little",
                payload_family="netbsd6_samba4",
                device_generation="gen5",
                supported=True,
                reason_code="supported_netbsd6",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            precomputed_probe_state=precomputed,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_connection_state": mock.Mock(
                    side_effect=AssertionError("should not reprobe")
                )
            },
        )
        self.assertFalse(run.fatal)
        self.assertTrue(any("Detected supported device: NetBSD 6.0" in result.message for result in run.results))

    def test_run_doctor_checks_reports_unsupported_device_compatibility(self) -> None:
        probe_state = mock.Mock(
            probe_result=mock.Mock(
                ssh_authenticated=True,
                error=None,
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="unknown",
            ),
            compatibility=DeviceCompatibility(
                os_name="NetBSD",
                os_release="6.0",
                arch="earmv4",
                elf_endianness="unknown",
                payload_family=None,
                device_generation="unknown",
                supported=False,
                reason_code="unsupported_netbsd6_endianness",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_connection_state": mock.Mock(return_value=probe_state)},
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any(result.status == "FAIL" and "unknown-endian" in result.message for result in run.results))

    def test_run_doctor_checks_finds_bonjour_records_by_the_mac_ssh_reached(self) -> None:
        probe_state = ProbedDeviceState(
            probe_result=ProbeResult(ssh_status=SshAccessStatus.OPEN_AUTHENTICATED, error=None, os_name="NetBSD",
                                     os_release="6.0", arch="earmv4", elf_endianness="little",
                                     airport_mac="02:00:00:00:00:10"),
            compatibility=DeviceCompatibility(os_name="NetBSD", os_release="6.0", arch="earmv4",
                                              elf_endianness="little", payload_family="netbsd6_samba4",
                                              device_generation="gen5", supported=True,
                                              reason_code="supported_netbsd6"),
        )
        bonjour = mock.Mock(wraps=_add_bonjour_results)
        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_connection_state": mock.Mock(return_value=probe_state),
                           "timecapsulesmb.checks.doctor._add_bonjour_results": bonjour},
        )
        self.assertEqual(bonjour.call_args.kwargs["airport_mac"], "02:00:00:00:00:10")

    def test_run_doctor_checks_fails_airport_express_on_processor(self) -> None:
        probe_result = ProbeResult(
            ssh_status=SshAccessStatus.OPEN_AUTHENTICATED,
            error=None,
            os_name="NetBSD",
            os_release="4.0_STABLE",
            arch="ar7240",
            elf_endianness="big",
        )
        probe_state = ProbedDeviceState(
            probe_result=probe_result,
            compatibility=compatibility_from_probe_result(probe_result),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            extra_patches={"timecapsulesmb.checks.doctor_steps.probe_connection_state": mock.Mock(return_value=probe_state)},
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any(
            result.status == "FAIL" and "ar7240 processor" in result.message and "AirPort Express" in result.message
            for result in run.results
        ))

    def test_run_doctor_checks_reports_each_result_in_the_section_it_was_measured_in(self) -> None:
        run = self.run_doctor_with_mocks(
            local_tools=[CheckResult("PASS", "found local tool ssh")],
            ssh_login=CheckResult("PASS", "ssh ok"),
            smb_port=CheckResult("PASS", "445 ok"),
            smb_instance=[CheckResult("PASS", "instance ok")],
            smb_file_ops=[CheckResult("PASS", "file ops ok")],
            real_device_smb=True,
            device_reading=acp_reading(True),
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            # A fresh result per listing: the device's and this computer's.
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing":
                    mock.Mock(side_effect=lambda *_args, **_kwargs: self.smb_listing_result()),
            },
        )
        domains = {result.message: result.domain for result in run.results}
        # This computer: its tools and config, its SSH login, the saved
        # password read over ACP, and everything it sees over the network.
        for message in ("found local tool ssh", "ssh ok", "the saved password is the AirPort admin password",
                        "Bonjour IPv4: instance ok", "SMB IPv4 reachable at 10.0.0.2:445"):
            self.assertEqual(domains.get(message), "computer", message)
        # The device: measured over SSH, its own SMB check included.
        self.assertEqual(domains["file ops ok"], "device")
        self.assertTrue(any(result.domain == "device" and "managed smbd" in result.message for result in run.results))
        self.assertTrue(all(result.domain in ("computer", "device") for result in run.results))
        # This computer's own setup, the device, then this computer's view of
        # the device over the network: each section once.
        sections = [result.domain for result in run.results]
        changes = [section for index, section in enumerate(sections) if index == 0 or section != sections[index - 1]]
        self.assertEqual(changes, ["computer", "device", "computer"])

    def test_run_doctor_checks_reports_the_device_as_not_checked_when_ssh_login_fails(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("FAIL", "ssh down", {"code": "ssh_unreachable"}),
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            skip_bonjour=True,
        )
        device_results = [result for result in run.results if result.domain == "device"]
        self.assertEqual(
            [(result.status, result.details.get("code")) for result in device_results],
            [("SKIP", "device_not_checked")],
        )
        run.mocks.device_smb.assert_not_called()
        report = doctor_report(run.results)
        # Nothing ran on the device: not checked, not failed.
        self.assertEqual(report["sections"]["device"]["status"], "not_checked")
        self.assertEqual(report["sections"]["computer"]["fail_codes"], ["ssh_unreachable"])
        self.assertEqual(report["status"], "fail")

    def test_run_doctor_checks_skip_ssh_reports_no_device_section(self) -> None:
        # Skipping SSH is the user's choice, not a failure to report.
        run = self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, smb_listing=self.smb_listing_result(), smb_file_ops=[])
        self.assertFalse(any(result.domain == "device" for result in run.results))

    def test_run_doctor_checks_reports_the_apps_local_network_permission(self) -> None:
        cases = {
            "allowed": ("PASS", None),
            "denied": ("FAIL", "local_network_denied"),
            "unknown": ("INFO", None),
        }
        for permission, (status, code) in cases.items():
            with self.subTest(permission=permission):
                run = self.run_doctor_with_mocks(
                    local_network_permission=permission, skip_ssh=True, skip_bonjour=True, skip_smb=True,
                )
                result = next(result for result in run.results if "Local Network permission" in result.message)
                self.assertEqual((result.status, result.details.get("code"), result.domain), (status, code, "computer"))
                self.assertEqual(run.fatal, status == "FAIL")
        # The CLI has no such permission and says nothing about it.
        run = self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, skip_smb=True)
        self.assertFalse(any("Local Network permission" in result.message for result in run.results))

    def test_run_doctor_checks_reports_local_network_permission_even_without_a_config(self) -> None:
        run = self.run_doctor_with_mocks(exists=False, local_network_permission="denied")
        self.assertTrue(any(result.details.get("code") == "local_network_denied" for result in run.results))

    def test_run_doctor_checks_reports_this_macs_nsmb_conf_in_the_computer_section(self) -> None:
        nsmb_fail = CheckResult("FAIL", "/etc/nsmb.conf sets protocol_vers_map=2", {"code": "mac_smb3_disabled"})
        summary = {"global": {"present": True, "sections": [{"kind": "default", "protocol_vers_map": "2"}]}}
        for exists in (True, False):
            with self.subTest(config_exists=exists):
                debug_fields: dict[str, object] = {}
                run = self.run_doctor_with_mocks(
                    exists=exists,
                    skip_ssh=True,
                    skip_bonjour=True,
                    skip_smb=True,
                    debug_fields=debug_fields,
                    extra_patches={
                        "timecapsulesmb.checks.doctor_steps.check_nsmb_conf": mock.Mock(
                            return_value=([CheckResult(nsmb_fail.status, nsmb_fail.message, nsmb_fail.details)], summary)
                        ),
                    },
                )
                # Reported even before a configuration exists.
                result = next(result for result in run.results if result.details.get("code") == "mac_smb3_disabled")
                self.assertEqual((result.status, result.domain), ("FAIL", "computer"))
                self.assertTrue(run.fatal)
                self.assertEqual(debug_fields["nsmb_conf"], summary)

    def test_check_ssh_login_names_who_a_failure_points_at(self) -> None:
        connection = SshConnection("root@10.0.0.5", "pw", "")
        cases = (
            (SshLocalNetworkFilteredError("filtered"), "local_network_filtered"),
            (SshClientCrashedError(11, platform="darwin"), "local_network_filtered"),
            (SshClientConfigError("ssh too old"), "ssh_client_failed"),
            (SshAuthenticationError("denied"), "ssh_auth_rejected"),
            (SshAlgorithmNegotiationError("no shared key exchange", algorithm="kex", offered=("diffie-hellman-group1-sha1",)),
             "ssh_algorithm_negotiation_failed"),
            (SshNetworkError("timed out"), "ssh_unreachable"),
        )
        for error, code in cases:
            with self.subTest(code=code, error=type(error).__name__):
                # Port 22 answers: the failure is the SSH session's.
                with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=error), \
                        mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=None):
                    result = check_ssh_login(connection)
                self.assertEqual((result.status, result.details["code"]), ("FAIL", code))
        # ssh ran, and the command itself failed on the device.
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(returncode=1, stdout="")):
            result = check_ssh_login(connection)
        self.assertEqual(result.details["code"], "ssh_command_failed")

    def test_doctor_sink_counts_a_failure_the_device_explains_against_the_device(self) -> None:
        sink = DoctorSink(on_result=None, debug_fields=None)
        sink.blocked_by_device = _doctor_device_blocks(False, NATIVE_NBNS_READY, None)
        with sink.in_section("computer"):
            sink.add(CheckResult("FAIL", "SMB IPv4 is not reachable at 10.0.0.2:445", {"code": "smb_port_unreachable"}))
            sink.add(CheckResult("FAIL", "no discovered _smb._tcp instance", {"code": "bonjour_instance_not_seen"}))
        sink.add(CheckResult("FAIL", "device failure", {"code": "smb_port_unreachable"}))

        port, bonjour, device = sink.results
        self.assertEqual((port.status, port.details["code"], port.details["blocked_by_device"]),
                         ("SKIP", "smb_port_unreachable", True))
        self.assertTrue(port.message.endswith("; not counted against this computer: Samba is not running on the device (see Device)"))
        # Bonjour registration was not reported down, and device results are never changed.
        self.assertEqual(bonjour.status, "FAIL")
        self.assertEqual(device.status, "FAIL")

    def test_run_doctor_checks_does_not_blame_this_computer_when_samba_is_down_on_the_device(self) -> None:
        refused = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 10.0.0.2: NT_STATUS_CONNECTION_REFUSED",
            {"attempts": []},
        )
        smbd_down = ReadinessProbeResult(
            ready=False,
            detail="managed smbd parent process is not running",
            steps=(
                ProbeStepResult("smbd_config", "pass", "managed runtime smb.conf present"),
                ProbeStepResult("smbd_parent", "fail", "managed smbd parent process is not running"),
            ),
        )
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep"):
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_listing=refused,
                smbd_probe=smbd_down,
                skip_bonjour=True,
            )

        listing = next(result for result in run.results if result.details.get("code") == "smb_listing_failed")
        self.assertEqual((listing.status, listing.domain), ("SKIP", "computer"))
        self.assertIn("not counted against this computer: Samba is not running on the device", listing.message)
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["device"]["status"], "fail")
        self.assertNotIn("smb_listing_failed", sections["computer"]["fail_codes"])

    def test_doctor_device_blocks_name_only_services_the_device_reported_down(self) -> None:
        self.assertEqual(_doctor_device_blocks(None, None, None), {})
        self.assertEqual(_doctor_device_blocks(True, NATIVE_NBNS_READY, True), {})
        # Off is not down: there is no NBNS failure for the device to explain.
        self.assertEqual(_doctor_device_blocks(True, NATIVE_NBNS_OFF, True), {})
        self.assertEqual(set(_doctor_device_blocks(False, NATIVE_NBNS_NOT_READY, False)), {
            "smb_port_unreachable", "smb_listing_failed", NBNS_QUERY_TIMEOUT_CODE, NBNS_NEGATIVE_RESPONSE_CODE,
            "bonjour_instance_not_seen",
        })

    def test_doctor_report_sections_and_overall_status(self) -> None:
        def report(*results):
            return doctor_report(list(results))

        computer_fail = CheckResult("FAIL", "a", {"code": "smb_port_unreachable"}, domain="computer")
        computer_warn = CheckResult("WARN", "c", {"code": "smb_listing_host_unreachable"}, domain="computer")
        device_pass = CheckResult("PASS", "d", domain="device")
        not_checked = CheckResult("SKIP", "n", {"code": "device_not_checked"}, domain="device")

        both = report(computer_fail, CheckResult(computer_fail.status, "b", computer_fail.details, domain="computer"),
                      computer_warn, device_pass)
        self.assertEqual(both["sections"]["computer"]["fail_codes"], ["smb_port_unreachable"])
        self.assertEqual(both["sections"]["computer"]["warn_codes"], ["smb_listing_host_unreachable"])
        self.assertEqual(both["sections"]["computer"]["counts"], {"PASS": 0, "WARN": 1, "FAIL": 2, "INFO": 0})
        # A healthy device next to a failing computer: the device stays green.
        self.assertEqual(both["sections"]["device"]["status"], "pass")
        self.assertEqual(both["status"], "fail")

        # An untagged result is the device's.
        self.assertEqual(report(CheckResult("FAIL", "u"))["sections"]["device"]["status"], "fail")
        self.assertEqual(report(device_pass, computer_warn)["status"], "warn")
        # SSH skipped: nothing on the device, and that alone is no failure.
        skipped = report(CheckResult("PASS", "tools", domain="computer"))
        self.assertEqual((skipped["sections"]["device"]["status"], skipped["status"]), ("not_checked", "pass"))
        # SSH refused: only the "not checked" note.
        refused = report(CheckResult("FAIL", "ssh", {"code": "ssh_unreachable"}, domain="computer"), not_checked)
        self.assertEqual((refused["sections"]["device"]["status"], refused["status"]), ("not_checked", "fail"))
    def test_check_ssh_login_uses_configured_ssh_transport(self) -> None:
        connection = SshConnection("root@192.168.1.118", "pw", "-o ProxyCommand=jump")
        with mock.patch(
            "timecapsulesmb.checks.network.probe_ssh_command_conn",
            return_value=mock.Mock(ok=True, detail="ok"),
        ) as probe_mock:
            result = check_ssh_login(connection)
        self.assertEqual(result.status, "PASS")
        probe_mock.assert_called_once_with(
            connection,
            "/bin/echo ok",
            timeout=30,
            expected_stdout_suffix="ok",
        )

    def test_check_ssh_login_reports_friendlier_ssh_transport_error(self) -> None:
        connection = SshConnection("root@192.168.1.118", "pw", "-o LocalForward=127.0.0.1:108:127.0.0.1:108")
        with mock.patch(
            "timecapsulesmb.checks.network.probe_ssh_command_conn",
            return_value=mock.Mock(ok=False, detail="Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied"),
        ), mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value=None):
            result = check_ssh_login(connection)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.message,
            "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied",
        )

    def check_unanswered_ssh(self, ports: dict[int, str | None]):
        connection = SshConnection("root@10.0.0.5", "pw", "")
        connect = mock.Mock(side_effect=lambda _host, port, _timeout: ports[port])
        timed_out = SshCommandTimeout("Timed out waiting for ssh command to finish: /bin/echo ok")
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=timed_out), \
                mock.patch("timecapsulesmb.checks.network.tcp_connect_error", connect):
            return check_ssh_login(connection), connect

    def test_an_address_nothing_answers_at_is_unreachable_not_a_slow_command(self) -> None:
        # ssh waits out its connect timeout, so a dead address looked like a
        # command that timed out (telemetry install 079bca09, a device DHCP moved).
        result, connect = self.check_unanswered_ssh({22: "timed out", 5009: "timed out"})

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["code"], "device_unreachable")
        self.assertEqual(
            result.message,
            "The device is not answering at 10.0.0.5: neither SSH port 22 nor AirPort ACP port 5009 is reachable (timed out).",
        )
        self.assertEqual((result.details["ssh_port_error"], result.details["acp_port_error"]), ("timed out", "timed out"))
        self.assertEqual([call.args[:2] for call in connect.call_args_list], [("10.0.0.5", 22), ("10.0.0.5", 5009)])

    def test_ssh_off_with_acp_answering_says_so(self) -> None:
        result, _connect = self.check_unanswered_ssh({22: "Connection refused", 5009: None})

        self.assertEqual(result.details["code"], "ssh_disabled")
        self.assertEqual(result.message, "SSH is turned off on 10.0.0.5: AirPort ACP answers on port 5009, but SSH port 22 is closed.")

    def test_an_answering_ssh_port_keeps_the_session_failure(self) -> None:
        result, connect = self.check_unanswered_ssh({22: None, 5009: None})

        self.assertEqual(result.details["code"], "ssh_unreachable")
        self.assertIn("Timed out waiting for ssh command to finish", result.message)
        connect.assert_called_once()

    def test_acp_silent_while_ssh_refuses_says_acpd_is_stuck(self) -> None:
        # The device is up (port 22 refuses at once) and ACPd's port drops
        # every connect: only unplugging the device clears it.
        cases = {
            "one address": ({22: "Connection refused", 5009: "timed out"}, "timed out"),
            # IPv6 has no route; IPv4 answers: each address counts on its own.
            "two addresses": (
                {22: "timed out; Connection refused", 5009: "timed out; [Errno 65] No route to host"},
                "timed out; [Errno 65] No route to host",
            ),
        }
        for case, (ports, acp_error) in cases.items():
            with self.subTest(case=case):
                result, connect = self.check_unanswered_ssh(ports)

                self.assertEqual(result.status, "FAIL")
                # The ACPd step's own code: that step needs SSH and does not run.
                self.assertEqual(result.details["code"], "apple_acpd_broken")
                self.assertEqual(result.details["acpd_problem"], "acp_port_unanswered")
                self.assertEqual(
                    result.message,
                    # The firmware bug is ACPd's silence; the ports' answers follow as facts.
                    "Apple ACPd is broken: the AirPort service does not answer the network, a bug in Apple's firmware. "
                    f"AirPort ACP port 5009 does not answer at 10.0.0.5 ({acp_error}), while SSH port 22 refused a "
                    "connection. Unplug the device, wait 10 seconds, plug it back in and wait 2 minutes, then check again",
                )
                self.assertEqual((result.details["ssh_port_error"], result.details["acp_port_error"]), (ports[22], acp_error))
                # Port 22's own answer is reused, not asked again.
                self.assertEqual([call.args[:2] for call in connect.call_args_list], [("10.0.0.5", 22), ("10.0.0.5", 5009)])

    def closed_port(self, ports: dict[int, str | None], **kwargs):
        connect = mock.Mock(side_effect=lambda _host, port, _timeout: ports[port])
        with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", connect):
            closed = closed_ssh_port_error("10.0.0.5", **kwargs)
        return closed, [call.args[1] for call in connect.call_args_list]

    def test_a_closed_ssh_port_asks_port_22_only_when_acp_goes_unanswered(self) -> None:
        # Commands that failed their SSH probe have no port 22 error to give.
        cases = {
            "ACP answers": ({5009: None, 22: "Connection refused"}, "ssh_disabled", [5009]),
            "ACP refused": ({5009: "Connection refused", 22: "Connection refused"}, "device_unreachable", [5009]),
            "ACP host down": ({5009: "[Errno 64] Host is down", 22: "Connection refused"}, "device_unreachable", [5009]),
            "ACP silent, SSH refuses": ({5009: "timed out", 22: "Connection refused"}, "acpd_broken", [5009, 22]),
            "ACP silent, SSH just opened": ({5009: "timed out", 22: None}, "acpd_broken", [5009, 22]),
            "nothing answers": ({5009: "timed out", 22: "timed out"}, "device_unreachable", [5009, 22]),
        }
        for case, (ports, code, asked) in cases.items():
            with self.subTest(case=case):
                closed, ports_asked = self.closed_port(ports)

                self.assertEqual(closed.code, code)
                self.assertEqual(ports_asked, asked)

    def test_a_stuck_acpd_reads_as_the_device_answering_without_acp(self) -> None:
        closed, _asked = self.closed_port({5009: "timed out", 22: "Connection refused"})

        self.assertEqual(
            closed.message, "The device answers on the network, but its AirPort service does not.",
        )
        self.assertEqual(
            closed.detail, "AirPort ACP port 5009 does not answer at 10.0.0.5 (timed out), while SSH port 22 refused a connection.",
        )
        self.assertEqual(closed.details, {"acp_port_error": "timed out", "acpd_problem": "acp_port_unanswered"})
        self.assertEqual(closed.code, "acpd_broken")

    def test_a_stuck_acpd_error_says_how_port_22_answered(self) -> None:
        # Commands' errors carry only this text, so it keeps the evidence: a
        # refusal (SSH off) or a connect (SSH opened since the probe).
        for ssh_error, verb in (("Connection refused", "refused"), (None, "accepted")):
            with self.subTest(verb=verb):
                closed, _asked = self.closed_port({5009: "timed out", 22: ssh_error})

                self.assertTrue(closed.acp_stuck)
                self.assertTrue(closed.detail.endswith(f"while SSH port 22 {verb} a connection."), closed.detail)

    def test_a_password_or_filter_failure_does_not_probe_the_ports(self) -> None:
        connection = SshConnection("root@10.0.0.5", "pw", "")
        for error in (SshAuthenticationError("denied"), SshLocalNetworkFilteredError("filtered")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=error), \
                        mock.patch("timecapsulesmb.checks.network.tcp_connect_error") as connect:
                    check_ssh_login(connection)
                connect.assert_not_called()

    def test_check_ssh_login_reports_a_connection_this_mac_dropped_as_it_is(self) -> None:
        connection = SshConnection("root@10.0.0.5", "pw", "")
        # The real classifier, so the wording is this platform's, as doctor's is.
        dropped = parse_ssh_client_diagnostics("ssh: connect to host 10.0.0.5 port 22: Bad file descriptor\n").error
        self.assertIsInstance(dropped, SshLocalNetworkFilteredError)
        with mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=dropped):
            result = check_ssh_login(connection)

        self.assertEqual((result.status, result.message), ("FAIL", str(dropped)))

    def test_run_doctor_checks_skip_ssh_does_not_probe_nbns_flash_config(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch("timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution") as nbns_config_mock:
                        with mock.patch("timecapsulesmb.device.probe.run_ssh") as run_ssh_mock:
                            results, fatal = run_doctor_checks(
                                self.doctor_config(values),
                                repo_root=REPO_ROOT,
                                skip_ssh=True,
                                skip_bonjour=True,
                                skip_smb=True,
                            )
        self.assertFalse(fatal)
        nbns_config_mock.assert_not_called()
        run_ssh_mock.assert_not_called()

    def test_check_xattr_tdb_persistence_passes_for_disk_path(self) -> None:
        smb_conf = "    xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=smb_conf)):
            result = check_xattr_tdb_persistence(SshConnection("root@tc", "pw", "-o foo"))
        self.assertEqual(result.status, "PASS")
        self.assertIn("/Volumes/dk2/samba4/private/xattr.tdb", result.message)

    def test_check_xattr_tdb_persistence_fails_for_ramdisk_path(self) -> None:
        smb_conf = "    xattr_tdb:file = /mnt/Memory/samba4/private/xattr.tdb\n"
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=smb_conf)):
            result = check_xattr_tdb_persistence(SshConnection("root@tc", "pw", "-o foo"))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("non-persistent ramdisk", result.message)

    def test_check_xattr_tdb_persistence_warns_when_missing(self) -> None:
        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="[global]\n")):
            result = check_xattr_tdb_persistence(SshConnection("root@tc", "pw", "-o foo"))
        self.assertEqual(result.status, "WARN")
        self.assertIn("does not contain xattr_tdb:file", result.message)

    def test_check_xattr_tdb_persistence_uses_supplied_config_text(self) -> None:
        smb_conf = "    xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"
        read_active_smb_conf = mock.Mock(side_effect=AssertionError("active smb.conf should not be read again"))
        with mock.patch("timecapsulesmb.checks.doctor_steps.read_active_smb_conf_conn", read_active_smb_conf):
            result = check_xattr_tdb_persistence(SshConnection("root@tc", "pw", "-o foo"), config_text=smb_conf)
        self.assertEqual(result.status, "PASS")
        read_active_smb_conf.assert_not_called()

    def test_run_doctor_checks_reuses_active_smb_conf_for_xattr_check(self) -> None:
        active_smb_conf = "[global]\n    xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            read_active_smb_conf=active_smb_conf,
            xattr_result=CheckResult("PASS", "xattr ok"),
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertFalse(run.fatal)
        run.mocks.check_xattr_tdb_persistence.assert_called_once()
        self.assertIsInstance(run.mocks.check_xattr_tdb_persistence.call_args.args[0], SshConnection)
        self.assertEqual(run.mocks.check_xattr_tdb_persistence.call_args.args[1], active_smb_conf)

    def test_run_doctor_checks_reports_results_as_they_complete(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        emitted: list[str] = []
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[mock.Mock(status="PASS", message="bonjour ok")],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            on_result=lambda result: emitted.append(result.message),
        )
        self.assertFalse(run.fatal)
        self.assertEqual([result.message for result in run.results], emitted)

    def test_run_doctor_checks_emits_detailed_smb_operation_results(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        smb_results = [
            mock.Mock(status="PASS", message="SMB directory create works"),
            mock.Mock(status="PASS", message="SMB file create works"),
            mock.Mock(status="PASS", message="SMB file overwrite/edit works"),
            mock.Mock(status="PASS", message="SMB file read works"),
            mock.Mock(status="PASS", message="SMB file rename works"),
            mock.Mock(status="PASS", message="SMB file copy works"),
            mock.Mock(status="PASS", message="SMB file delete works"),
            mock.Mock(status="PASS", message="SMB directory ls list works"),
            mock.Mock(status="PASS", message="SMB directory delete works"),
            mock.Mock(status="PASS", message="SMB final cleanup check passed"),
        ]
        run = self.run_doctor_with_mocks(
            values,
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=smb_results,
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            skip_bonjour=True,
        )
        self.assertFalse(run.fatal)
        # The device's own SMB check changes files through the SSH tunnel to
        # its loopback; this computer's LAN check only lists shares.
        messages = [result.message for result in run.results]
        start = messages.index(smb_results[0].message)
        self.assertEqual(messages[start:start + 10], [result.message for result in smb_results])
        run.mocks.check_authenticated_smb_file_ops_detailed.assert_called_once_with(
            "admin", "pw", "127.0.0.1", "Data", port=2445,
        )
        run.mocks.ssh_local_forward.assert_called_once_with(
            mock.ANY, local_port=2445, remote_host="127.0.0.1", remote_port=445,
        )

    def test_run_doctor_checks_emits_naming_diagnostics(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "HomeSamba",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Home-Samba",
            "TC_MDNS_HOST_LABEL": "home-samba",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        active_smb_conf = """
[global]
    netbios name = HomeSamba

[Data]
    path = /Volumes/dk2/ShareRoot

[Data_Kitchen]
    path = /Volumes/dk2/Other
"""
        bonjour_instance = BonjourServiceInstance("_smb._tcp.local.", "Home-Samba", "Home-Samba._smb._tcp.local.")
        bonjour_record = BonjourResolvedService("Home-Samba", "home-samba.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"])
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            read_active_smb_conf=active_smb_conf,
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([bonjour_instance], [bonjour_record]), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": mock.Mock(return_value=(bonjour_record, None)),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 0))]
                ),
            },
        )
        self.assertFalse(run.fatal)
        info_messages = [result.message for result in run.results if result.status == "INFO"]
        self.assertIn("advertised Bonjour instance: Home-Samba", info_messages)
        self.assertIn("advertised Bonjour host label: home-samba", info_messages)
        self.assertIn("active Samba NetBIOS name: HomeSamba", info_messages)
        self.assertIn("active Samba share names: Data, Data_Kitchen", info_messages)

    def test_run_doctor_checks_fails_when_same_bonjour_instance_uses_inconsistent_service_targets(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.217",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_PAYLOAD_DIR_NAME": ".samba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        instance_name = "James's AirPort Time Capsule"
        instances = [
            BonjourServiceInstance("_airport._tcp.local.", instance_name, f"{instance_name}._airport._tcp.local."),
            BonjourServiceInstance("_smb._tcp.local.", instance_name, f"{instance_name}._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", instance_name, f"{instance_name}._adisk._tcp.local."),
            BonjourServiceInstance("_device-info._tcp.local.", instance_name, f"{instance_name}._device-info._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, "Jamess-AirPort-Time-Capsule.local", "_airport._tcp.local.", port=5009),
            BonjourResolvedService(instance_name, "james-s-airport-time-capsule.local", "_smb._tcp.local.", port=445, ipv4=["192.168.1.217"]),
            BonjourResolvedService(instance_name, "james-s-airport-time-capsule.local", "_adisk._tcp.local.", port=9),
            BonjourResolvedService(instance_name, "james-s-airport-time-capsule.local", "_device-info._tcp.local.", port=0),
        ]
        probed_identity = RuntimeNamingIdentityProbeResult(
            system_name=instance_name,
            hostname="jamess-airport-time-capsule",
            mdns_instance_name=instance_name,
            mdns_host_label="jamess-airport-time-capsule",
            netbios_name="jamess-airport-",
            detail="ok",
        )
        active_smb_conf = """
        [global]
            netbios name = jamess-airport-

        [AirPort Disk]
            path = /Volumes/dk2/ShareRoot
        """

        with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]), \
             mock.patch("timecapsulesmb.checks.doctor_steps.check_nsmb_conf", return_value=([], None)), \
             mock.patch("timecapsulesmb.checks.doctor_steps._add_tunneled_authenticated_smb_results",
                        return_value=DEVICE_SMB_PASSED):
            with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor_steps.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor_steps.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch(
                            "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed",
                            return_value=(BonjourDiscoverySnapshot(instances, records), None, None),
                        ):
                            with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.217", 0))]):
                                with mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing", return_value=self.smb_listing_result("james-s-airport-time-capsule.local")):
                                    with mock.patch("timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                        with mock.patch("timecapsulesmb.checks.doctor_steps.probe_remote_runtime_naming_identity_conn", return_value=probed_identity):
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=active_smb_conf)), \
                                                    mock.patch("timecapsulesmb.checks.doctor_steps.probe_migration_activity", return_value=MigrationActivity(())):
                                                with mock.patch("timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution",
                                                                return_value=CheckResult("PASS", "native NBNS resolved")):
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)

        self.assertTrue(fatal)
        messages = [result.message for result in results]
        self.assertIn(
            "Bonjour IPv4: advertised Bonjour service targets for \"James's AirPort Time Capsule\": _airport=Jamess-AirPort-Time-Capsule.local; _smb=james-s-airport-time-capsule.local; _adisk=james-s-airport-time-capsule.local; _device-info=james-s-airport-time-capsule.local",
            messages,
        )
        self.assertIn(
            "Bonjour IPv4: Bonjour services for \"James's AirPort Time Capsule\" advertise inconsistent host targets: _airport=Jamess-AirPort-Time-Capsule.local; _smb=james-s-airport-time-capsule.local; _adisk=james-s-airport-time-capsule.local; _device-info=james-s-airport-time-capsule.local",
            messages,
        )

    def test_run_doctor_checks_accepts_punctuated_instance_with_dns_safe_time_machine_target(self) -> None:
        instance_name = "A.B.'s AirPort Time Capsule"
        values = self.valid_doctor_values(
            TC_HOST="root@192.168.1.217",
            TC_MDNS_INSTANCE_NAME=instance_name,
            TC_MDNS_HOST_LABEL="time-capsule",
            TC_NETBIOS_NAME="TimeCapsule",
        )
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", instance_name, f"{instance_name}._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", instance_name, f"{instance_name}._adisk._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, "time-capsule.local", "_smb._tcp.local.", port=445, ipv4=["192.168.1.217"]),
            BonjourResolvedService(
                instance_name,
                "time-capsule.local",
                "_adisk._tcp.local.",
                port=9,
                properties={
                    "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                    "adVF": "0x1010",
                    "dk2": "adVF=0x82,adVN=AirPort Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
                },
            ),
        ]
        active_smb_conf = """
        [global]
            netbios name = TimeCapsule

        [AirPort Disk]
            path = /Volumes/dk2/ShareRoot
        """
        identity = RuntimeNamingIdentityProbeResult(
            system_name=instance_name,
            hostname="time-capsule.local",
            mdns_instance_name=instance_name,
            mdns_host_label="time-capsule",
            netbios_name="TimeCapsule",
            detail="ok",
        )

        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            read_active_smb_conf=active_smb_conf,
            runtime_naming_identity=identity,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
            },
        )

        self.assertFalse(run.fatal)
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: Bonjour _smb._tcp target host label is DNS-safe for Time Machine: time-capsule", messages)
        self.assertIn("Bonjour IPv4: Bonjour _adisk._tcp target host label is DNS-safe for Time Machine: time-capsule", messages)
        self.assertIn("Bonjour IPv4: _smb._tcp target host label matches runtime mDNS host label 'time-capsule'", messages)
        self.assertIn("Bonjour IPv4: _adisk._tcp TXT advertises active Time Machine shares: AirPort Disk", messages)
        self.assertFalse(any("unsafe label" in result.message for result in run.results))

    def test_run_doctor_checks_fails_when_time_machine_srv_target_uses_display_name_label(self) -> None:
        instance_name = "James's AirPort Time Capsule"
        values = self.valid_doctor_values(
            TC_HOST="root@192.168.1.217",
            TC_MDNS_INSTANCE_NAME=instance_name,
            TC_MDNS_HOST_LABEL="jamess-airport-time-capsule",
            TC_NETBIOS_NAME="jamess-airport-",
        )
        unsafe_host = "James's AirPort Time Capsule.local"
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", instance_name, f"{instance_name}._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", instance_name, f"{instance_name}._adisk._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, unsafe_host, "_smb._tcp.local.", port=445, ipv4=["192.168.1.217"]),
            BonjourResolvedService(
                instance_name,
                unsafe_host,
                "_adisk._tcp.local.",
                port=9,
                properties={
                    "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                    "adVF": "0x1010",
                    "dk2": "adVF=0x82,adVN=AirPort Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
                },
            ),
        ]
        active_smb_conf = """
        [global]
            netbios name = jamess-airport-

        [AirPort Disk]
            path = /Volumes/dk2/ShareRoot
        """
        identity = RuntimeNamingIdentityProbeResult(
            system_name=instance_name,
            hostname="jamess-airport-time-capsule",
            mdns_instance_name=instance_name,
            mdns_host_label="jamess-airport-time-capsule",
            netbios_name="jamess-airport-",
            detail="ok",
        )

        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            read_active_smb_conf=active_smb_conf,
            runtime_naming_identity=identity,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=False),
            },
        )

        self.assertTrue(run.fatal)
        messages = [result.message for result in run.results]
        self.assertIn(
            "Bonjour IPv4: Bonjour _smb._tcp target host \"James's AirPort Time Capsule.local\" uses unsafe label \"James's AirPort Time Capsule\"; Time Machine Settings may ignore SRV targets with spaces or punctuation",
            messages,
        )
        self.assertIn(
            "Bonjour IPv4: _smb._tcp target host label \"James's AirPort Time Capsule\" does not match runtime mDNS host label 'jamess-airport-time-capsule'",
            messages,
        )

    def test_run_doctor_checks_fails_when_adisk_txt_does_not_match_active_samba_shares(self) -> None:
        instance_name = "Home"
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME=instance_name,
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", instance_name, "Home._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", instance_name, "Home._adisk._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"]),
            BonjourResolvedService(
                instance_name,
                "home.local",
                "_adisk._tcp.local.",
                port=9,
                properties={
                    "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                    "adVF": "0x1010",
                    "dk2": "adVF=0x82,adVN=Backup,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
                },
            ),
        ]

        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=False),
            },
        )

        self.assertTrue(run.fatal)
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: _adisk._tcp TXT does not advertise active Samba share(s): Data", messages)
        self.assertIn("Bonjour IPv4: _adisk._tcp TXT advertises stale share(s) not present in active Samba config: Backup", messages)

    def test_run_doctor_checks_reports_adisk_name_samba_cannot_serve(self) -> None:
        # Samba serves "[Home  Disk]" as "Home Disk" and matches tree connects
        # exactly, so Time Machine cannot mount an ADisk name with two spaces.
        instance_name = "Home"
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME=instance_name,
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", instance_name, "Home._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", instance_name, "Home._adisk._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"]),
            BonjourResolvedService(
                instance_name,
                "home.local",
                "_adisk._tcp.local.",
                port=9,
                properties={
                    "sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                    "adVF": "0x1010",
                    "dk2": "adVF=0x82,adVN=Home  Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
                },
            ),
        ]
        active_smb_conf = DEFAULT_ACTIVE_SMB_CONF.replace("[Data]", "[Home  Disk]")

        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            read_active_smb_conf=active_smb_conf,
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=False),
            },
        )

        self.assertTrue(run.fatal)
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: _adisk._tcp TXT does not advertise active Samba share(s): Home Disk", messages)
        self.assertIn(
            "Bonjour IPv4: _adisk._tcp TXT advertises stale share(s) not present in active Samba config: Home  Disk",
            messages,
        )

    def _apple_responder_doctor_run(self, instances, records, *, advertise_afp: bool = False):
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME="Home",
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
            TC_MDNS_ADVERTISE_AFP="true" if advertise_afp else "false",
        )
        return self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=False),
            },
        )

    def _apple_responder_records(self, *, model: str | None = "TimeCapsule6,116"):
        records = [
            BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"]),
            BonjourResolvedService(
                "Home", "home.local", "_adisk._tcp.local.", port=9,
                properties={"sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010",
                            "dk2": "adVF=0x82,adVN=Data,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852"},
            ),
        ]
        if model is not None:
            records.append(BonjourResolvedService("Home", "home.local", "_device-info._tcp.local.", port=0, properties={"model": model}))
        return records

    def test_run_doctor_checks_passes_apple_responder_expectations(self) -> None:
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", "Home", "Home._adisk._tcp.local."),
            BonjourServiceInstance("_device-info._tcp.local.", "Home", "Home._device-info._tcp.local."),
            # Another device with a similar name is not a rename of ours.
            BonjourServiceInstance("_smb._tcp.local.", "Home (Office)", "Home (Office)._smb._tcp.local."),
        ]
        run = self._apple_responder_doctor_run(instances, self._apple_responder_records())
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: _adisk._tcp TXT advertises SMB only as configured", messages)
        self.assertIn("Bonjour IPv4: no duplicate SMB/ADisk registrations for device home.local", messages)
        self.assertIn("Bonjour IPv4: _device-info._tcp model is Apple's: TimeCapsule6,116", messages)
        self.assertFalse(any(result.status == "FAIL" and "Apple" in result.message for result in run.results), messages)

    def test_run_doctor_checks_fails_on_duplicate_device_services_and_foreign_model(self) -> None:
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local."),
            BonjourServiceInstance("_smb._tcp.local.", "Home (2)", "Home (2)._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", "Home (2)", "Home (2)._adisk._tcp.local."),
            BonjourServiceInstance("_device-info._tcp.local.", "Home", "Home._device-info._tcp.local."),
        ]
        records = self._apple_responder_records(model="Macmini9,1")
        records += [replace(record, name="Home (2)") for record in records[:2]]
        run = self._apple_responder_doctor_run(instances, records)
        self.assertTrue(run.fatal)
        failures = [result.message for result in run.results if result.status == "FAIL"]
        self.assertTrue(any("duplicate Bonjour registrations for device home.local" in m
                            and "_smb: Home, Home (2)" in m and "_adisk: Home, Home (2)" in m for m in failures), failures)
        self.assertTrue(any("_device-info._tcp model for 'Home' is Macmini9,1" in m for m in failures), failures)

    def test_doctor_accepts_apples_shared_conflict_name_and_ignores_original_name_on_peer(self) -> None:
        # Stock diskd on 2026-09-19 moved SMB and ADisk to '(2)' after only
        # SMB collided. syNm/hostname stayed unchanged. Identify the NAS by
        # its resolved endpoint, not the display name owned by the competitor.
        records = [replace(record, name="Home (2)") for record in self._apple_responder_records()]
        records.insert(0, BonjourResolvedService("Home", "peer.local", "_smb._tcp.local.", port=59431, ipv4=["10.0.0.9"]))
        instances = [BonjourServiceInstance(record.service_type, record.name, f"{record.name}.{record.service_type}")
                     for record in records]
        run = self._apple_responder_doctor_run(instances, records)
        self.assertFalse(run.fatal, [r.message for r in run.results if r.status == "FAIL"])
        self.assertTrue(any("resolved _smb._tcp instance 'Home (2)' to home.local:445" in r.message for r in run.results))
        self.assertTrue(any("_adisk._tcp TXT advertises active Time Machine shares: Data" in r.message for r in run.results))

    def test_doctor_rejects_wrong_endpoint_even_when_name_has_an_apple_suffix(self) -> None:
        for address, port in (("10.0.0.99", 445), ("10.0.0.2", 1234), ("10.0.0.2", 0)):
            with self.subTest(address=address, port=port):
                records = [replace(record, name="Home (2)") for record in self._apple_responder_records()]
                records[0] = replace(records[0], ipv4=[address], port=port)
                instances = [BonjourServiceInstance(r.service_type, r.name, f"{r.name}.{r.service_type}") for r in records]
                run = self._apple_responder_doctor_run(instances, records)
                self.assertTrue(run.fatal)
                self.assertTrue(any(r.status == "FAIL" and ("expected 10.0.0.2" in r.message or "expected 445" in r.message)
                                    for r in run.results), [r.message for r in run.results])

    def test_resolve_observed_apple_suffix_verifies_device_before_selecting_it(self) -> None:
        ours = BonjourServiceInstance("_smb._tcp.local.", "Home (2)", "Home (2)._smb._tcp.local.")
        peer = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        record = BonjourResolvedService("Home (2)", "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"])
        resolver = mock.Mock(return_value=(record, None))
        resolution = resolve_expected_smb_record(
            [peer, ours], [BonjourResolvedService("Home", "peer.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.9"])],
            expected_instance_name="Home", expected_host_label="home", target_ip="10.0.0.2", resolver=resolver,
        )
        self.assertEqual(resolution.instance, ours)
        self.assertEqual(resolution.record, record)
        self.assertEqual(resolver.call_args.args[0], ours)

    def test_original_name_on_peer_is_not_a_concrete_failure_for_expected_device(self) -> None:
        peer = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        record = BonjourResolvedService("Home", "peer.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.9"])
        resolution = resolve_expected_smb_record([peer], [record], expected_instance_name="Home",
                                                  expected_host_label="home", target_ip="10.0.0.2")
        self.assertIsNone(resolution.record)
        self.assertIn("belongs to another device", resolution.error.message)

    def test_renamed_link_local_service_still_requires_the_correct_scope(self) -> None:
        # A native conflict suffix does not make equal fe80 address bytes on
        # two different links the same device.
        instance = BonjourServiceInstance("_smb._tcp.local.", "Home (2)", "Home (2)._smb._tcp.local.")
        record = BonjourResolvedService("Home (2)", "home.local", "_smb._tcp.local.",
                                         port=445, ipv6=["fe80::1234%2"])
        resolver = mock.Mock(return_value=(None, CheckResult("FAIL", "not resolved")))
        wrong = resolve_expected_smb_record([instance], [record], expected_instance_name="Home",
                                            target_ip="fe80::1234%1", resolver=resolver)
        self.assertIsNone(wrong.record)
        right = resolve_expected_smb_record([instance], [record], expected_instance_name="Home",
                                            target_ip="fe80::1234%2", resolver=resolver)
        self.assertIs(right.record, record)

    def test_run_doctor_checks_gives_no_model_verdict_without_device_info(self) -> None:
        instances = [BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")]
        run = self._apple_responder_doctor_run(instances, self._apple_responder_records(model=None))
        messages = [result.message for result in run.results]
        self.assertFalse(any("_device-info._tcp model" in m for m in messages))   # nothing resolved: no verdict

    def test_run_doctor_checks_fails_when_adisk_service_is_missing_for_active_shares(self) -> None:
        instance_name = "Home"
        values = self.valid_doctor_values(
            TC_HOST="root@10.0.0.2",
            TC_MDNS_INSTANCE_NAME=instance_name,
            TC_MDNS_HOST_LABEL="home",
            TC_NETBIOS_NAME="Home",
        )
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", instance_name, "Home._smb._tcp.local."),
            BonjourServiceInstance("_device-info._tcp.local.", instance_name, "Home._device-info._tcp.local."),
        ]
        records = [
            BonjourResolvedService(instance_name, "home.local", "_smb._tcp.local.", port=445, ipv4=["10.0.0.2"]),
            BonjourResolvedService(instance_name, "home.local", "_device-info._tcp.local.", port=0),
        ]

        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot(instances, records), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(side_effect=OSError("no dns")),
                "timecapsulesmb.discovery.bonjour.command_exists": mock.Mock(return_value=False),
            },
        )

        self.assertTrue(run.fatal)
        self.assertIn(
            "Bonjour IPv4: _adisk._tcp Time Machine service missing for 'Home'; Time Machine Settings will not list active shares: Data",
            [result.message for result in run.results],
        )

    def test_run_doctor_checks_passes_bonjour_when_service_record_lacks_embedded_ip_but_host_resolves(self) -> None:
        values = {
            "TC_HOST": "root@10.0.1.1",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "Home",
            "TC_PAYLOAD_DIR_NAME": ".samba4",
            "TC_MDNS_INSTANCE_NAME": "Home",
            "TC_MDNS_HOST_LABEL": "home",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        bonjour_instance = BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local.")
        bonjour_record = BonjourResolvedService("Home", "home.local", "_smb._tcp.local.", port=445)
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.1.1", 0))]
        resolve_mock = mock.Mock(side_effect=AssertionError("fallback resolve should not run"))
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([bonjour_instance], [bonjour_record]), None, None)
                ),
                "timecapsulesmb.checks.doctor_steps.resolve_smb_instance": resolve_mock,
                "timecapsulesmb.core.net.socket.getaddrinfo": mock.Mock(return_value=addrinfo),
                "timecapsulesmb.checks.doctor_steps.check_bonjour_host_ip": mock.Mock(side_effect=check_bonjour_host_ip),
            },
        )

        self.assertFalse(run.fatal)
        resolve_mock.assert_not_called()
        pass_messages = [result.message for result in run.results if result.status == "PASS"]
        self.assertIn("Bonjour IPv4: discovered _smb._tcp instance 'Home'", pass_messages)
        self.assertIn("Bonjour IPv4: resolved _smb._tcp instance 'Home' to home.local:445", pass_messages)
        self.assertIn("Bonjour IPv4: resolved Bonjour host home.local to 10.0.1.1", pass_messages)

    def test_run_doctor_checks_lists_shares_before_selecting_active_file_ops_share(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        run = self.run_doctor_with_mocks(
            values,
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            runtime_naming_identity=self.runtime_identity_from_values(values),
        )
        self.assertFalse(run.fatal)
        # The device lists and changes files through the SSH tunnel first;
        # this computer then lists the shares over the LAN.
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_args_list, [
            mock.call("admin", "pw", "127.0.0.1", port=2445),
            mock.call("admin", "pw", [SmbClientTarget("timecapsulesamba4.local", "10.0.0.2")], port=445),
        ])
        run.mocks.check_authenticated_smb_file_ops_detailed.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            "Data",
            port=2445,
        )
        self.assertTrue(any(result.status == "PASS" and "includes active share 'Data'" in result.message for result in run.results))

    def test_run_doctor_checks_matches_active_share_whose_volume_name_has_double_spaces(self) -> None:
        # A v3.1.1 smb.conf keeps the volume name's two spaces; Samba serves
        # and lists the share with one. Doctor must match it and test that share.
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        active_smb_conf = DEFAULT_ACTIVE_SMB_CONF.replace("[Data]", "[Nicholas  McBride's Time Ca]")
        run = self.run_doctor_with_mocks(
            values,
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_bonjour=True,
            smb_listing=self.smb_listing_result(disk_shares=["Nicholas McBride's Time Ca"]),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(active_smb_conf=active_smb_conf),
            read_active_smb_conf=active_smb_conf,
            runtime_naming_identity=self.runtime_identity_from_values(values),
        )

        self.assertFalse(run.fatal)
        run.mocks.check_authenticated_smb_file_ops_detailed.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            "Nicholas McBride's Time Ca",
            port=2445,
        )
        self.assertIn(
            "authenticated SMB listing includes active share \"Nicholas McBride's Time Ca\"",
            [result.message for result in run.results if result.status == "PASS"],
        )

    def test_run_doctor_checks_fails_when_active_share_missing_from_smb_listing(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result(disk_shares=["Public"]))
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            skip_bonjour=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertTrue(run.fatal)
        self.assertTrue(
            any(
                result.status == "FAIL"
                and "authenticated SMB listing did not include any active Samba share" in result.message
                and "Data" in result.message
                and "Public" in result.message
                for result in run.results
            )
        )
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_skip_ssh_uses_listed_smb_share_for_file_ops(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result("10.0.0.2", disk_shares=["Public"]))
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "INFO" and "active Samba share comparison skipped; SSH check skipped" in result.message for result in run.results))
        listing_mock.assert_called_once_with(
            "admin",
            "pw",
            [SmbClientTarget("10.0.0.2", "10.0.0.2")],
            port=445,
        )
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "10.0.0.2",
            "Public",
            port=445,
        )

    def test_run_doctor_checks_skip_ssh_fails_when_smb_listing_has_no_disk_shares(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result(disk_shares=[]))
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertTrue(run.fatal)
        self.assertTrue(any(result.status == "FAIL" and "no disk shares were advertised" in result.message for result in run.results))
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_ssh_ok_skips_authenticated_smb_when_requested(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result())
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            skip_bonjour=True,
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        listing_mock.assert_not_called()
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_skip_ssh_and_skip_smb_runs_no_authenticated_smb(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result())
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        listing_mock.assert_not_called()
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_ignores_legacy_mdns_host_label_for_smb_targets(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "10.0.1.99",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        probed_identity = RuntimeNamingIdentityProbeResult(
            system_name="Time Capsule",
            hostname="time-capsule",
            mdns_instance_name="Time Capsule",
            mdns_host_label="time-capsule",
            netbios_name="time-capsule",
            detail="ok",
        )
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_listing=self.smb_listing_result("time-capsule.local"),
            smb_file_ops=[],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            runtime_naming_identity=probed_identity,
            skip_bonjour=True,
        )
        self.assertFalse(any("TC_MDNS_HOST_LABEL" in result.message for result in run.results))
        self.assertFalse(run.fatal)
        called_servers = run.mocks.check_authenticated_smb_listing.call_args.args[2]
        self.assertIn(SmbClientTarget("time-capsule.local", "10.0.0.2"), called_servers)
        self.assertNotIn("10.0.1.99.local", called_servers)

    def test_check_authenticated_smb_listing_passes_on_share_table_when_ipv6_name_exits_1(self) -> None:
        # smbclient -L exits 1 after listing the shares when the server name
        # has no IPv4 address; the listing itself worked.
        proc = subprocess.CompletedProcess(
            ["smbclient"],
            1,
            "Disk|Data|\nIPC|IPC$|IPC Service (Samba)\ntc.local is an IPv6 address -- no workgroup available\n",
            "",
        )
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing("admin", "pw", SmbClientTarget("tc.local", "fe80::1%en0"))
        self.assertEqual(result.status, "PASS")
        self.assertIn("listing works", result.message)
        self.assertEqual(result.details["server"], "tc.local")
        self.assertEqual(result.details["disk_shares"], ["Data"])
        self.assertEqual(result.details["attempts"][0]["returncode"], 1)

    def test_check_authenticated_smb_listing_fails_without_share_table_despite_exit_0(self) -> None:
        # -L exits 0 once IPC$ accepts the login, even when the share
        # enumeration that follows fails.
        # The reason is on stdout; stderr noise must not replace it.
        proc = subprocess.CompletedProcess(
            ["smbclient"],
            0,
            "Error returning browse list: NT_STATUS_ACCESS_DENIED\n",
            "lpcfg_do_global_parameter: WARNING: The \"client lanman auth\" option is deprecated\n",
        )
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing("admin", "pw", "server.local")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("Error returning browse list: NT_STATUS_ACCESS_DENIED", result.message)
        self.assertEqual(result.details["attempts"][0]["failure"], "Error returning browse list: NT_STATUS_ACCESS_DENIED")
        self.assertEqual(result.details["attempts"][0]["outcome"], "error")
        self.assertNotIn("disk_shares", result.details)

    def test_check_authenticated_smb_listing_fails_on_logon_failure(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 1, "session setup failed: NT_STATUS_LOGON_FAILURE\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing("admin", "pw", "server.local")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("NT_STATUS_LOGON_FAILURE", result.message)

    def test_check_authenticated_smb_listing_fails_without_smbclient(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=False):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture") as run_mock:
                result = check_authenticated_smb_listing("admin", "pw", ["server.local"])
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.message, "missing local tool smbclient")
        run_mock.assert_not_called()

    def test_parse_smbclient_disk_shares_uses_machine_listing_types(self) -> None:
        output = "\n".join([
            "Disk|Data|Main storage",
            "IPC|IPC$|IPC Service",
            "Printer|lp|Printer",
            "Disk|Archive Data|",
            "Disk|Data|Duplicate",
            "SMB1 disabled -- no workgroup available",
            "Server|TC|Time Capsule",
            "Workgroup|WORKGROUP|TC",
        ])

        self.assertEqual(parse_smbclient_disk_shares(output), ["Data", "Archive Data"])

    def test_parse_smbclient_disk_shares_tells_no_table_from_no_disk_shares(self) -> None:
        self.assertIsNone(parse_smbclient_disk_shares(""))
        self.assertIsNone(parse_smbclient_disk_shares("session setup failed: NT_STATUS_LOGON_FAILURE\nData\n"))
        self.assertEqual(parse_smbclient_disk_shares("IPC|IPC$|IPC Service (Samba)\n"), [])

    def test_check_authenticated_smb_listing_falls_back_to_second_server_when_first_times_out(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[
                    subprocess.TimeoutExpired(cmd=["smbclient"], timeout=30),
                    proc,
                ],
            ):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    ["home.local", "10.0.1.1"],
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("admin@10.0.1.1", result.message)
        self.assertEqual(result.details["server"], "10.0.1.1")

    def test_check_authenticated_smb_listing_continues_when_first_server_returns_no_share_table(self) -> None:
        missing_proc = subprocess.CompletedProcess(["smbclient"], 0, "Error returning browse list: NT_STATUS_ACCESS_DENIED\n", "")
        good_proc = subprocess.CompletedProcess(["smbclient"], 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[missing_proc, good_proc],
            ):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    ["home.local", "10.0.1.1"],
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("admin@10.0.1.1", result.message)
        self.assertEqual(result.details["server"], "10.0.1.1")
        self.assertEqual([attempt["outcome"] for attempt in result.details["attempts"]], ["error", "pass"])

    def test_check_authenticated_smb_listing_records_attempt_debug_details(self) -> None:
        failed_proc = subprocess.CompletedProcess(["smbclient"], 1, "", "NT_STATUS_IO_TIMEOUT\n")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[
                    subprocess.TimeoutExpired(cmd=["smbclient"], timeout=30),
                    failed_proc,
                ],
            ):
                result = check_authenticated_smb_listing(
                    "admin",
                    "secret-password",
                    ["home.local", "10.0.1.1"],
                )

        self.assertEqual(result.status, "FAIL")
        attempts = result.details["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["server"], "home.local")
        self.assertEqual(attempts[0]["outcome"], "timeout")
        self.assertEqual(attempts[0]["timeout_sec"], 20)
        self.assertEqual(attempts[1]["server"], "10.0.1.1")
        self.assertEqual(attempts[1]["outcome"], "error")
        self.assertEqual(attempts[1]["returncode"], 1)
        self.assertEqual(attempts[1]["failure"], "NT_STATUS_IO_TIMEOUT")
        self.assertNotIn("secret-password", str(attempts))
        self.assertIn("authenticated SMB listing failed after 2 attempt(s)", result.message)
        self.assertIn("attempt 1 home.local", result.message)
        self.assertIn("attempt 2 10.0.1.1", result.message)
        self.assertIn("NT_STATUS_IO_TIMEOUT", result.message)
        self.assertNotIn("secret-password", result.message)

    def test_run_doctor_checks_retries_transient_smb_listing_after_shared_delay(self) -> None:
        transient = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 home.local: NT_STATUS_IO_TIMEOUT",
            {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]},
        )
        listing_mock = mock.Mock(side_effect=[transient, self.smb_listing_result()])

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                skip_bonjour=True,
                smb_file_ops=[],
                extra_patches={"timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(listing_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        listing_results = [result for result in run.results if result.message == "listing ok"]
        self.assertEqual(len(listing_results), 1)
        self.assertEqual(listing_results[0].details["attempts"][0]["next_retry_delay_sec"], 10)

    def test_run_doctor_checks_retries_smb_listing_targets_by_round(self) -> None:
        first_round = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 2 attempt(s)",
            {
                "attempts": [
                    {"server": "home.local", "outcome": "error", "failure": "NT_STATUS_CONNECTION_REFUSED"},
                    {"server": "10.0.1.1", "outcome": "error", "failure": "NT_STATUS_CONNECTION_REFUSED"},
                ]
            },
        )
        listing_mock = mock.Mock(side_effect=[first_round, self.smb_listing_result("home.local")])

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                skip_bonjour=True,
                smb_file_ops=[],
                extra_patches={"timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock},
            )

        self.assertFalse(run.fatal)
        self.assertEqual(listing_mock.call_count, 2)
        sleep_mock.assert_called_once_with(10)
        listing_result = next(result for result in run.results if result.message == "listing ok")
        self.assertEqual([attempt["server"] for attempt in listing_result.details["attempts"]], ["home.local", "10.0.1.1"])

    def test_run_doctor_checks_exhausts_transient_smb_listing_retries(self) -> None:
        failures = [
            CheckResult("FAIL", "listing failed", {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]}),
            CheckResult("FAIL", "listing failed", {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]}),
            CheckResult("FAIL", "listing failed", {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_IO_TIMEOUT"}]}),
        ]
        listing_mock = mock.Mock(side_effect=failures)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                skip_bonjour=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock},
            )

        self.assertTrue(run.fatal)
        self.assertEqual(listing_mock.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [10, 15])
        final_listing = next(result for result in run.results if result.message.startswith("authenticated SMB listing failed after 3 attempt(s)"))
        attempts = final_listing.details["attempts"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[0]["next_retry_delay_sec"], 10)
        self.assertEqual(attempts[1]["next_retry_delay_sec"], 15)
        self.assertNotIn("next_retry_delay_sec", attempts[2])

    def test_run_doctor_checks_does_not_retry_smb_listing_auth_failure(self) -> None:
        failure = CheckResult(
            "FAIL",
            "listing failed",
            {"attempts": [{"server": "home.local", "outcome": "error", "failure": "NT_STATUS_LOGON_FAILURE"}]},
        )
        listing_mock = mock.Mock(return_value=failure)

        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                skip_bonjour=True,
                extra_patches={"timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock},
            )

        self.assertTrue(run.fatal)
        listing_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_run_doctor_checks_adds_smb_listing_attempts_to_debug_fields(self) -> None:
        debug_fields: dict[str, object] = {}
        listing_attempts = [
            {"server": "timecapsulesamba4.local", "outcome": "error", "failure": "NT_STATUS_LOGON_FAILURE"},
            {"server": "10.0.0.2", "outcome": "error", "failure": "NT_STATUS_LOGON_FAILURE"},
        ]
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_listing=CheckResult(
                "FAIL",
                "authenticated SMB listing failed: NT_STATUS_LOGON_FAILURE",
                {"attempts": listing_attempts},
            ),
            smb_file_ops=[],
            debug_fields=debug_fields,
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            debug_fields["authenticated_smb_listing_servers"],
            ["timecapsulesamba4.local via 10.0.0.2"],
        )
        self.assertEqual(debug_fields["authenticated_smb_listing_active_shares"], ["Data"])
        self.assertEqual(debug_fields["authenticated_smb_listing_attempts"], listing_attempts)

    def test_run_doctor_checks_warns_when_this_mac_cannot_reach_smb_that_works_on_the_device(self) -> None:
        # macOS refuses the LAN connection (NT_STATUS_HOST_UNREACHABLE) when the
        # app lacks Local Network permission; the device's own SMB check
        # through SSH still passes, so the device stays healthy.
        debug_fields: dict[str, object] = {}
        direct_attempts = [
            {
                "server": "timecapsulesamba4.local",
                "ip_address": "10.0.0.2",
                "outcome": "error",
                "failure": "do_connect: Connection to timecapsulesamba4.local failed (Error NT_STATUS_HOST_UNREACHABLE)",
            }
        ]
        direct_failure = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 timecapsulesamba4.local via 10.0.0.2: NT_STATUS_HOST_UNREACHABLE",
            {"attempts": direct_attempts},
        )
        listing_mock = mock.Mock(side_effect=[self.smb_listing_result("127.0.0.1"), direct_failure])
        file_ops_mock = mock.Mock(return_value=[CheckResult("PASS", "file ops ok")])

        run = self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        run.mocks.ssh_local_forward.assert_called_once_with(mock.ANY, local_port=2445, remote_host="127.0.0.1", remote_port=445)
        self.assertEqual(listing_mock.call_args_list[0], mock.call("admin", "pw", "127.0.0.1", port=2445))
        file_ops_mock.assert_called_once_with("admin", "pw", "127.0.0.1", "Data", port=2445)
        self.assertEqual(next(r for r in run.results if r.message == "file ops ok").domain, "device")
        warning = next(result for result in run.results if result.details.get("code") == "smb_listing_host_unreachable")
        self.assertEqual((warning.status, warning.domain), ("WARN", "computer"))
        self.assertIn("Local Network permission", warning.message)
        self.assertEqual(debug_fields["authenticated_smb_listing_attempts"], direct_attempts)
        self.assertEqual(debug_fields["device_smb_listing_servers"], ["127.0.0.1"])

    def device_smb_run(self, listing, **kwargs):
        return self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": mock.Mock(return_value=[]),
                **kwargs.pop("extra_patches", {}),
            },
            **kwargs,
        )

    def test_run_doctor_checks_says_in_each_section_that_the_smb_checks_were_skipped(self) -> None:
        listing = mock.Mock(return_value=self.smb_listing_result())
        run = self.device_smb_run(listing, skip_smb=True)

        listing.assert_not_called()
        run.mocks.ssh_local_forward.assert_not_called()
        skipped = [result for result in run.results if result.details.get("code") == "smb_checks_skipped"]
        self.assertEqual(
            [(result.status, result.domain, result.message) for result in skipped],
            [
                ("SKIP", "device", "SMB login and file checks skipped (Checkup option in the macOS app, or --skip-smb)"),
                ("SKIP", "computer", "SMB login and file checks skipped (Checkup option in the macOS app, or --skip-smb)"),
            ],
        )
        self.assertFalse(run.fatal)

        # Without SSH only this computer would have run them.
        run = self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, skip_smb=True)
        self.assertEqual(
            [result.domain for result in run.results if result.details.get("code") == "smb_checks_skipped"],
            ["computer"],
        )

    def test_run_doctor_checks_blames_the_device_when_samba_rejects_the_airport_password(self) -> None:
        # The password check said the saved password is the AirPort admin
        # password, so Samba rejecting it means the device has not applied it.
        rejected = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 127.0.0.1: NT_STATUS_LOGON_FAILURE",
            {"attempts": []},
        )
        listing = mock.Mock(return_value=rejected)
        run = self.device_smb_run(listing, device_reading=acp_reading(True))

        failure = next(result for result in run.results if result.details.get("code") == "device_smb_password_stale")
        self.assertEqual((failure.status, failure.domain), ("FAIL", "device"))
        self.assertIn("restart the device", failure.message)
        self.assertNotIn("smb_login_rejected", doctor_report(run.results)["sections"]["computer"]["fail_codes"])
        listing.assert_called_once()

    def test_run_doctor_checks_reports_other_errors_on_the_device_without_blaming_the_tunnel(self) -> None:
        def listing(username, password, server, port=None):
            # Only the device's listing, through the tunnel, breaks.
            if server == "127.0.0.1":
                raise RuntimeError("smbclient crashed")
            return self.smb_listing_result()

        run = self.device_smb_run(mock.Mock(side_effect=listing))

        failure = next(result for result in run.results if "smbclient crashed" in result.message)
        self.assertEqual((failure.status, failure.domain), ("FAIL", "device"))
        self.assertEqual(failure.message, "authenticated SMB checks on the device failed: smbclient crashed")
        self.assertNotIn("code", failure.details)

    def test_run_doctor_checks_blames_this_computer_when_samba_rejects_the_saved_password(self) -> None:
        rejected = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 127.0.0.1: NT_STATUS_LOGON_FAILURE",
            {"attempts": []},
        )
        listing = mock.Mock(return_value=rejected)
        # ACP did not answer, so whether the password is right is unknown.
        run = self.device_smb_run(listing, device_reading=acp_reading(None))

        self.assertTrue(run.fatal)
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["computer"]["fail_codes"], ["smb_login_rejected"])
        # The device is not broken, only not checked: yellow.
        self.assertEqual(sections["device"]["status"], "warn")
        self.assertEqual(sections["device"]["warn_codes"], ["device_smb_not_checked"])
        # The LAN listing would only be rejected again.
        listing.assert_called_once_with("admin", "pw", "127.0.0.1", port=2445)

    def test_run_doctor_checks_does_not_check_samba_with_a_password_known_not_to_match(self) -> None:
        listing = mock.Mock(return_value=self.smb_listing_result())
        run = self.device_smb_run(listing, device_reading=acp_reading(False))

        listing.assert_not_called()
        run.mocks.ssh_local_forward.assert_not_called()
        warning = next(result for result in run.results if result.details.get("code") == "device_smb_not_checked")
        self.assertEqual((warning.status, warning.domain), ("WARN", "device"))
        self.assertIn("not the AirPort admin password", warning.message)
        mismatch = next(result for result in run.results if "not the AirPort admin password" in result.message
                        and result is not warning)
        self.assertEqual((mismatch.status, mismatch.domain), ("WARN", "computer"))
        # It runs with this computer's setup, before anything on the device.
        self.assertLess(run.results.index(mismatch), next(
            index for index, result in enumerate(run.results) if result.domain == "device"
        ))

    def test_run_doctor_checks_reports_the_device_not_checked_without_smbclient_here(self) -> None:
        listing = mock.Mock(return_value=self.smb_listing_result())
        run = self.device_smb_run(
            listing,
            extra_patches={"timecapsulesmb.checks.doctor_steps.command_exists": mock.Mock(return_value=False)},
        )

        run.mocks.ssh_local_forward.assert_not_called()
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["device"]["status"], "warn")
        self.assertEqual(sections["device"]["warn_codes"], ["device_smb_not_checked"])
        # The LAN listing would only report the missing tool a second time.
        listing.assert_not_called()
        self.assertNotIn("smb_listing_failed", sections["computer"]["fail_codes"])

    def broken_smbclient_patches(self) -> dict[str, object]:
        return {
            "timecapsulesmb.checks.doctor_steps.broken_smbclient_output": mock.Mock(return_value="BACKTRACE"),
            "timecapsulesmb.checks.doctor_steps.find_command": mock.Mock(return_value="/opt/local/bin/smbclient"),
        }

    def test_run_doctor_checks_blames_a_smbclient_here_that_does_not_work(self) -> None:
        listing = mock.Mock(return_value=self.smb_listing_result())
        debug_fields: dict[str, object] = {}
        run = self.device_smb_run(listing, debug_fields=debug_fields, extra_patches=self.broken_smbclient_patches())

        # Nothing runs smbclient against the device, through the tunnel or over the LAN.
        listing.assert_not_called()
        run.mocks.ssh_local_forward.assert_not_called()
        failure = next(result for result in run.results if "does not work" in result.message and result.status == "FAIL")
        self.assertEqual(failure.domain, "computer")
        self.assertEqual(
            failure.message,
            "smbclient at /opt/local/bin/smbclient does not work; doctor cannot run its SMB checks until it does",
        )
        warning = next(result for result in run.results if result.details.get("code") == "device_smb_not_checked")
        self.assertEqual((warning.status, warning.domain), ("WARN", "device"))
        self.assertIn("smbclient on this computer does not work", warning.message)
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["device"]["status"], "warn")
        self.assertNotIn("smb_listing_failed", sections["computer"]["fail_codes"])
        # The tool's own output goes to telemetry, not to the user.
        self.assertEqual(debug_fields["local_smbclient_self_test"], "BACKTRACE")
        self.assertNotIn("BACKTRACE", failure.message)

    def test_run_doctor_checks_blames_a_broken_smbclient_here_without_ssh(self) -> None:
        # With no SSH the device's check never runs; this computer's check still says why no SMB check ran.
        listing = mock.Mock(return_value=self.smb_listing_result())
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="FAIL", message="ssh refused"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing,
                **self.broken_smbclient_patches(),
            },
        )

        listing.assert_not_called()
        self.assertEqual(
            [result.domain for result in run.results if result.status == "FAIL" and "does not work" in result.message],
            ["computer"],
        )

    def test_run_doctor_checks_blames_a_broken_smbclient_here_when_the_device_does_not_answer(self) -> None:
        # The network checks never run against a dead address; this computer's check still says why.
        query = mock.Mock()
        query.return_value.browse.return_value = (BonjourDiscoverySnapshot([], []), None)
        run = self.run_doctor_with_mocks(
            ssh_login=self.UNREACHABLE,
            extra_patches={"timecapsulesmb.checks.doctor_steps.BonjourQuery": query, **self.broken_smbclient_patches()},
        )

        failures = [(r.domain, r.details.get("code")) for r in run.results if r.status == "FAIL"]
        self.assertIn(("computer", "device_unreachable"), failures)
        self.assertEqual(
            [r.domain for r in run.results if r.status == "FAIL" and "does not work" in r.message], ["computer"],
        )

    def test_run_doctor_checks_reports_a_missing_smbclient_once_without_ssh(self) -> None:
        # Without SSH only the LAN checks would use smbclient, and they could only report it missing again.
        listing = mock.Mock(return_value=self.smb_listing_result())
        missing = CheckResult("FAIL", "missing local tool smbclient, please install smbclient on your computer")
        run = self.run_doctor_with_mocks(
            local_tools=[missing],
            ssh_login=mock.Mock(status="FAIL", message="ssh refused"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing,
                "timecapsulesmb.checks.doctor_steps.command_exists": mock.Mock(return_value=False),
            },
        )

        listing.assert_not_called()
        self.assertEqual([r for r in run.results if "local tool smbclient" in r.message], [missing])

    def test_run_doctor_checks_does_not_test_smbclient_when_smb_checks_are_skipped(self) -> None:
        # Skipping the SMB checks is how people get past a broken smbclient; it must not fail doctor.
        listing = mock.Mock(return_value=self.smb_listing_result())
        patches = self.broken_smbclient_patches()
        run = self.device_smb_run(listing, skip_smb=True, extra_patches=patches)

        patches["timecapsulesmb.checks.doctor_steps.broken_smbclient_output"].assert_not_called()
        self.assertFalse([result for result in run.results if "does not work" in result.message])

    def test_run_doctor_checks_fails_the_device_when_its_ssh_tunnel_cannot_open(self) -> None:
        # Deploy needs SSH too, so the device is not green without it.
        listing = mock.Mock(return_value=self.smb_listing_result())
        run = self.device_smb_run(
            listing,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.ssh_local_forward": mock.Mock(side_effect=SshError("tunnel refused")),
            },
        )

        failure = next(result for result in run.results if result.details.get("code") == "device_smb_tunnel_failed")
        self.assertEqual((failure.status, failure.domain), ("FAIL", "device"))
        self.assertIn("tunnel refused", failure.message)
        # This computer's LAN listing still runs, without file operations.
        listing.assert_called_once()

    def test_run_doctor_checks_fails_an_unreachable_lan_listing_when_nothing_checked_smb(self) -> None:
        # Without SSH, a refused LAN connection is all doctor knows about SMB.
        unreachable = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 10.0.0.2: NT_STATUS_HOST_UNREACHABLE",
            {"attempts": []},
        )
        run = self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            extra_patches={"timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": mock.Mock(return_value=unreachable)},
        )

        result = next(result for result in run.results if result.details.get("code") == "smb_listing_host_unreachable")
        self.assertEqual((result.status, result.domain), ("FAIL", "computer"))
        self.assertTrue(run.fatal)

    def test_run_doctor_checks_fails_the_device_when_its_own_smb_listing_fails(self) -> None:
        # The listing through SSH reaches smbd on the device's loopback: its
        # failure is the device's, whatever this computer's LAN listing says.
        tunnel_failure = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 127.0.0.1: NT_STATUS_BAD_NETWORK_NAME",
            {"attempts": []},
        )
        listing_mock = mock.Mock(side_effect=[tunnel_failure, self.smb_listing_result()])
        file_ops_mock = mock.Mock(return_value=[])

        run = self.run_doctor_with_mocks(
            real_device_smb=True,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertTrue(run.fatal)
        failure = next(result for result in run.results if result.message == tunnel_failure.message)
        self.assertEqual(failure.domain, "device")
        file_ops_mock.assert_not_called()
        sections = doctor_report(run.results)["sections"]
        self.assertEqual(sections["device"]["status"], "fail")
        self.assertEqual(sections["computer"]["status"], "pass")

    def test_check_authenticated_smb_file_ops_detailed_reports_each_step(self) -> None:
        def fake_run_local_capture(args, timeout=15, **kwargs):
            self.assertEqual(args[0], "smbclient")
            self.assertEqual(args[1:3], ["-s", "/dev/null"])
            command_text = args[-1]
            if 'get ".sample.txt"' in command_text:
                download_target = Path(command_text.split('get ".sample.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get ".sample-renamed.txt"' in command_text and 'get ".sample-copy.txt"' in command_text:
                renamed_target = Path(command_text.split('get ".sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                copy_target = Path(command_text.split('get ".sample-copy.txt" "', 1)[1].split('"', 1)[0])
                renamed_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                copy_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get ".sample-renamed.txt"' in command_text:
                download_target = Path(command_text.split('get ".sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get ".sample-copy.txt"' in command_text:
                download_target = Path(command_text.split('get ".sample-copy.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'del ".sample-copy.txt"; ls' in command_text:
                return subprocess.CompletedProcess(args, 0, ".sample-renamed.txt\n", "")
            if command_text == "ls":
                return subprocess.CompletedProcess(args, 0, "Public\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")
        self.assertEqual([result.status for result in results], ["PASS"] * 10)
        self.assertEqual(
            [result.message for result in results],
            [
                "SMB directory create works for admin@server.local/Data",
                "SMB file create works for admin@server.local/Data",
                "SMB file overwrite/edit works for admin@server.local/Data",
                "SMB file read works for admin@server.local/Data",
                "SMB file rename works for admin@server.local/Data",
                "SMB file copy works for admin@server.local/Data",
                "SMB file delete works for admin@server.local/Data",
                "SMB directory ls list works for admin@server.local/Data",
                "SMB directory delete works for admin@server.local/Data",
                "SMB final cleanup check passed for admin@server.local/Data",
            ],
        )

    def test_check_authenticated_smb_file_ops_detailed_reports_initial_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbclient"], timeout=20),
            ):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(results[0].message, "SMB directory create timed out for admin@server.local/Data")

    def test_check_authenticated_smb_file_ops_detailed_preserves_passes_before_later_timeout(self) -> None:
        def fake_run_local_capture(args, timeout=15, **kwargs):
            command_text = args[-1]
            if 'get ".sample.txt"' in command_text:
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        self.assertEqual(
            [(result.status, result.message) for result in results],
            [
                ("PASS", "SMB directory create works for admin@server.local/Data"),
                ("PASS", "SMB file create works for admin@server.local/Data"),
                ("PASS", "SMB file overwrite/edit works for admin@server.local/Data"),
                ("FAIL", "SMB file read timed out for admin@server.local/Data"),
            ],
        )

    def test_check_authenticated_smb_file_ops_detailed_surfaces_nt_status_over_smb1_fallback_noise(self) -> None:
        # smbclient prints the real NT status on stdout and misleading SMB1
        # fallback noise as the last stderr line; the failure message must keep
        # the NT status and the details must preserve both streams.
        nt_status_line = "NT_STATUS_INVALID_PARAMETER opening remote file .sample.txt"
        smb1_noise = "smb1cli_req_writev_submit: called for dialect[SMB3_11] server[server.local]"

        def fake_run_local_capture(args, timeout=15, **kwargs):
            command_text = args[-1]
            if 'put "' in command_text:
                return subprocess.CompletedProcess(args, 1, f"{nt_status_line}\n", f"session setup ok\n{smb1_noise}\n")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        self.assertEqual(
            [(result.status, result.message) for result in results],
            [
                ("PASS", "SMB directory create works for admin@server.local/Data"),
                ("FAIL", f"SMB file create failed: {nt_status_line}"),
            ],
        )
        failure = results[-1]
        self.assertEqual(failure.details["returncode"], 1)
        self.assertEqual(failure.details["stdout_tail"], nt_status_line)
        self.assertEqual(failure.details["stderr_tail"], f"session setup ok\n{smb1_noise}")

    def test_check_authenticated_smb_file_ops_detailed_directory_create_failure_prefers_nt_status(self) -> None:
        def fake_run_local_capture(args, timeout=15, **kwargs):
            command_text = args[-1]
            if 'mkdir "' in command_text:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    "NT_STATUS_MEDIA_WRITE_PROTECTED making remote directory\n",
                    "smb1cli_req_writev_submit: called for dialect[SMB3_11] server[server.local]\n",
                )
            self.fail(f"unexpected smbclient invocation after mkdir failure: {command_text}")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "FAIL")
        self.assertEqual(
            results[0].message,
            "SMB directory create failed: NT_STATUS_MEDIA_WRITE_PROTECTED making remote directory",
        )
        self.assertEqual(results[0].details["returncode"], 1)
        self.assertIn("stderr_tail", results[0].details)

    def test_check_authenticated_smb_file_ops_detailed_failure_without_nt_status_uses_last_line(self) -> None:
        def fake_run_local_capture(args, timeout=15, **kwargs):
            command_text = args[-1]
            if 'put "' in command_text:
                return subprocess.CompletedProcess(args, 1, "", "first noise line\nfinal error line\n")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        failure = results[-1]
        self.assertEqual(failure.status, "FAIL")
        self.assertEqual(failure.message, "SMB file create failed: final error line")
        self.assertNotIn("stdout_tail", failure.details)
        self.assertEqual(failure.details["stderr_tail"], "first noise line\nfinal error line")

    def test_check_authenticated_smb_file_ops_detailed_failure_without_output_reports_returncode(self) -> None:
        def fake_run_local_capture(args, timeout=15, **kwargs):
            command_text = args[-1]
            if 'put "' in command_text:
                return subprocess.CompletedProcess(args, 3, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "server.local", "Data")

        failure = results[-1]
        self.assertEqual(failure.status, "FAIL")
        self.assertEqual(failure.message, "SMB file create failed: failed with rc=3")
        self.assertEqual(failure.details, {"returncode": 3})

    def test_check_authenticated_smb_listing_uses_neutral_smbclient_config(self) -> None:
        captured_args = None
        captured_env = None

        def fake_run_local_capture(args, timeout=20, **kwargs):
            nonlocal captured_args
            nonlocal captured_env
            captured_args = args
            captured_env = kwargs.get("env")
            return subprocess.CompletedProcess(args, 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing("admin", "pw", "server.local")
        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(captured_args)
        self.assertEqual(captured_args[:3], ["smbclient", "-s", "/dev/null"])
        self.assertIsInstance(captured_env, dict)
        self.assertNotIn("KRB5CCNAME", captured_env)
        self.assertNotIn("DYLD_LIBRARY_PATH", captured_env)

    def test_check_authenticated_smb_listing_places_custom_port_before_dash_l_target(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20, **kwargs):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "127.0.0.1",
                    port=1445,
                )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            captured_args,
            ["smbclient", "-s", "/dev/null", "-g", "-p", "1445", "-I", "127.0.0.1", "-L", "//127.0.0.1", "-U", "admin%pw"],
        )

    def test_check_authenticated_smb_listing_can_pin_connect_address(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20, **kwargs):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    SmbClientTarget("server.local", "192.168.1.217"),
                )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["server"], "server.local")
        self.assertEqual(result.details["ip_address"], "192.168.1.217")
        self.assertEqual(
            captured_args,
            ["smbclient", "-s", "/dev/null", "-g", "-I", "192.168.1.217", "-L", "//server.local", "-U", "admin%pw"],
        )

    def test_authenticated_smb_listing_passes_a_scoped_server_unbracketed(self) -> None:
        # smbclient fails on [fe80::…%en0] and lists shares for the bare form.
        captured_args = None

        def fake_run_local_capture(args, timeout=30, **kwargs):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    SmbClientTarget("fe80::1%en0", "fe80::1%en0"),
                )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            captured_args,
            ["smbclient", "-s", "/dev/null", "-g", "-I", "fe80::1%en0", "-L", "//fe80::1%en0", "-U", "admin%pw"],
        )

    def test_check_authenticated_smb_listing_forwards_custom_port_for_server_lists(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=30, **kwargs):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Disk|Data|\nDisk|Public|\nIPC|IPC$|IPC Service (Samba)\nSMB1 disabled -- no workgroup available\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing("admin", "pw", ["127.0.0.1"], port=2445)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(captured_args[3:6], ["-g", "-p", "2445"])

    def test_parse_nbns_response_reads_single_answer_ipv4(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9"
        )
        self.assertEqual(parse_nbns_response(packet), NbnsResponse(0, ("192.168.1.217",)))

    def test_parse_nbns_response_reads_every_apple_interface_entry(self) -> None:
        # Apple's wcifsnd in router mode: no question, the full owner name,
        # TTL 0 and one NB entry per registered interface, WAN first.
        for entries in (["203.0.113.5", "10.0.1.1"], ["203.0.113.5", "10.0.1.1", "172.16.42.1"]):
            with self.subTest(entries=entries):
                rdata = b"".join(b"\x00\x00" + socket.inet_aton(address) for address in entries)
                packet = (
                    b"\x13\x37\x85\x00\x00\x00\x00\x01\x00\x00\x00\x00"
                    + _nbns_owner_name("TimeCapsule")
                    + b"\x00\x20\x00\x01\x00\x00\x00\x00" + struct.pack("!H", len(rdata)) + rdata
                )
                self.assertEqual(parse_nbns_response(packet), NbnsResponse(0, tuple(entries)))

    def test_parse_nbns_response_rejects_bad_rdlength(self) -> None:
        for rdlength, rdata in ((0, b""), (7, b"\x00" * 7), (12, b"\x00" * 6)):
            with self.subTest(rdlength=rdlength):
                packet = (
                    b"\x13\x37\x85\x00\x00\x00\x00\x01\x00\x00\x00\x00"
                    + _nbns_owner_name("TimeCapsule")
                    + b"\x00\x20\x00\x01\x00\x00\x00\x00" + struct.pack("!H", rdlength) + rdata
                )
                self.assertIsNone(parse_nbns_response(packet))

    def test_parse_nbns_response_rejects_non_query_opcode_and_requests(self) -> None:
        answer = (
            _nbns_owner_name("TimeCapsule")
            + b"\x00\x20\x00\x01\x00\x00\x00\x00\x00\x06\x00\x00\xc0\xa8\x01\xd9"
        )
        self.assertIsNone(parse_nbns_response(b"\x13\x37\xad\x00\x00\x00\x00\x01\x00\x00\x00\x00" + answer))
        self.assertIsNone(parse_nbns_response(b"\x13\x37\x05\x00\x00\x00\x00\x01\x00\x00\x00\x00" + answer))

    def test_parse_nbns_response_reports_negative_rcode(self) -> None:
        packet = b"\x13\x37\x85\x03\x00\x00\x00\x00\x00\x00\x00\x00" + _nbns_owner_name("TimeCapsule")
        self.assertEqual(parse_nbns_response(packet), NbnsResponse(3))

    def test_parse_nbns_response_returns_none_for_truncated_name(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA"
        )
        self.assertIsNone(parse_nbns_response(packet))

    def test_parse_nbns_response_returns_none_for_truncated_answer_header(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00"
        )
        self.assertIsNone(parse_nbns_response(packet))

    def test_build_nbns_query_has_expected_header_and_question(self) -> None:
        packet = build_nbns_query("TimeCapsule", transaction_id=0x1337)
        self.assertEqual(packet[:2], b"\x13\x37")
        self.assertEqual(packet[2:4], b"\x00\x00")
        self.assertEqual(packet[4:6], b"\x00\x01")
        self.assertEqual(packet[-4:], b"\x00\x20\x00\x01")

    def test_check_nbns_name_resolution_reports_timeout(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.side_effect = TimeoutError()
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out", result.message)

    def test_check_nbns_name_resolution_without_expected_ip_accepts_the_devices_answer(self) -> None:
        answer = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xda"
        )
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (answer, ("169.254.155.207", 137))
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "169.254.155.207", None)

        self.assertEqual(result.status, "PASS")
        self.assertIn("at 169.254.155.207 resolved to 192.168.1.218", result.message)
        fake_sock.sendto.assert_called_once_with(mock.ANY, ("169.254.155.207", 137))
        # With an expected address, the same answer is still a mismatch.
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "169.254.155.207", "169.254.155.207")
        self.assertEqual(result.status, "FAIL")

    def test_check_nbns_name_resolution_reports_success(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9",
            ("192.168.1.217", 137),
        )
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "PASS")
        self.assertIn("192.168.1.217", result.message)
        fake_sock.sendto.assert_called_once()

    def test_check_nbns_name_resolution_rejects_ipv6_expected_ip(self) -> None:
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket") as socket_mock:
            result = check_nbns_name_resolution("TimeCapsule", "fd00::217", "fd00::217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("NBNS only supports IPv4", result.message)
        socket_mock.assert_not_called()

    def test_check_nbns_name_resolution_names_the_address_it_rejects(self) -> None:
        cases = {
            ("fd00::217", None): "NBNS only supports IPv4 addresses, got fd00::217",
            ("192.168.1.217", "fd00::217"): "NBNS only supports IPv4 addresses, got fd00::217",
            ("capsule.local", None): "NBNS check address is invalid: capsule.local",
            ("192.168.1.217", "bogus"): "NBNS check address is invalid: bogus",
        }
        for (target, expected), message in cases.items():
            with self.subTest(target=target, expected=expected):
                with mock.patch("timecapsulesmb.checks.nbns.socket.socket") as socket_mock:
                    result = check_nbns_name_resolution("TimeCapsule", target, expected)
                self.assertEqual((result.status, result.message), ("FAIL", message))
                socket_mock.assert_not_called()

    def test_check_nbns_name_resolution_reports_wrong_ip(self) -> None:
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\x10",
            ("192.168.1.217", 137),
        )
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            result = check_nbns_name_resolution("TimeCapsule", "192.168.1.217", "192.168.1.217")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("resolved to 192.168.1.16", result.message)

    def _nbns_check_with_reply(self, packet: bytes, expected_ip: str = "10.0.1.1"):
        fake_sock = mock.Mock()
        fake_sock.recvfrom.return_value = (packet, (expected_ip, 137))
        with mock.patch("timecapsulesmb.checks.nbns.socket.socket", return_value=fake_sock):
            return check_nbns_name_resolution("TimeCapsule", expected_ip, expected_ip)

    def test_check_nbns_name_resolution_passes_router_mode_answer_listing_lan_second(self) -> None:
        rdata = b"\x00\x00" + socket.inet_aton("203.0.113.5") + b"\x00\x00" + socket.inet_aton("10.0.1.1")
        result = self._nbns_check_with_reply(
            b"\x13\x37\x85\x00\x00\x00\x00\x01\x00\x00\x00\x00" + _nbns_owner_name("TimeCapsule")
            + b"\x00\x20\x00\x01\x00\x00\x00\x00\x00\x0c" + rdata
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.message, "NBNS query for 'TimeCapsule' resolved to 10.0.1.1 (also lists 203.0.113.5)")

    def test_check_nbns_name_resolution_never_matches_ipv6_extension_answer(self) -> None:
        # An 18-byte IPv6 extension answer happens to be three 6-byte entries;
        # its bytes must never be read as the device's IPv4 address.
        result = self._nbns_check_with_reply(
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x12\x00\x00"
            + socket.inet_pton(socket.AF_INET6, "fd00::217"),
            expected_ip="192.168.1.217",
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("expected 192.168.1.217", result.message)

    def test_check_nbns_name_resolution_reports_negative_response(self) -> None:
        result = self._nbns_check_with_reply(
            b"\x13\x37\x85\x03\x00\x00\x00\x00\x00\x00\x00\x00" + _nbns_owner_name("TimeCapsule")
        )
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.message, "NBNS query for 'TimeCapsule' returned a negative response (rcode 3)")
        self.assertEqual(result.details, {"code": NBNS_NEGATIVE_RESPONSE_CODE, "rcode": 3})

    def test_check_nbns_name_resolution_reports_invalid_response(self) -> None:
        result = self._nbns_check_with_reply(b"\x13\x37\x85")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.message, "NBNS query for 'TimeCapsule' returned an invalid response")

    # Trimmed from `/sbin/ifconfig -a` on the NetBSD 6 and NetBSD 4 LAN devices.
    IFCONFIG_NETBSD6 = """bcmeth1: flags=ffffe802<BROADCAST,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	options=80000<LRO4>
	ether 80:ea:96:e6:58:68
bcmeth0: flags=ffffe943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	options=80000<LRO4>
	extra flag=1<NOINET6>
lo0: flags=ffff8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33188
	extra flag=0<>
	inet 127.0.0.1 netmask 0xff000000 
	inet6 ::1 prefixlen 128 
	inet6 fe80::1%lo0 prefixlen 64 scopeid 0x3 
bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	extra flag=2<PFQUICKPASS>
	ether 80:ea:96:e6:58:68
	inet6 fe80::82ea:96ff:fee6:5868%bridge0 prefixlen 64 scopeid 0x9 
	inet 192.168.1.218 netmask 0xffffff00 broadcast 192.168.1.255
	inet 169.254.155.207 netmask 0xffff0000 broadcast 169.254.255.255
	member: wlan0 flags=3<LEARNING,DISCOVER>
	member: bcmeth0 flags=3<LEARNING,DISCOVER>
bridge1: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	member: vlan0 flags=3<LEARNING,DISCOVER>
"""
    IFCONFIG_NETBSD4 = """mgi1: flags=e802<BROADCAST,SIMPLEX,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	ether e8:8d:28:58:f1:5c
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172
	inet 127.0.0.1 netmask 0xff000000 
bridge0: flags=e043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
	inet6 fe80::ea8d:28ff:fe58:f15c%bridge0 prefixlen 64 scopeid 0x9 
	inet 192.168.1.10 netmask 0xffffff00 broadcast 192.168.1.255
	inet6 2600:1700:83b7:20f:ea8d:28ff:fe58:f15c prefixlen 64 autoconf 
	inet 169.254.147.85 netmask 0xffff0000 broadcast 169.254.255.255
	member: wlan0 flags=3<LEARNING,DISCOVER>
bridge1: flags=e002<BROADCAST,LINK1,LINK2,MULTICAST> metric 0 mtu 1500
"""

    def test_parse_ifconfig_keeps_the_routable_lan_address_on_netbsd6(self) -> None:
        self.assertEqual(
            parse_ifconfig_ipv4_entries(self.IFCONFIG_NETBSD6),
            (DeviceIpv4Entry("bridge0", "192.168.1.218", "255.255.255.0", "192.168.1.255"),),
        )

    def test_parse_ifconfig_keeps_the_routable_lan_address_on_netbsd4(self) -> None:
        entries = parse_ifconfig_ipv4_entries(self.IFCONFIG_NETBSD4)
        self.assertEqual(entries, (DeviceIpv4Entry("bridge0", "192.168.1.10", "255.255.255.0", "192.168.1.255"),))
        self.assertEqual(entries[0].network, "192.168.1.0/24")

    def test_parse_ifconfig_keeps_a_link_local_address_when_it_is_the_only_one(self) -> None:
        text = (
            "bridge0: flags=e043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 169.254.147.85 netmask 0xffff0000 broadcast 169.254.255.255\n"
        )
        self.assertEqual(
            parse_ifconfig_ipv4_entries(text),
            (DeviceIpv4Entry("bridge0", "169.254.147.85", "255.255.0.0", "169.254.255.255"),),
        )

    def test_parse_ifconfig_skips_interfaces_wcifsnd_does_not_use(self) -> None:
        text = (
            "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172\n"
            "\tinet 127.0.0.1 netmask 0xff000000 broadcast 127.255.255.255\n"
            "bridge1: flags=e002<BROADCAST,LINK1,LINK2,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.2.1 netmask 0xffffff00 broadcast 10.0.2.255\n"
            "ppp0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> metric 0 mtu 1492\n"
            "\tinet 198.51.100.4 -> 198.51.100.1 netmask 0xffffffff\n"
        )
        self.assertEqual(parse_ifconfig_ipv4_entries(text), ())

    def test_parse_ifconfig_keeps_each_alias_as_its_own_entry(self) -> None:
        text = (
            "bridge0: flags=e043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255\n"
            "\tinet alias 10.0.5.1 netmask 0xffffff00 broadcast 10.0.5.255\n"
        )
        self.assertEqual(
            [entry.address for entry in parse_ifconfig_ipv4_entries(text)],
            ["10.0.1.1", "10.0.5.1"],
        )

    def test_parse_ifconfig_keeps_router_mode_wan_and_lan_in_interface_order(self) -> None:
        text = (
            "bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 203.0.113.7 netmask 0xffffff00 broadcast 203.0.113.255\n"
            "bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255\n"
        )
        self.assertEqual(
            [(entry.interface, entry.network) for entry in parse_ifconfig_ipv4_entries(text)],
            [("bcmeth1", "203.0.113.0/24"), ("bridge0", "10.0.1.0/24")],
        )

    def test_parse_ifconfig_reads_dotted_netmasks_and_derives_a_missing_broadcast(self) -> None:
        text = (
            "bridge0: flags=e043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 192.168.28.229 netmask 255.255.252.0\n"
        )
        self.assertEqual(
            parse_ifconfig_ipv4_entries(text),
            (DeviceIpv4Entry("bridge0", "192.168.28.229", "255.255.252.0", "192.168.31.255"),),
        )

    def test_parse_ifconfig_ignores_lines_it_cannot_read(self) -> None:
        text = (
            "bridge0: flags=e043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 999.1.1.1 netmask 0xffffff00 broadcast 999.1.1.255\n"
            "\tinet 10.0.0.2 netmask 0xnothex\n"
            "\tinet 10.0.0.3 netmask 0x00000000\n"
            "\tinet 0.0.0.0 netmask 0xffffff00\n"
            "\tinet 10.0.0.4 netmask 0xff00ff00\n"
            "\tinet 10.0.0.5\n"
            "\tinet6 fd00::2 prefixlen 64\n"
            "pass out proto udp from any to any port = domain keep state\n"
            "\tinet 10.9.9.9 netmask 0xffffff00 broadcast 10.9.9.255\n"
            "\tinet 10.0.0.6 netmask 0xffffff00 broadcast 10.0.0.255\n"
        )
        self.assertEqual(parse_ifconfig_ipv4_entries(text), ())

    def test_parse_ifconfig_follows_wcifsnd_and_drops_link_local_beside_zero(self) -> None:
        # No DHCP server: dhclient-script's 0.0.0.0 rules out the 169.254
        # address, and 0.0.0.0 itself is never answered on.
        no_lease = (
            "bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 0.0.0.0 netmask 0x0 broadcast 255.255.255.255\n"
            "\tinet alias 169.254.155.207 netmask 0xffff0000 broadcast 169.254.255.255\n"
        )
        self.assertEqual(parse_ifconfig_ipv4_entries(no_lease), ())
        self.assertEqual(parse_ifconfig_ipv4_entries(no_lease.replace("\tinet 0.0.0.0 netmask 0x0 broadcast 255.255.255.255\n", "")),
                         (DeviceIpv4Entry("bridge0", "169.254.155.207", "255.255.0.0", "169.254.255.255"),))
        # An address this parser cannot read still counts as another address.
        unreadable = no_lease.replace("inet 0.0.0.0 netmask 0x0 broadcast 255.255.255.255", "inet 10.0.0.3 netmask 0x00000000")
        self.assertEqual(parse_ifconfig_ipv4_entries(unreadable), ())
        # The rule is per interface: another interface's zero changes nothing here.
        two = (
            "bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 0.0.0.0 netmask 0x0 broadcast 255.255.255.255\n"
            "bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 169.254.155.207 netmask 0xffff0000 broadcast 169.254.255.255\n"
        )
        self.assertEqual([entry.address for entry in parse_ifconfig_ipv4_entries(two)], ["169.254.155.207"])

    def test_ipv4_address_kind_names_each_kind_without_the_address(self) -> None:
        cases = {
            "0.0.0.0": "zero",
            "127.0.0.1": "loopback",
            "169.254.147.85": "link_local",
            "10.0.1.1": "private",
            "172.16.42.1": "private",
            "192.168.1.10": "private",
            "203.0.113.7": "public",
            "8.8.8.8": "public",
            "999.1.1.1": None,
            "fe80::1": None,
            "": None,
        }
        for address, kind in cases.items():
            with self.subTest(address=address):
                self.assertEqual(ipv4_address_kind(address), kind)

    def test_parse_ifconfig_interface_ipv4_keeps_dhclients_zero_address_beside_link_local(self) -> None:
        # A device with no DHCP server: dhclient-script's PREINIT leaves
        # 0.0.0.0 on bridge0, link_local adds its 169.254 alias.
        text = (
            "bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,LINK1,LINK2,MULTICAST> metric 0 mtu 1500\n"
            "\tinet6 fe80::82ea:96ff:fee6:5868%bridge0 prefixlen 64 scopeid 0x9 \n"
            "\tinet 0.0.0.0 netmask 0x0 broadcast 255.255.255.255\n"
            "\tinet 169.254.155.207 netmask 0xffff0000 broadcast 169.254.255.255\n"
        )
        self.assertEqual(
            parse_ifconfig_interface_ipv4(text),
            (DeviceInterfaceIpv4("bridge0", "0xffffe043", ("zero", "link_local")),),
        )

    def test_parse_ifconfig_interface_ipv4_lists_up_interfaces_and_aliases_in_order(self) -> None:
        self.assertEqual(
            parse_ifconfig_interface_ipv4(self.IFCONFIG_NETBSD4),
            (DeviceInterfaceIpv4("bridge0", "0xe043", ("private", "link_local")),),
        )
        text = (
            "bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 203.0.113.7 netmask 0xffffff00 broadcast 203.0.113.255\n"
            "bridge0: flags=E043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255\n"
            "\tinet alias 10.0.5.1 netmask 0xffffff00 broadcast 10.0.5.255\n"
        )
        self.assertEqual(parse_ifconfig_interface_ipv4(text), (
            DeviceInterfaceIpv4("bcmeth1", "0xffffe843", ("public",)),
            DeviceInterfaceIpv4("bridge0", "0xe043", ("private", "private")),
        ))

    def test_parse_ifconfig_interface_ipv4_leaves_out_down_loopback_and_addressless_interfaces(self) -> None:
        text = (
            "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "bridge1: flags=e002<BROADCAST,LINK1,LINK2,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.2.1 netmask 0xffffff00 broadcast 10.0.2.255\n"
            "wlan0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
            "\tinet6 fe80::1%wlan0 prefixlen 64 scopeid 0x3\n"
            "pass out proto udp from any to any port = domain keep state\n"
            "\tinet 10.9.9.9 netmask 0xffffff00\n"
            "ppp0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> metric 0 mtu 1492\n"
            "\tinet 198.51.100.4 -> 198.51.100.1 netmask 0xffffffff\n"
            "\tinet 999.1.1.1 netmask 0xffffffff\n"
        )
        # ppp0 is UP and not loopback, so it is listed; its unreadable line is not.
        self.assertEqual(parse_ifconfig_interface_ipv4(text), (DeviceInterfaceIpv4("ppp0", "0x8051", ("public",)),))

    def test_link_plan_ipv4_kinds_reads_only_the_links_sharing_smb(self) -> None:
        plan = _parse_link_plan(router_plan(wan_ipv4="203.0.113.7"))
        self.assertEqual(link_plan_ipv4_kinds(plan), {"bridge0": ["private", "link_local"]})
        shared_wan = _parse_link_plan(router_plan(wan_ipv4="203.0.113.7", wan_mask="smb,adisk"))
        self.assertEqual(link_plan_ipv4_kinds(shared_wan), {"mgi1": ["public"], "bridge0": ["private", "link_local"]})
        zero_alias = _parse_link_plan(router_plan().replace(
            "addr: link=9 family=inet addr=10.0.1.1 prefix=24", "addr: link=9 family=inet addr=0.0.0.0 prefix=0"))
        self.assertEqual(link_plan_ipv4_kinds(zero_alias), {"bridge0": ["zero", "link_local"]})
        self.assertEqual(link_plan_ipv4_kinds(_parse_link_plan(router_plan(lan_mask="none"))), {})
        self.assertEqual(link_plan_ipv4_kinds(None), {})
        self.assertEqual(link_plan_ipv4_kinds({"links": "bad", "addresses": []}), {})

    def test_apple_nbns_client_on_subnet_follows_wcifsnd_broadcast_match(self) -> None:
        device = DeviceIpv4Entry("bridge0", "192.168.28.229", "255.255.255.0", "192.168.28.255")
        wide = DeviceIpv4Entry("bridge0", "192.168.28.229", "255.255.248.0", "192.168.31.255")
        wan = DeviceIpv4Entry("bcmeth1", "192.168.24.1", "255.255.255.0", "192.168.24.255")
        cases = (
            ((device,), "192.168.24.102", False),
            ((wide,), "192.168.24.102", True),
            ((device,), "192.168.28.17", True),
            ((device, wan), "192.168.24.102", True),
            ((), "192.168.24.102", False),
        )
        for entries, client, expected in cases:
            with self.subTest(entries=entries, client=client):
                self.assertEqual(apple_nbns_client_on_subnet(entries, client), expected)

    def test_apple_nbns_client_on_subnet_compares_the_stored_broadcast(self) -> None:
        # wcifsnd compares the interface's own broadcast, not one derived from its mask.
        odd = DeviceIpv4Entry("bridge0", "10.0.0.2", "255.255.255.0", "10.0.0.0")
        self.assertFalse(apple_nbns_client_on_subnet((odd,), "10.0.0.50"))

    def test_parse_ifconfig_networks_lists_both_families_on_both_devices(self) -> None:
        self.assertEqual(parse_ifconfig_networks(self.IFCONFIG_NETBSD6), ("192.168.1.0/24",))
        self.assertEqual(parse_ifconfig_networks(self.IFCONFIG_NETBSD4), ("192.168.1.0/24", "2600:1700:83b7:20f::/64"))

    def test_parse_ifconfig_networks_keeps_up_interfaces_and_drops_link_local_and_unreadable_lines(self) -> None:
        text = (
            "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 33172\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "\tinet6 ::1 prefixlen 128\n"
            "bcmeth1: flags=ffffe843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 203.0.113.7 netmask 0xffffff00 broadcast 203.0.113.255\n"
            "bridge0: flags=ffffe043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.1.1 netmask 0xffffff00 broadcast 10.0.1.255\n"
            "\tinet 169.254.147.85 netmask 0xffff0000 broadcast 169.254.255.255\n"
            "\tinet6 fe80::1%bridge0 prefixlen 64 scopeid 0x9\n"
            "\tinet6 fd00::2 prefixlen 64\n"
            "\tinet6 fd00::3 prefixlen 64\n"
            "\tinet6 nonsense prefixlen 64\n"
            "\tinet6 2001:db8::1 prefixlen 999\n"
            "bridge1: flags=e002<BROADCAST,LINK1,LINK2,MULTICAST> metric 0 mtu 1500\n"
            "\tinet 10.0.2.1 netmask 0xffffff00 broadcast 10.0.2.255\n"
            "\tinet6 2001:db8:2::1 prefixlen 64\n"
        )

        self.assertEqual(parse_ifconfig_networks(text), ("203.0.113.0/24", "10.0.1.0/24", "fd00::/64"))

    def test_probe_device_networks_reads_ifconfig_once_for_both_uses(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=subprocess.CompletedProcess([], 0, self.IFCONFIG_NETBSD4, ""),
        ) as run_ssh_mock:
            result = probe_device_networks_conn(connection)

        run_ssh_mock.assert_called_once()
        self.assertEqual(run_ssh_mock.call_args.args, (connection, "/sbin/ifconfig -a"))
        self.assertEqual(result.networks, ("192.168.1.0/24", "2600:1700:83b7:20f::/64"))
        self.assertEqual(
            result.ipv4_subnets,
            DeviceIpv4SubnetsProbeResult((DeviceIpv4Entry("bridge0", "192.168.1.10", "255.255.255.0", "192.168.1.255"),)),
        )

    def test_probe_device_networks_reports_errors_and_ipv6_only_devices(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        ipv6_only = (
            "bridge0: flags=e043<UP,BROADCAST,RUNNING,MULTICAST> metric 0 mtu 1500\n"
            "\tinet6 2001:db8:1::5 prefixlen 64\n"
        )
        cases = (
            (mock.Mock(side_effect=SshCommandTimeout("timed out")), DeviceNetworksProbeResult(error="ifconfig timed out")),
            (mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "")), DeviceNetworksProbeResult(error="ifconfig exited 1")),
            (mock.Mock(return_value=subprocess.CompletedProcess([], 0, ipv6_only, "")),
             DeviceNetworksProbeResult(networks=("2001:db8:1::/64",))),
        )
        for run_ssh_mock, expected in cases:
            with self.subTest(expected=expected):
                with mock.patch("timecapsulesmb.device.probe.run_ssh", run_ssh_mock):
                    result = probe_device_networks_conn(connection)
                self.assertEqual(result, expected)
                # NBNS keeps seeing the same errors as before.
                self.assertEqual(
                    result.ipv4_subnets.error,
                    expected.error or "ifconfig listed no broadcast IPv4 address",
                )

    def test_probe_device_ipv4_subnets_reads_ifconfig(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        with mock.patch(
            "timecapsulesmb.device.probe.run_ssh",
            return_value=subprocess.CompletedProcess([], 0, self.IFCONFIG_NETBSD6, ""),
        ) as run_ssh_mock:
            result = probe_device_networks_conn(connection).ipv4_subnets

        # /sbin is not on the device's ssh PATH.
        run_ssh_mock.assert_called_once()
        self.assertEqual(run_ssh_mock.call_args.args, (connection, "/sbin/ifconfig -a"))
        self.assertIsNone(result.error)
        self.assertEqual([entry.network for entry in result.entries], ["192.168.1.0/24"])

    def test_probe_device_ipv4_subnets_reports_errors(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "-o foo")
        cases = (
            (mock.Mock(return_value=subprocess.CompletedProcess([], 127, "", "not found")), "ifconfig exited 127"),
            (mock.Mock(side_effect=SshCommandTimeout("timed out")), "ifconfig timed out"),
            (mock.Mock(return_value=subprocess.CompletedProcess([], 0, self.IFCONFIG_NETBSD4.split("bridge0")[0], "")),
             "ifconfig listed no broadcast IPv4 address"),
        )
        for run_ssh_mock, error in cases:
            with self.subTest(error=error):
                with mock.patch("timecapsulesmb.device.probe.run_ssh", run_ssh_mock):
                    result = probe_device_networks_conn(connection).ipv4_subnets
                self.assertEqual(result, DeviceIpv4SubnetsProbeResult(error=error))

    def test_run_doctor_checks_checks_nbns_without_flash_preference(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": mock.Mock(return_value=CheckResult("PASS", "native nbns ok")),
            },
        )
        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if "native nbns ok" in result.message)
        self.assertEqual(nbns_result.status, "PASS")
        nbns_index = run.results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(run.results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)

    def test_run_doctor_checks_checks_nbns_automatically(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        nbns_mock = mock.Mock(return_value=mock.Mock(status="PASS", message="nbns ok"))
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            runtime_naming_identity=self.runtime_identity_from_values(values),
            skip_bonjour=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_usb_printer_conn": mock.Mock(return_value=UsbPrinterProbeResult(present=False, name=None)),
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
            },
        )
        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if result.message == "nbns ok")
        self.assertEqual(nbns_result.status, "PASS")
        nbns_index = run.results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(run.results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)
        nbns_mock.assert_called_once_with("TimeCapsule", "10.0.0.2", "10.0.0.2")

    def test_run_doctor_checks_uses_discovered_ipv4_for_hostname_target_nbns(self) -> None:
        values = {
            "TC_HOST": "root@timecapsule.local",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        nbns_mock = mock.Mock(return_value=mock.Mock(status="PASS", message="nbns ok"))
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_usb_printer_conn": mock.Mock(return_value=UsbPrinterProbeResult(present=False, name=None)),
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
            },
        )
        self.assertFalse(run.fatal)
        self.assertEqual(next(result for result in run.results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "10.0.0.2", "10.0.0.2")

    def test_run_doctor_checks_uses_discovered_ipv4_instead_of_ssh_hostname_for_nbns(self) -> None:
        values = {
            "TC_HOST": "root@wan.example.com",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        nbns_mock = mock.Mock(return_value=mock.Mock(status="PASS", message="nbns ok"))
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            runtime_naming_identity=self.runtime_identity_from_values(values),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_usb_printer_conn": mock.Mock(return_value=UsbPrinterProbeResult(present=False, name=None)),
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
            },
        )
        self.assertFalse(run.fatal)
        self.assertEqual(next(result for result in run.results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "10.0.0.2", "10.0.0.2")

    def test_run_doctor_checks_nbns_prefers_non_link_local_reachable_ipv4(self) -> None:
        nbns_mock = mock.Mock(return_value=mock.Mock(status="PASS", message="nbns ok"))
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(
                    ("169.254.1.2", "10.0.0.2")
                ),
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertEqual(
            nbns_mock.call_args_list,
            [
                mock.call("TimeCapsule", "10.0.0.2", "10.0.0.2"),
            ],
        )

    def test_run_doctor_checks_nbns_queries_a_link_local_only_address_for_any_answer(self) -> None:
        # The Mac reaches the device only at its 169.254 address (another IPv4
        # subnet of the same wire); the device answers with its LAN address.
        nbns_mock = mock.Mock(return_value=mock.Mock(status="PASS", message="nbns ok"))
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(
                    ("169.254.1.2",)
                ),
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertEqual(nbns_mock.call_args_list, [mock.call("TimeCapsule", "169.254.1.2", None)])

    def test_run_doctor_checks_pins_authenticated_smb_to_runtime_addresses(self) -> None:
        listing_result_v4 = CheckResult(
            "PASS",
            "authenticated SMB listing works for admin@timecapsulesamba4.local via 10.0.0.2",
            {
                "server": "timecapsulesamba4.local",
                "ip_address": "10.0.0.2",
                "disk_shares": ["Data"],
                "attempts": [],
            },
        )
        listing_result_v6 = CheckResult(
            "PASS",
            "authenticated SMB listing works for admin@timecapsulesamba4.local via fd00::2",
            {
                "server": "timecapsulesamba4.local",
                "ip_address": "fd00::2",
                "disk_shares": ["Data"],
                "attempts": [],
            },
        )
        listing_mock = mock.Mock(side_effect=[listing_result_v4, listing_result_v6])
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertEqual(
            [call.args[2] for call in listing_mock.call_args_list],
            [
                [SmbClientTarget("timecapsulesamba4.local", "10.0.0.2")],
                [SmbClientTarget("timecapsulesamba4.local", "fd00::2")],
            ],
        )
        self.assertTrue(all(call.kwargs["port"] == 445 for call in listing_mock.call_args_list))
        # SSH works, so the device changes files itself; the LAN only lists.
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_warns_when_only_routable_ipv6_authenticated_listing_fails(self) -> None:
        listing_v4 = CheckResult(
            "PASS",
            "authenticated SMB listing works over IPv4",
            {
                "server": "timecapsulesamba4.local",
                "ip_address": "10.0.0.2",
                "disk_shares": ["Data"],
                "attempts": [],
            },
        )
        listing_v6 = CheckResult(
            "FAIL",
            "authenticated SMB listing failed over IPv6: NT_STATUS_INVALID_NETWORK_RESPONSE",
            {
                "attempts": [
                    {
                        "server": "timecapsulesamba4.local",
                        "ip_address": "fd00::2",
                        "outcome": "error",
                        "failure": "NT_STATUS_INVALID_NETWORK_RESPONSE",
                    }
                ]
            },
        )
        listing_mock = mock.Mock(side_effect=[listing_v4, listing_v6])
        file_ops_mock = mock.Mock(return_value=[CheckResult("PASS", "file ops ok")])

        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        ipv6_warning = next(result for result in run.results if "authenticated SMB IPv6 listing failed" in result.message)
        self.assertEqual(ipv6_warning.status, "WARN")
        self.assertEqual(listing_mock.call_count, 2)
        # SSH works, so the device changes files itself; the LAN only lists.
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_uses_working_ipv6_when_client_has_no_ipv4_route(self) -> None:
        routes = {
            "10.0.0.2": RouteSelection("unavailable", error="no route", error_number=51),
            "fd00::2": RouteSelection("available", source="fd00::9"),
        }
        port_mock = mock.Mock(return_value=CheckResult("PASS", "SMB reachable at fd00::2:445"))
        listing_result = CheckResult(
            "PASS",
            "authenticated SMB listing works over IPv6",
            {
                "server": "timecapsulesamba4.local",
                "ip_address": "fd00::2",
                "disk_shares": ["Data"],
                "attempts": [],
            },
        )
        listing_mock = mock.Mock(return_value=listing_result)
        file_ops_mock = mock.Mock(return_value=[CheckResult("PASS", "file ops ok")])

        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            smb_port=REAL_SMB_PORT_CHECK,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.select_route_to_address": mock.Mock(side_effect=routes.__getitem__),
                "timecapsulesmb.checks.doctor_steps.check_smb_port": port_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        port_mock.assert_called_once_with("fd00::2")
        self.assertEqual(
            listing_mock.call_args.args[2],
            [SmbClientTarget("timecapsulesamba4.local", "fd00::2")],
        )
        # SSH works, so the device changes files itself; the LAN only lists.
        file_ops_mock.assert_not_called()

    def test_run_doctor_checks_passes_ipv6_only_listing_when_smbclient_exits_1_after_the_table(self) -> None:
        # The only address this computer reaches is IPv6, and the device's name
        # has no IPv4 address here: smbclient -L lists the shares, then exits
        # 1 for the workgroup step. The listing worked.
        routes = {
            "10.0.0.2": RouteSelection("unavailable", error="no route", error_number=51),
            "fd00::2": RouteSelection("available", source="fd00::9"),
        }
        smbclient_calls: list[list[str]] = []

        def fake_smbclient(args, timeout=20, **kwargs):
            smbclient_calls.append(args)
            return subprocess.CompletedProcess(
                args,
                1,
                "Disk|Data|\nIPC|IPC$|IPC Service (Samba)\n"
                "timecapsulesamba4.local is an IPv6 address -- no workgroup available\n",
                "",
            )

        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            smb_port=REAL_SMB_PORT_CHECK,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.discover_smb_services_detailed": self.dual_stack_discovery(),
                "timecapsulesmb.checks.doctor_steps.select_route_to_address": mock.Mock(side_effect=routes.__getitem__),
                "timecapsulesmb.checks.doctor_steps.check_smb_port": mock.Mock(
                    return_value=CheckResult("PASS", "SMB reachable at fd00::2:445"),
                ),
                "timecapsulesmb.checks.smb.command_exists": mock.Mock(return_value=True),
                "timecapsulesmb.checks.smb.run_local_capture": mock.Mock(side_effect=fake_smbclient),
            },
        )

        self.assertFalse(run.fatal)
        self.assertEqual(len(smbclient_calls), 1)
        self.assertEqual(smbclient_calls[0][-4:-2], ["-L", "//timecapsulesamba4.local"])
        self.assertIn("fd00::2", smbclient_calls[0])
        listing = next(result for result in run.results if result.message.startswith("authenticated SMB listing"))
        self.assertEqual(
            (listing.status, listing.message),
            ("PASS", "authenticated SMB listing works for admin@timecapsulesamba4.local via fd00::2"),
        )
        self.assertEqual(listing.details["disk_shares"], ["Data"])
        self.assertFalse([result for result in run.results if result.status != "PASS" and "SMB listing" in result.message])


    def test_run_doctor_checks_changes_files_over_the_lan_pinned_to_the_listed_address_without_ssh(self) -> None:
        # Without SSH nothing checks file operations on the device, so this
        # computer runs them over the LAN, on the address its listing used.
        listing_result = CheckResult(
            "PASS",
            "authenticated SMB listing works for admin@timecapsulesamba4.local via 10.0.0.2",
            {"server": "timecapsulesamba4.local", "ip_address": "10.0.0.2", "disk_shares": ["Data"], "attempts": []},
        )
        file_ops_mock = mock.Mock(return_value=[CheckResult("PASS", "file ops ok")])

        run = self.run_doctor_with_mocks(
            skip_ssh=True,
            skip_bonjour=True,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": mock.Mock(return_value=listing_result),
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        run.mocks.device_smb.assert_not_called()
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
            port=445,
            ip_address="10.0.0.2",
        )
        file_ops_result = next(result for result in run.results if result.message == "file ops ok")
        self.assertEqual(file_ops_result.domain, "computer")
    def test_run_doctor_checks_browses_once_and_evaluates_both_families(self):
        run, _debug, browse, _diagnostics = self.run_selected_bonjour(self.selected_snapshot())
        self.assertFalse(run.fatal)
        browse.assert_called_once()
        self.assertEqual(browse.call_args.kwargs["target_ip"], "10.0.0.2")
        self.assertTrue(browse.call_args.kwargs["include_related"])
        self.assertTrue(any("Bonjour IPv4: resolved Bonjour host home.local" in r.message for r in run.results))
        self.assertTrue(any("Bonjour IPv6: resolved Bonjour host home.local" in r.message for r in run.results))

    def test_run_doctor_checks_warns_when_nbns_query_fails(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": mock.Mock(
                    side_effect=RuntimeError("NBNS query failed")
                ),
            },
        )
        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("NBNS query failed", nbns_result.message)

    def test_run_doctor_checks_warns_when_nbns_query_raises_transport_error(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o foo",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": mock.Mock(side_effect=SshError("ssh failed")),
            },
        )
        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("ssh failed", nbns_result.message)

    def test_check_authenticated_smb_file_ops_detailed_passes_custom_port_to_smbclient(self) -> None:
        captured_args: list[list[str]] = []

        def fake_run_local_capture(args, timeout=15, **kwargs):
            captured_args.append(args)
            command_text = args[-1]
            if 'get ".sample.txt"' in command_text:
                download_target = Path(command_text.split('get ".sample.txt" "', 1)[1].split('"', 1)[0])
                download_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get ".sample-renamed.txt"' in command_text and 'get ".sample-copy.txt"' in command_text:
                renamed_target = Path(command_text.split('get ".sample-renamed.txt" "', 1)[1].split('"', 1)[0])
                copy_target = Path(command_text.split('get ".sample-copy.txt" "', 1)[1].split('"', 1)[0])
                renamed_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                copy_target.write_text("line1\nline2\nline3\nline4-updated\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'del ".sample-copy.txt"; ls' in command_text:
                return subprocess.CompletedProcess(args, 0, ".sample-renamed.txt\n", "")
            if command_text == "ls":
                return subprocess.CompletedProcess(args, 0, "Public\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed("admin", "pw", "127.0.0.1", "Data", port=3445)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(args[:5] == ["smbclient", "-s", "/dev/null", "-p", "3445"] for args in captured_args))

    def test_check_authenticated_smb_file_ops_detailed_can_pin_connect_address(self) -> None:
        captured_args: list[list[str]] = []

        def fake_run_local_capture(args, timeout=15, **kwargs):
            captured_args.append(args)
            command_text = args[-1]
            if 'get ".sample.txt"' in command_text:
                Path(command_text.split('get ".sample.txt" "', 1)[1].split('"', 1)[0]).write_text(
                    "line1\nline2\nline3\nline4-updated\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'get ".sample-renamed.txt"' in command_text and 'get ".sample-copy.txt"' in command_text:
                Path(command_text.split('get ".sample-renamed.txt" "', 1)[1].split('"', 1)[0]).write_text(
                    "line1\nline2\nline3\nline4-updated\n",
                    encoding="utf-8",
                )
                Path(command_text.split('get ".sample-copy.txt" "', 1)[1].split('"', 1)[0]).write_text(
                    "line1\nline2\nline3\nline4-updated\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args, 0, "", "")
            if 'del ".sample-copy.txt"; ls' in command_text:
                return subprocess.CompletedProcess(args, 0, ".sample-renamed.txt\n", "")
            if command_text == "ls":
                return subprocess.CompletedProcess(args, 0, "Public\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                results = check_authenticated_smb_file_ops_detailed(
                    "admin",
                    "pw",
                    "server.local",
                    "Data",
                    ip_address="fd00::217",
                )
        self.assertEqual(len(results), 10)
        self.assertTrue(all(args[:5] == ["smbclient", "-s", "/dev/null", "-I", "fd00::217"] for args in captured_args))

    # The disks MaSt lists: diskd's SMART verdict and each volume's space.

    DISK_CODES = {DOCTOR_CODE_DISK_INFO, DOCTOR_CODE_DISK_SMART_FAILING, DOCTOR_CODE_DISK_INFO_UNAVAILABLE}

    def give_mast(self, stdout: str, *, returncode: int = 0, stderr: str = "") -> None:
        self._mast_read.side_effect = None
        self._mast_read.return_value = subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)

    def disk_rows(self, run) -> list[CheckResult]:
        return [result for result in run.results if result.details.get("code") in self.DISK_CODES]

    def run_healthy_doctor(self, **kwargs):
        return self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"), skip_bonjour=True, skip_smb=True, **kwargs,
        )

    @staticmethod
    def disk(*, builtin=True, smart_status="verified", label="Disk 1", partitions=None) -> MaStDiskSnapshot:
        if partitions is None:
            partitions = (MaStPartitionSnapshot("dk2", "Data", "hfs", 1905681, 8977, 1896704),)
        return MaStDiskSnapshot("sd0", "", None, builtin, partitions, label=label, smart_status=smart_status)

    def test_disk_row_says_smart_status_and_space(self) -> None:
        cases = {
            "internal, verified": (
                self.disk(), "INFO", DOCTOR_CODE_DISK_INFO,
                "Disk 1 (internal): SMART verified. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "internal, no status": (
                self.disk(smart_status=None), "INFO", DOCTOR_CODE_DISK_INFO,
                "Disk 1 (internal): SMART not reported. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            # diskd checks only the built-in disk, so a USB disk says so too.
            "USB, no status": (
                self.disk(builtin=False, smart_status=None, label="Disk 2"), "INFO", DOCTOR_CODE_DISK_INFO,
                "Disk 2 (USB): SMART not reported. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "USB that reports": (
                self.disk(builtin=False, label="Disk 2"), "INFO", DOCTOR_CODE_DISK_INFO,
                "Disk 2 (USB): SMART verified. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "a value diskd has not been seen to give": (
                self.disk(smart_status="unknownStatus"), "INFO", DOCTOR_CODE_DISK_INFO,
                "Disk 1 (internal): SMART unknownStatus. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "failing": (
                self.disk(smart_status="failing"), "WARN", DOCTOR_CODE_DISK_SMART_FAILING,
                "Disk 1 (internal) reports that it is failing (SMART); copy off the files you need and replace "
                "the disk. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "USB failing": (
                self.disk(builtin=False, smart_status="failing", label="Disk 2"), "WARN", DOCTOR_CODE_DISK_SMART_FAILING,
                "Disk 2 (USB) reports that it is failing (SMART); copy off the files you need and replace "
                "the disk. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
            "no label": (
                self.disk(label=None), "INFO", DOCTOR_CODE_DISK_INFO,
                "sd0 (internal): SMART verified. Data: 9.4 GB used, 2.0 TB free of 2.0 TB",
            ),
        }
        for case, (disk, status, code, message) in cases.items():
            with self.subTest(case):
                result = _disk_health_result(disk)
                self.assertEqual((result.status, result.details["code"], result.message), (status, code, message))
                self.assertEqual(
                    {key: result.details[key] for key in ("disk", "builtin", "smart_status")},
                    {"disk": "sd0", "builtin": disk.builtin, "smart_status": disk.smart_status},
                )

    def test_disk_row_lists_every_volume_with_space_whatever_its_format(self) -> None:
        partitions = (
            MaStPartitionSnapshot("dk2", "Data", "hfs", 1905681, 1392180, 513501),
            # FAT32 USB disks mount through mount_msdos.
            MaStPartitionSnapshot("dk3", "", "msdos", 15000, 1000, 14000),
            # Not mounted: diskd reports no space, or none at all.
            MaStPartitionSnapshot("dk4", "Gone", "hfs", 1000, 0, 0),
            MaStPartitionSnapshot("dk5", "No sizes", "hfs"),
            MaStPartitionSnapshot("dk6", "No size", "hfs", None, 1, 1),
            MaStPartitionSnapshot("dk7", "Zero size", "hfs", 0, 1, 1),
        )
        self.assertEqual(
            _disk_health_result(self.disk(partitions=partitions)).message,
            "Disk 1 (internal): SMART verified. Data: 1.5 TB used, 538.4 GB free of 2.0 TB; "
            "dk3: 1.0 GB used, 14.7 GB free of 15.7 GB",
        )

    def test_disk_row_without_space_ends_at_its_smart_status(self) -> None:
        for partitions in ((), (MaStPartitionSnapshot("dk2", "Data", "hfs", 1000, 0, 0),)):
            with self.subTest(partitions=partitions):
                self.assertEqual(_disk_health_result(self.disk(partitions=partitions)).message,
                                 "Disk 1 (internal): SMART verified")

    def test_decimal_size_picks_the_unit_finder_would(self) -> None:
        self.assertEqual(
            [_decimal_size(mib) for mib in (0, 1, 900, 953, 954, 953674, 953675, 1905681)],
            # Just under a unit rounds up into it, never to "1000.0".
            ["0.0 MB", "1.0 MB", "943.7 MB", "1.0 GB", "1.0 GB", "1.0 TB", "1.0 TB", "2.0 TB"],
        )

    def test_doctor_lists_each_disk_mast_reports(self) -> None:
        self.give_mast(test_storage_runtime.netbsd6_internal_and_usb_xml())
        debug_fields: dict[str, object] = {}
        run = self.run_healthy_doctor(debug_fields=debug_fields)

        rows = self.disk_rows(run)
        self.assertEqual([(row.status, row.message) for row in rows], [
            ("INFO", "Disk 1 (internal): SMART verified. AirPort Disk: 1.5 TB used, 538.4 GB free of 2.0 TB"),
            ("INFO", "Disk 2 (USB): SMART not reported. 4TB: 77.6 GB used, 3.9 TB free of 4.0 TB"),
        ])
        self.assertTrue(all(row.domain == "device" for row in rows))
        self.assertEqual([(row.details["disk"], row.details["builtin"]) for row in rows], [("wd0", True), ("sd0", False)])
        # One bounded read of the XML form, whose strings are escaped, unlike
        # acp's text form; it never asks ACPd anything else.
        self._mast_read.assert_called_once_with(
            mock.ANY, MAST_ACP_COMMAND, check=False, timeout=MAST_PROBE_TIMEOUT_SECONDS,
        )
        # Telemetry gets every disk whole: label, model, firmware, diskd's
        # state, and each volume's name and sizes. Never the serial number.
        health = debug_fields["disk_health"]
        self.assertIsNone(health["error"])
        self.assertEqual(
            [(disk["device"], disk["label"], disk["builtin"], disk["smart_status"], disk["model"], disk["revision"],
              disk["soft_disconnected"]) for disk in health["disks"]],
            [("wd0", "Disk 1", True, "verified", "PALP EDH DTS0200MD00 1", "PA45", False),
             ("sd0", "Disk 2", False, None, "WD", "1014", False)],
        )
        self.assertEqual(health["disks"][0]["partitions"][0], {
            "device": "dk2", "name": "AirPort Disk", "format": "hfs",
            "size_mib": 1905681, "used_mib": 1392180, "free_mib": 513501, "users": 1,
            "uuid": "0a000000-0000-0000-0000-000000000002",
        })
        self.assertNotIn("SERIAL6", json.dumps(health))

    def test_doctor_reads_mast_behind_lines_the_ssh_client_printed(self) -> None:
        self.give_mast("Warning: Permanently added '10.0.0.2' (RSA) to the list of known hosts.\n"
                       + test_storage_runtime.netbsd6_internal_and_usb_xml())
        rows = self.disk_rows(self.run_healthy_doctor())
        self.assertEqual([row.details["code"] for row in rows], [DOCTOR_CODE_DISK_INFO, DOCTOR_CODE_DISK_INFO])

    def test_doctor_lists_no_disks_for_a_device_without_any(self) -> None:
        # An AirPort Extreme with nothing plugged in: acp prints an empty array.
        debug_fields: dict[str, object] = {}
        run = self.run_healthy_doctor(debug_fields=debug_fields)
        self.assertEqual(self.disk_rows(run), [])
        self._mast_read.assert_called_once()
        self.assertEqual(debug_fields["disk_health"], {"error": None, "disks": []})

    def test_a_failing_disk_warns_without_failing_the_checkup(self) -> None:
        baseline = self.run_healthy_doctor()
        self.give_mast(test_storage_runtime.netbsd6_internal_and_usb_xml("failing"))
        run = self.run_healthy_doctor()

        self.assertEqual([row.status for row in self.disk_rows(run)], ["WARN", "INFO"])
        self.assertEqual(run.fatal, baseline.fatal)
        self.assertIn(DOCTOR_CODE_DISK_SMART_FAILING, doctor_report(run.results)["sections"]["device"]["warn_codes"])

    def test_doctor_says_when_mast_cannot_be_read_and_goes_on(self) -> None:
        prefix = "Attempt to read Apple ACPd's MaSt to get disk information failed: "
        failed_get = "### get 'MaSt' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>"
        ssh_warning = "Warning: Permanently added '10.0.0.2' (RSA) to the list of known hosts."
        cases = {
            "transport error": (dict(side_effect=SshError("connection reset")), "connection reset"),
            "timeout": (dict(side_effect=SshCommandTimeout("Timed out waiting for ssh command")),
                        "Timed out waiting for ssh command"),
            # acp's own text for a failed get, on stderr with exit 0; run_ssh
            # joins stderr into stdout, behind what the SSH client printed.
            "failed get": (dict(stdout=f"{ssh_warning}\n{failed_get}\n"), failed_get),
            "failed get on stderr": (dict(stdout="", stderr=f"{ssh_warning}\n{failed_get}\n"), failed_get),
            "failed get beside a non-zero exit": (dict(stdout=f"{failed_get}\nlater\n", returncode=1), failed_get),
            "exit status": (dict(stdout="", returncode=255), "exit 255: no output"),
            "exit status with output": (
                dict(stdout="ssh: connect to host 10.0.0.2 port 22: Connection refused\n", returncode=255),
                "exit 255: ssh: connect to host 10.0.0.2 port 22: Connection refused",
            ),
            "nothing printed": (dict(stdout="  \n"), "no output"),
            # Exit 0 without "MaSt=": acp printed no property.
            "only the SSH client spoke": (dict(stdout=f"{ssh_warning}\n"), ssh_warning),
        }
        for case, (probe, reason) in cases.items():
            with self.subTest(case):
                if "side_effect" in probe:
                    self._mast_read.side_effect = probe["side_effect"]
                else:
                    self.give_mast(**probe)
                debug_fields: dict[str, object] = {}
                run = self.run_healthy_doctor(debug_fields=debug_fields)

                rows = self.disk_rows(run)
                self.assertEqual([(row.status, row.details["code"], row.message) for row in rows],
                                 [("INFO", DOCTOR_CODE_DISK_INFO_UNAVAILABLE, prefix + reason)])
                self.assertEqual(debug_fields["disk_health"], {"error": reason, "disks": []})
                # The checks after it still ran.
                self.assertIn("diskd RPC: getVolumeCounts answered", [result.message for result in run.results])

    def test_doctor_says_when_mast_is_not_a_plist_it_can_read(self) -> None:
        prefix = "Attempt to read Apple ACPd's MaSt to get disk information failed: "
        cases = {
            # Each of these once read as a device without disks.
            "bare property name": ("MaSt=\n", "acp printed no MaSt plist"),
            "not a plist": ("MaSt=not a plist\n", "acp printed no MaSt plist"),
            "a dictionary": ("MaSt=" + plistlib.dumps({"MaSt": []}).decode(), "MaSt is not an array of disks"),
            "partitions not an array": ("MaSt=" + plistlib.dumps([{"deviceName": "wd0", "partitions": "dk2"}]).decode(),
                                        "MaSt lists wd0's partitions as something else"),
            "a plist cut off": ("MaSt=<?xml version=\"1.0\"?><plist><array><dict>", None),
        }
        for case, (output, reason) in cases.items():
            with self.subTest(case):
                self.give_mast(output)
                debug_fields: dict[str, object] = {}
                rows = self.disk_rows(self.run_healthy_doctor(debug_fields=debug_fields))

                self.assertEqual([row.details["code"] for row in rows], [DOCTOR_CODE_DISK_INFO_UNAVAILABLE])
                self.assertTrue(rows[0].message.startswith(prefix))
                error = debug_fields["disk_health"]["error"]
                self.assertEqual(rows[0].message, prefix + error)
                self.assertEqual(debug_fields["disk_health"]["disks"], [])
                if reason is not None:
                    self.assertEqual(error, reason)

    def test_disks_are_listed_ahead_of_checks_that_stop_doctor(self) -> None:
        # Apple's diskd reports the disks whether or not the runtime is installed.
        self.give_mast(test_storage_runtime.netbsd6_internal_and_usb_xml())
        not_installed = self.run_healthy_doctor(deployed_config_present=False)
        missing = next(problem for problem in DISK_PAYLOAD_PROBLEMS if problem.token == "payload=missing")
        payload_missing, _ = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(12.0, "manager started 12s ago", payload_problems=(missing,)), (),
        )
        not_mounting, _ = self.run_doctor_with_disk_errors(
            ManagerStartupAgeProbeResult(14400.0, "manager started 14400s ago", unmounted_since=1260), (),
        )
        for name, run in (("not installed", not_installed), ("payload missing", payload_missing),
                          ("disk not mounting", not_mounting)):
            with self.subTest(name):
                self.assertTrue(run.fatal)
                rows = self.disk_rows(run)
                self.assertEqual(len(rows), 2)
                first_fail = next(i for i, result in enumerate(run.results) if result.status == "FAIL")
                self.assertLess(run.results.index(rows[-1]), first_fail)

    def test_doctor_reads_no_mast_when_acpd_is_gone_or_ssh_is_skipped(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.QUIT_LISTING):
            acpd_gone = self.run_doctor_with_process_snapshot(f"{self.HEALTHY_SMBD_ROW}\n{self.QUIT_LISTING}")
        self.assertTrue(acpd_gone.fatal)
        self._mast_read.assert_not_called()

        self.run_doctor_with_mocks(skip_ssh=True, skip_bonjour=True, skip_smb=True)
        self._mast_read.assert_not_called()

    def test_doctor_does_not_ask_an_acpd_that_quit_while_samba_serves(self) -> None:
        # Doctor goes on past it, but acp would only wait out its timeout.
        serving = f"  107 1 15 S 0 select 0:00.32 sshd\n{self.HEALTHY_SMBD_ROW}\ntcapsule-ps: end"
        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=serving):
            run = self.run_doctor_with_process_snapshot(serving)

        self.assertEqual([result.details["acpd_problem"] for result in self.apple_acpd_failures(run)], ["quit"])
        self._mast_read.assert_not_called()
        self.assertEqual(
            [(row.details["code"], row.message) for row in self.disk_rows(run)],
            [(DOCTOR_CODE_DISK_INFO_UNAVAILABLE,
              "Attempt to read Apple ACPd's MaSt to get disk information failed: Apple ACPd is not answering")],
        )
        self.assertEqual(run.debug_fields["disk_health"], {"error": "Apple ACPd is not answering", "disks": []})

    def test_disk_health_skips_only_an_acpd_that_cannot_answer_acp(self) -> None:
        # Each problem as _doctor_check_apple_acpd records it, ahead of this step.
        self.give_mast(test_storage_runtime.netbsd6_internal_and_usb_xml())
        target = SimpleNamespace(connection=SshConnection("root@10.0.0.2", "pw", ""))
        remote = SimpleNamespace(remote_checks_enabled=True)
        for problem, gone, asked in (("quit", True, False), ("restart_never_happened", True, False),
                                     ("syut_silent", False, False), ("stuck_child", False, True),
                                     ("network_silent", False, True)):
            with self.subTest(problem):
                self._mast_read.reset_mock()
                sink = DoctorSink(on_result=None, debug_fields={})
                sink.add(CheckResult("FAIL", "Apple ACPd is broken", {
                    "code": APPLE_ACPD_BROKEN_CODE, "acpd_problem": problem, "acpd_gone": gone,
                }))

                _doctor_check_disk_health(target, remote, sink)

                self.assertEqual(self._mast_read.called, asked)
                codes = [result.details["code"] for result in sink.results[1:]]
                self.assertEqual(codes, [DOCTOR_CODE_DISK_INFO] * 2 if asked else [DOCTOR_CODE_DISK_INFO_UNAVAILABLE])
        # Another check's FAIL with the same details is not ACPd's.
        sink = DoctorSink(on_result=None, debug_fields={})
        sink.add(CheckResult("FAIL", "other", {"code": "other", "acpd_problem": "syut_silent", "acpd_gone": True}))
        self._mast_read.reset_mock()
        _doctor_check_disk_health(target, remote, sink)
        self._mast_read.assert_called_once()

    def test_disk_health_reads_nothing_without_remote_checks(self) -> None:
        sink = DoctorSink(on_result=None, debug_fields={})
        _doctor_check_disk_health(SimpleNamespace(connection=None), SimpleNamespace(remote_checks_enabled=False), sink)
        self._mast_read.assert_not_called()
        self.assertEqual((sink.results, sink.debug_fields), ([], {}))

    def test_disk_health_keeps_no_telemetry_without_debug_fields(self) -> None:
        self.give_mast(test_storage_runtime.netbsd6_internal_and_usb_xml())
        sink = DoctorSink(on_result=None, debug_fields=None)
        _doctor_check_disk_health(SimpleNamespace(connection=None), SimpleNamespace(remote_checks_enabled=True), sink)
        self.assertEqual(len(sink.results), 2)

if __name__ == "__main__":
    unittest.main()


class DiscoveryLogMergeTests(unittest.TestCase):
    # Discovery logs to RAM while smbd is not ready and to the payload after,
    # so the latest registrant plan can be in either file.
    RAM = "remote_diskless_discovery_log_tail"
    PAYLOAD = "remote_discovery_log_tail"
    INCOMPLETE = "registrant: plan incomplete mode=bridge reason=mode"
    VALIDATED = "registrant: plan validated mode=bridge desired=1 [if=9 _smb._tcp]"

    def _plan_line(self, fields: dict[str, str]) -> str:
        from timecapsulesmb.services.doctor import build_mdns_boot_context
        return next(line for line in build_mdns_boot_context(fields) if "registrant" in line)

    def test_newest_plan_wins_when_it_is_in_the_payload_log(self) -> None:
        line = self._plan_line({
            self.RAM: f"2026-09-16 07:35:21 {self.INCOMPLETE}",
            self.PAYLOAD: f"2026-09-16 07:40:00 {self.VALIDATED}",
        })
        self.assertIn("validated", line)

    def test_newest_plan_wins_when_it_is_in_the_ram_log(self) -> None:
        line = self._plan_line({
            self.RAM: f"2026-09-16 07:45:00 {self.INCOMPLETE}",
            self.PAYLOAD: f"2026-09-16 07:40:00 {self.VALIDATED}",
        })
        self.assertIn("incomplete", line)

    def test_either_log_alone_is_summarized_and_no_logs_give_nothing(self) -> None:
        from timecapsulesmb.services.doctor import build_mdns_boot_context
        self.assertIn("validated", self._plan_line({self.RAM: f"2026-09-16 07:40:00 {self.VALIDATED}"}))
        self.assertIn("validated", self._plan_line({self.PAYLOAD: f"2026-09-16 07:40:00 {self.VALIDATED}"}))
        self.assertEqual(build_mdns_boot_context({}), [])
        self.assertEqual(build_mdns_boot_context({self.RAM: None, self.PAYLOAD: 7}), [])
