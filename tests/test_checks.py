from __future__ import annotations

import ipaddress
import sys
import socket
import struct
import tempfile
import unittest
from contextlib import ExitStack
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
    select_smb_instance,
)
from timecapsulesmb.checks.doctor import run_doctor_checks
from timecapsulesmb.checks.doctor_steps import check_xattr_tdb_persistence
from timecapsulesmb.checks.doctor_debug import _data_disk_unresponsive_result
from timecapsulesmb.checks.doctor_steps import (
    DOCTOR_CODE_DEVICE_STARTING_UP,
    DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK,
    DOCTOR_STARTUP_GRACE_SECONDS,
    STARTUP_GRACE_DETAIL_KEY,
    STARTUP_GRACE_MASK,
    _apply_startup_grace,
    _add_sshpass_result,
)
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.doctor_steps import BONJOUR_OFF_LINK_CODE
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import LocalInterfaceNetwork, check_smb_port, check_ssh_login
from timecapsulesmb.core.net import RouteSelection
from timecapsulesmb.checks.nbns import (
    NBNS_NEGATIVE_RESPONSE_CODE,
    NBNS_OFF_SUBNET_CODE,
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
    try_authenticated_smb_listing,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.device.compat import DeviceCompatibility, compatibility_from_probe_result
from timecapsulesmb.device.migration_jobs import MigrationActivity, MigrationProgress, RunningMigration
from timecapsulesmb.device.probe import (
    DeviceIpv4Entry,
    DeviceIpv4SubnetsProbeResult,
    DeviceNetworksProbeResult,
    UsbPrinterProbeResult,
    parse_ifconfig_ipv4_entries,
    parse_ifconfig_networks,
    probe_device_networks_conn,
    DeviceHostnameProbeResult,
    DeployedVersionProbeResult,
    FLASH_RUNTIME_CONFIG,
    ManagerStartupAgeProbeResult,
    ProbedDeviceState,
    ProbeResult,
    ProbeStepResult,
    ReadinessProbeResult,
    RUNTIME_RAM_ROOT,
    RUNTIME_SMB_CONF,
    RuntimeNamingIdentityProbeResult,
    SshAccessStatus,
)
from timecapsulesmb.device.storage import MAST_PROBE_COMMAND, MaStProbeDiagnostics, MaStVolume
from timecapsulesmb.discovery.models import _merge_snapshots
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    BonjourServiceInstance,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection, SshError


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
        command_exists=True,
        read_active_smb_conf: str | None = None,
        xattr_result=None,
        smbd_probe=None,
        mdns_probe=None,
        connection=None,
        precomputed_probe_state=None,
        skip_ssh: bool = False,
        skip_bonjour: bool = False,
        skip_smb: bool = False,
        startup_grace: bool = True,
        debug_fields=None,
        on_result=None,
        runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None,
        deployed_config_present: bool = True,
        migration_activity: MigrationActivity | Exception | None = None,
        deployed_version: DeployedVersionProbeResult | None = None,
        runtime_ram_root_present: bool = True,
        client_source: str | None = None,
        device_subnets_probe: DeviceIpv4SubnetsProbeResult = SAME_SUBNET_DEVICE_PROBE,
        local_networks=SAME_NETWORK_LOCAL_NETWORKS,
        extra_patches: dict[str, object] | None = None,
    ):
        resolved_values = values or self.valid_doctor_values()
        mocks = SimpleNamespace()
        with ExitStack() as stack:
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
            if run_ssh_side_effect is not None:
                mocks.run_ssh = stack.enter_context(mock.patch("timecapsulesmb.device.probe.run_ssh", side_effect=run_ssh_side_effect))
            else:
                mocks.run_ssh = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.device.probe.run_ssh",
                        return_value=mock.Mock(returncode=run_ssh_returncode, stdout=run_ssh_stdout),
                    )
                )
            if command_exists is not None:
                mocks.command_exists = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor_steps.command_exists", return_value=command_exists))
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
            for index, (target, replacement) in enumerate((extra_patches or {}).items()):
                setattr(mocks, f"extra_{index}", stack.enter_context(mock.patch(target, replacement, create=True)))

            results, fatal = run_doctor_checks(
                self.doctor_config(resolved_values, exists=exists),
                repo_root=REPO_ROOT,
                connection=connection,
                precomputed_probe_state=precomputed_probe_state,
                skip_ssh=skip_ssh,
                skip_bonjour=skip_bonjour,
                skip_smb=skip_smb,
                startup_grace=startup_grace,
                on_result=on_result,
                debug_fields=debug_fields,
            )

        return SimpleNamespace(results=results, fatal=fatal, mocks=mocks)

    def setUp(self) -> None:
        self._exit_stack = ExitStack()
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
                "dk2": "adVF=0x83,adVN=Data,adVU=12345678-1234-1234-1234-123456789012",
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
        file_ops_mock.assert_called_once_with("admin", "pw", "timecapsulesamba4.local", "Data", port=445, ip_address="10.0.0.2")

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
                properties={"sys": "waMA=80:EA:96:E6:58:68,adVF=0x1010", "dk2": "adVF=0x83,adVN=Data,adVU=12345678-1234-1234-1234-123456789012"}, fullname=f"{name}._adisk._tcp.local."),
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
                        conflicting.properties["dk2"] = "adVF=0x83,adVN=Wrong,adVU=another-volume"
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
                            adisk.properties["dk2"] = "adVF=0x83,adVN=Wrong,adVU=another-volume"
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
                "dk2": "adVF=0x83,adVN=Data,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
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

    def test_check_required_local_tools_marks_dns_sd_missing_as_fail(self) -> None:
        def fake_exists(name: str) -> bool:
            return name == "ssh"

        with mock.patch("timecapsulesmb.checks.local_tools.command_exists", side_effect=fake_exists):
            results = check_required_local_tools()
        self.assertEqual([r.status for r in results], ["FAIL", "PASS"])
        self.assertEqual(
            [r.message for r in results],
            ["missing local tool smbclient, please install smbclient on your computer", "found local tool ssh"],
        )

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

        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip=None, family=None, interfaces=None)
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

        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip="10.0.1.77", family=None, interfaces=None)
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

    def test_try_authenticated_smb_listing_handles_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbclient"], timeout=30),
            ):
                result = try_authenticated_smb_listing("admin", "pw", ["server.local"])
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out", result.message)

    def test_check_authenticated_smb_listing_handles_timeout(self) -> None:
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=subprocess.TimeoutExpired(cmd=["smbclient"], timeout=20),
            ):
                result = check_authenticated_smb_listing("admin", "pw", "home.local", expected_share_name="Data")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("timed out via home.local", result.message)

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

    def test_run_doctor_checks_fails_missing_sshpass_for_netbsd4(self) -> None:
        values = self.valid_doctor_values(TC_MDNS_DEVICE_MODEL="TimeCapsule6,113", TC_AIRPORT_SYAP="113")
        netbsd4_state = mock.Mock(
            probe_result=mock.Mock(ssh_authenticated=True, error=None),
            compatibility=DeviceCompatibility(
                os_name="NetBSD",
                os_release="4.0_STABLE",
                arch="evbarm",
                elf_endianness="little",
                payload_family="netbsd4le_samba4",
                device_generation="gen1-4",
                supported=True,
                reason_code="supported_netbsd4",
            ),
        )
        run = self.run_doctor_with_mocks(
            values,
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            command_exists=False,
            mdns_probe=mock.Mock(ready=True, detail="ok"),
            read_active_smb_conf="",
            xattr_result=mock.Mock(status="WARN", message="xattr skipped"),
            smb_port=mock.Mock(status="SKIP", message="port skipped"),
            precomputed_probe_state=netbsd4_state,
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any(result.status == "FAIL" and "missing local tool sshpass" in result.message for result in run.results))

    def test_run_doctor_checks_fails_missing_sshpass_for_password_netbsd6(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            command_exists=False,
            mdns_probe=mock.Mock(ready=True, detail="ok"),
            read_active_smb_conf="",
            xattr_result=mock.Mock(status="WARN", message="xattr skipped"),
            smb_port=mock.Mock(status="SKIP", message="port skipped"),
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertTrue(any(result.status == "FAIL" and "password-based SSH uploads require sshpass" in result.message for result in run.results))

    def test_run_doctor_checks_allows_missing_sshpass_for_key_authentication(self) -> None:
        results: list[CheckResult] = []
        with mock.patch("timecapsulesmb.checks.doctor_steps.command_exists", return_value=False):
            _add_sshpass_result(results.append, password_auth=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "INFO")
        self.assertIn("key-authenticated SSH uploads", results[0].message)

    def test_run_doctor_checks_passes_when_sshpass_installed(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            command_exists=True,
            mdns_probe=mock.Mock(ready=True, detail="ok"),
            read_active_smb_conf="",
            xattr_result=mock.Mock(status="WARN", message="xattr skipped"),
            smb_port=mock.Mock(status="SKIP", message="port skipped"),
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertTrue(any(result.status == "PASS" and result.message == "found local tool sshpass" for result in run.results))

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
            command_exists=True,
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

    STUCK_SMBD_ROW = " 3166   457   457 D      127 biowait  smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group"
    HEALTHY_SMBD_ROW = "  457   146   457 I       20 select   smbd     /mnt/Memory/samba4/sbin/smbd -F --no-process-group"

    def run_doctor_with_process_snapshot(
        self,
        snapshot: object,
        *,
        smb_listing: CheckResult | None = None,
        manager_started_seconds_ago: float = 400.0,
        ssh_login: object | None = None,
    ):
        debug_fields: dict[str, object] = {}
        snapshot_mock = mock.Mock(side_effect=snapshot) if isinstance(snapshot, Exception) else mock.Mock(return_value=snapshot)
        log_tail_mock = mock.Mock(return_value={})
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=ssh_login or mock.Mock(status="PASS", message="ssh ok"),
                smb_port=mock.Mock(status="PASS", message="445 ok"),
                smb_instance=[],
                smb_listing=smb_listing or self.smb_listing_result(),
                smb_file_ops=[],
                smbd_probe=mock.Mock(ready=True, detail="managed smbd is ready"),
                mdns_probe=mock.Mock(ready=True, detail="managed mDNS registrant active"),
                debug_fields=debug_fields,
                extra_patches={
                    "timecapsulesmb.checks.doctor_steps.read_process_snapshot_conn": snapshot_mock,
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
        stuck = [result for result in run.results if "blocked in the kernel" in result.message]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0].status, "FAIL")
        self.assertIn("smbd (pid 3166) waiting on biowait for 127+ s", stuck[0].message)
        self.assertEqual(
            stuck[0].details["stuck_processes"],
            [{"pid": 3166, "name": "smbd", "wchan": "biowait", "sleep_seconds": 127}],
        )
        skipped = [result for result in run.results if result.status == "SKIP" and "authenticated SMB" in result.message]
        self.assertEqual(len(skipped), 1)
        run.mocks.check_authenticated_smb_listing.assert_not_called()
        self.assertIn(self.STUCK_SMBD_ROW.strip(), run.debug_fields["remote_process_snapshot"])
        # The snapshot answered, so the data-disk logs are still worth reading.
        run.log_tails.assert_called_once()
        self.assertIsNone(run.log_tails.call_args.kwargs["skip_data_disk"])

    def test_run_doctor_checks_reports_stuck_non_smbd_process_but_still_checks_smb(self) -> None:
        stuck_mdns = "  359   119     2 D       90 tstile   mDNSResponder /sbin/mDNSResponder -d"
        run = self.run_doctor_with_process_snapshot(
            "\n".join([self.HEALTHY_SMBD_ROW, stuck_mdns]),
            smb_listing=self.connection_shaped_smb_failure(),
        )

        stuck = [result for result in run.results if "blocked in the kernel" in result.message]
        self.assertEqual(len(stuck), 1)
        self.assertIn("mDNSResponder (pid 359) waiting on tstile for 90 s", stuck[0].message)
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 3)
        self.assertEqual([call.args[0] for call in run.sleep.call_args_list], [10, 15])

    def test_run_doctor_checks_ignores_short_uninterruptible_sleeps_and_kernel_threads(self) -> None:
        snapshot = "\n".join(
            [
                self.HEALTHY_SMBD_ROW,
                " 3166   457   457 D       30 biowait  smbd     /mnt/Memory/samba4/sbin/smbd -F",
                "    0     0     0 DKl    127 uvm      system   [system]",
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
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 1)
        run.sleep.assert_not_called()
        self.assertEqual(
            run.log_tails.call_args.kwargs["skip_data_disk"],
            "the device's process list timed out",
        )
        self.assertIn("SshCommandTimeout", run.debug_fields["remote_process_snapshot_error"])
        self.assertNotIn("remote_process_snapshot", run.debug_fields)

    def test_run_doctor_checks_keeps_normal_flow_when_snapshot_fails_without_timeout(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            SshError("ssh command failed with rc=255"),
            smb_listing=self.connection_shaped_smb_failure(),
        )

        self.assertFalse(any(result.status == "WARN" and "processes" in result.message for result in run.results))
        self.assertEqual(run.mocks.check_authenticated_smb_listing.call_count, 3)
        self.assertIsNone(run.log_tails.call_args.kwargs["skip_data_disk"])
        self.assertIn("ssh command failed", run.debug_fields["remote_process_snapshot_error"])

    def test_run_doctor_checks_does_not_list_processes_when_ssh_login_fails(self) -> None:
        run = self.run_doctor_with_process_snapshot(
            self.STUCK_SMBD_ROW,
            ssh_login=mock.Mock(status="FAIL", message="ssh failed"),
        )

        run.snapshot.assert_not_called()
        self.assertFalse(any("blocked in the kernel" in result.message for result in run.results))

    def test_run_doctor_checks_startup_grace_does_not_mask_stuck_processes(self) -> None:
        run = self.run_doctor_with_process_snapshot(self.STUCK_SMBD_ROW, manager_started_seconds_ago=41.0)

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertTrue(any("blocked in the kernel" in result.message for result in failures))
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in failures))

    def test_apply_startup_grace_masks_failures_within_grace_window(self) -> None:
        results = [
            CheckResult("PASS", "ssh ok"),
            CheckResult("FAIL", "managed runtime smbd binary missing", {STARTUP_GRACE_DETAIL_KEY: STARTUP_GRACE_MASK}),
            CheckResult("WARN", "could not inspect active smb.conf"),
            CheckResult(
                "FAIL",
                "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener",
                {"domain": "Runtime", STARTUP_GRACE_DETAIL_KEY: STARTUP_GRACE_MASK},
            ),
        ]

        transformed, synthesized = _apply_startup_grace(results, 41.0)
        startup_fail = synthesized[0]

        self.assertEqual(len(synthesized), 1)
        self.assertEqual(
            [(result.status, result.message) for result in transformed],
            [
                ("PASS", "ssh ok"),
                ("INFO", "managed runtime smbd binary missing"),
                ("WARN", "could not inspect active smb.conf"),
                ("INFO", "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener"),
                ("FAIL", startup_fail.message),
            ],
        )
        self.assertIn("still starting up", startup_fail.message)
        self.assertIn("41s ago", startup_fail.message)
        self.assertEqual(startup_fail.details["code"], DOCTOR_CODE_DEVICE_STARTING_UP)
        self.assertEqual(startup_fail.details["domain"], "Runtime")
        self.assertEqual(startup_fail.details["manager_started_seconds_ago"], 41)
        self.assertEqual(startup_fail.details["startup_grace_seconds"], DOCTOR_STARTUP_GRACE_SECONDS)
        self.assertEqual(
            startup_fail.details["masked_failures"],
            ["managed runtime smbd binary missing", "smbd is missing an IPv4 or IPv6 wildcard TCP 445 listener"],
        )
        demoted = transformed[3]
        self.assertEqual(demoted.details["masked_by"], DOCTOR_CODE_DEVICE_STARTING_UP)
        self.assertEqual(demoted.details["domain"], "Runtime")

    def test_apply_startup_grace_leaves_passing_results_untouched_within_grace_window(self) -> None:
        results = [CheckResult("PASS", "ssh ok"), CheckResult("WARN", "minor")]

        transformed, synthesized = _apply_startup_grace(results, 41.0)

        self.assertEqual(synthesized, ())
        self.assertEqual(transformed, results)

    def test_apply_startup_grace_skips_when_manager_started_long_ago(self) -> None:
        results = [CheckResult("FAIL", "managed runtime smbd binary missing", {STARTUP_GRACE_DETAIL_KEY: STARTUP_GRACE_MASK})]

        transformed, synthesized = _apply_startup_grace(results, float(DOCTOR_STARTUP_GRACE_SECONDS))

        self.assertEqual(synthesized, ())
        self.assertEqual(transformed, results)

    def test_apply_startup_grace_skips_when_manager_age_unknown(self) -> None:
        results = [CheckResult("FAIL", "managed runtime smbd binary missing", {STARTUP_GRACE_DETAIL_KEY: STARTUP_GRACE_MASK})]

        transformed, synthesized = _apply_startup_grace(results, None)

        self.assertEqual(synthesized, ())
        self.assertEqual(transformed, results)

    def test_apply_startup_grace_keeps_persistent_failures_and_adds_recent_startup_note(self) -> None:
        results = [
            CheckResult("FAIL", "missing local tool sshpass; password-based SSH uploads require sshpass"),
            CheckResult(
                "FAIL",
                "Detected NetBSD 6.0 (earmv4) with big-endian binaries, "
                "which is not supported by the current Samba payload.",
            ),
            CheckResult(
                "FAIL",
                "active smb.conf xattr_tdb:file parent is missing",
                {"code": DOCTOR_CODE_PAYLOAD_MISSING_FROM_DISK},
            ),
        ]

        transformed, synthesized = _apply_startup_grace(results, 41.0)

        self.assertEqual(len(synthesized), 1)
        self.assertEqual(synthesized[0].status, "INFO")
        self.assertIn("device services started 41s ago", synthesized[0].message)
        self.assertEqual(transformed[:-1], results)
        self.assertEqual(transformed[-1], synthesized[0])

    def test_apply_startup_grace_preserves_unknown_failures_by_default(self) -> None:
        results = [CheckResult("FAIL", "new failure without startup policy")]

        transformed, synthesized = _apply_startup_grace(results, 41.0)

        self.assertEqual(len(synthesized), 1)
        self.assertEqual(transformed[0], results[0])
        self.assertEqual(transformed[1].status, "INFO")
        self.assertIn("some failures above may resolve", transformed[1].message)

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

    def test_doctor_masks_hostname_failures_while_the_device_is_starting_up(self) -> None:
        for probe, code in (
            (DeviceHostnameProbeResult("", (), manager_waiting=True), "hostname_waiting"),
            (DeviceHostnameProbeResult("capsule", ()), "hostname_unmapped"),
        ):
            with self.subTest(code=code):
                run = self.run_doctor_with_hostname(probe, started_seconds_ago=20.0)
                masked = next(result for result in run.results if result.details.get("code") == code)
                self.assertEqual(masked.status, "INFO")
                self.assertEqual(masked.details["masked_by"], DOCTOR_CODE_DEVICE_STARTING_UP)
                starting = [result for result in run.results if result.status == "FAIL"]
                self.assertEqual([result.details["code"] for result in starting], [DOCTOR_CODE_DEVICE_STARTING_UP])
                self.assertIn(masked.message, starting[0].details["masked_failures"])

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

    def test_run_doctor_checks_collapses_startup_failures_into_single_fail(self) -> None:
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
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        self.assertTrue(run.fatal)
        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].details["code"], DOCTOR_CODE_DEVICE_STARTING_UP)
        demoted = [result for result in run.results if result.details.get("masked_by") == DOCTOR_CODE_DEVICE_STARTING_UP]
        self.assertTrue(demoted)
        self.assertTrue(all(result.status == "INFO" for result in demoted))
        self.assertTrue(any("managed mDNS registrant is not active" in result.message for result in demoted))
        self.assertEqual(failures[0].details["masked_failures"], [result.message for result in demoted])
        self.assertTrue(debug_fields["startup_grace_applied"])
        self.assertEqual(debug_fields["manager_startup_age"], {"seconds_ago": 41.0, "detail": "manager started 41s ago"})
        # The synthesized failure is streamed so live consumers (CLI) see it last.
        self.assertEqual(streamed[-1].details.get("code"), DOCTOR_CODE_DEVICE_STARTING_UP)

    def test_run_doctor_checks_can_disable_startup_grace_transform(self) -> None:
        debug_fields: dict[str, object] = {}
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS registrant not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            startup_grace=False,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        self.assertTrue(run.fatal)
        self.assertTrue(
            any(
                result.status == "FAIL" and "managed mDNS registrant is not active" in result.message
                for result in run.results
            )
        )
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in run.results))
        self.assertNotIn("startup_grace_applied", debug_fields)

    def test_run_doctor_checks_keeps_smb_auth_failure_during_startup_grace(self) -> None:
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
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(41.0, "manager started 41s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertTrue(any("NT_STATUS_LOGON_FAILURE" in result.message for result in failures))
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in failures))
        self.assertEqual(
            [result for result in run.results if result.details.get("masked_by") == DOCTOR_CODE_DEVICE_STARTING_UP],
            [],
        )
        self.assertTrue(
            any(
                result.status == "INFO" and "device services started 41s ago" in result.message
                for result in run.results
            )
        )

    def test_run_doctor_checks_masks_connection_shaped_smb_failure_during_startup_grace(self) -> None:
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

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].details["code"], DOCTOR_CODE_DEVICE_STARTING_UP)
        demoted = [result for result in run.results if result.details.get("masked_by") == DOCTOR_CODE_DEVICE_STARTING_UP]
        self.assertEqual(len(demoted), 1)
        self.assertIn("NT_STATUS_IO_TIMEOUT", demoted[0].message)

    def test_run_doctor_checks_keeps_failures_when_manager_started_long_ago(self) -> None:
        debug_fields: dict[str, object] = {}
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
                "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                    return_value=ManagerStartupAgeProbeResult(400.0, "manager started 400s ago")
                ),
                "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
            },
        )

        self.assertTrue(run.fatal)
        self.assertTrue(
            any(
                result.status == "FAIL" and "managed mDNS registrant is not active" in result.message
                for result in run.results
            )
        )
        self.assertFalse(
            any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in run.results)
        )
        self.assertNotIn("startup_grace_applied", debug_fields)

    def test_run_doctor_checks_keeps_compatibility_failure_during_startup_grace(self) -> None:
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
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in failures))
        demoted = [result for result in run.results if result.details.get("masked_by") == DOCTOR_CODE_DEVICE_STARTING_UP]
        self.assertEqual(demoted, [])
        self.assertFalse(
            any("not supported by the current Samba payload" in result.message for result in demoted)
        )
        self.assertTrue(
            any(
                result.status == "INFO" and "device services started 41s ago" in result.message
                for result in run.results
            )
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
        self.assertFalse(
            any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in run.results)
        )
        self.assertNotIn("startup_grace_applied", debug_fields)

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
            },
        )

        mast_probe_mock.assert_called_once()
        self.assertEqual(debug_fields["mast_probe_volume_count"], 1)

    def test_run_doctor_checks_adds_mast_probe_for_bad_network_name_file_ops(self) -> None:
        debug_fields: dict[str, object] = {}
        mast_probe_mock = mock.Mock(return_value=self.mast_probe_diagnostics())

        self.run_doctor_with_mocks(
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
    ):
        # This computer defaults to 10.0.0.50, on the harness device's 10.0.0.0/24.
        with mock.patch("timecapsulesmb.checks.doctor_steps.time.sleep") as sleep_mock:
            run = self.run_doctor_with_mocks(
                ssh_login=mock.Mock(status="PASS", message="ssh ok"),
                smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                xattr_result=CheckResult("PASS", "xattr ok"),
                read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
                skip_bonjour=True,
                skip_smb=True,
                client_source=client_source,
                debug_fields=debug_fields,
                extra_patches={
                    "timecapsulesmb.checks.doctor_steps.probe_device_networks_conn": device_networks_probe(
                        device_probe_mock or mock.Mock(return_value=SAME_SUBNET_DEVICE_PROBE)
                    ),
                    "timecapsulesmb.checks.doctor_steps.probe_managed_mdns_conn": mdns_mock,
                    "timecapsulesmb.checks.doctor_steps.check_nbns_name_resolution": nbns_mock,
                    "timecapsulesmb.checks.doctor_steps.probe_manager_startup_age_conn": mock.Mock(
                        return_value=ManagerStartupAgeProbeResult(startup_age, f"manager started {int(startup_age)}s ago")
                    ),
                    "timecapsulesmb.checks.doctor_debug.read_runtime_log_tails_conn": mock.Mock(return_value={}),
                    "timecapsulesmb.checks.doctor_debug.read_runtime_ram_diagnostics_conn": mock.Mock(return_value="ram ok"),
                    "timecapsulesmb.checks.doctor_debug.read_remote_service_socket_diagnostics_conn": (
                        socket_debug_mock or mock.Mock(return_value="")
                    ),
                },
            )
        return run, [call.args[0] for call in sleep_mock.call_args_list]

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
        # and the device; waiting cannot fix it and startup grace must not
        # hide it, even right after a boot.
        ready = self._native_nbns_probe("pass", "Apple wcifsnd is ready on UDP 137 and 138")
        nbns_mock = mock.Mock(return_value=self._nbns_query_timeout())

        run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=ready), nbns_mock, startup_age=41.0)

        self.assertTrue(run.fatal)
        self.assertEqual(sleeps, [])
        nbns_mock.assert_called_once()
        query = next(result for result in run.results if "NBNS query" in result.message)
        self.assertEqual(query.status, "FAIL")
        self.assertNotIn("startup_grace", query.details)
        self.assertFalse(any(result.details.get("code") == DOCTOR_CODE_DEVICE_STARTING_UP for result in run.results))

    def test_run_doctor_checks_retries_query_while_native_nbns_is_not_ready(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        resolved = CheckResult("PASS", "NBNS query for 'TimeCapsule' resolved to 10.0.0.2")
        nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout(), resolved])

        run, sleeps = self._run_doctor_nbns(mock.Mock(return_value=not_ready), nbns_mock)

        self.assertEqual(sleeps, [10, 15, 10])
        self.assertEqual(nbns_mock.call_count, 2)
        self.assertIn(resolved, run.results)

    def test_run_doctor_checks_startup_grace_collapses_unregistered_nbns_failures(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")
        for startup_age, collapsed in ((41.0, True), (3600.0, False)):
            with self.subTest(startup_age=startup_age):
                nbns_mock = mock.Mock(side_effect=[self._nbns_query_timeout()] * 3)

                run, _ = self._run_doctor_nbns(mock.Mock(return_value=not_ready), nbns_mock, startup_age=startup_age)

                self.assertTrue(run.fatal)
                self.assertEqual(nbns_mock.call_count, 3)
                failures = [result for result in run.results if result.status == "FAIL"]
                if collapsed:
                    self.assertEqual([result.details.get("code") for result in failures], [DOCTOR_CODE_DEVICE_STARTING_UP])
                    self.assertIn("NBNS query for 'TimeCapsule' timed out against 10.0.0.2:137",
                                  failures[0].details["masked_failures"])
                    self.assertIn("discovery native NBNS is not ready", failures[0].details["masked_failures"])
                else:
                    self.assertEqual(
                        {result.message for result in failures},
                        {"discovery native NBNS is not ready", "NBNS query for 'TimeCapsule' timed out against 10.0.0.2:137"},
                    )

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
        device_probe.assert_not_called()
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
        device_probe.assert_not_called()
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
                device_probe.assert_not_called()
                self.assertNotIn("nbns_subnet", debug_fields)
                self.assertEqual(socket_debug.called, sockets_read)

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

    def test_run_doctor_checks_keeps_off_subnet_skip_out_of_startup_grace(self) -> None:
        not_ready = self._native_nbns_probe("fail", "discovery native NBNS is not ready")

        run, _ = self._run_doctor_nbns(
            mock.Mock(return_value=not_ready),
            mock.Mock(side_effect=[self._nbns_query_timeout()] * 3),
            startup_age=41.0,
            client_source="192.168.24.102",
            device_probe_mock=mock.Mock(return_value=self.OFF_SUBNET_DEVICE_PROBE),
        )

        failures = [result for result in run.results if result.status == "FAIL"]
        self.assertEqual([result.details.get("code") for result in failures], [DOCTOR_CODE_DEVICE_STARTING_UP])
        self.assertIn("discovery native NBNS is not ready", failures[0].details["masked_failures"])
        self.assertFalse(any("NBNS query" in masked for masked in failures[0].details["masked_failures"]))
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
        ):
            result = check_ssh_login(connection)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(
            result.message,
            "Connecting to the device failed, SSH error: bind [127.0.0.1]:108: Permission denied",
        )

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
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=smb_results,
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            skip_bonjour=True,
        )
        self.assertFalse(run.fatal)
        self.assertEqual([result.message for result in run.results[-10:]], [result.message for result in smb_results])

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

        with mock.patch("timecapsulesmb.checks.doctor_steps.check_required_local_tools", return_value=[]):
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
                    "dk2": "adVF=0x83,adVN=AirPort Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
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
                    "dk2": "adVF=0x83,adVN=AirPort Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
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
                    "dk2": "adVF=0x83,adVN=Backup,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
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
                    "dk2": "adVF=0x83,adVN=Home  Disk,adVU=117b94b1-3cf3-5600-b192-cc0dd671b852",
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
        self.assertIn("Bonjour IPv4: no _afpovertcp._tcp advertised for 'Home'", messages)
        self.assertIn("Bonjour IPv4: no duplicate SMB/ADisk registrations for device home.local", messages)
        self.assertIn("Bonjour IPv4: _device-info._tcp model is Apple's: TimeCapsule6,116", messages)
        self.assertFalse(any(result.status == "FAIL" and "Apple" in result.message for result in run.results), messages)

    def test_run_doctor_checks_fails_on_uninvited_afp_duplicate_device_services_and_foreign_model(self) -> None:
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local."),
            BonjourServiceInstance("_smb._tcp.local.", "Home (2)", "Home (2)._smb._tcp.local."),
            BonjourServiceInstance("_adisk._tcp.local.", "Home (2)", "Home (2)._adisk._tcp.local."),
            BonjourServiceInstance("_afpovertcp._tcp.local.", "Home", "Home._afpovertcp._tcp.local."),
            BonjourServiceInstance("_device-info._tcp.local.", "Home", "Home._device-info._tcp.local."),
        ]
        records = self._apple_responder_records(model="Macmini9,1")
        records += [replace(record, name="Home (2)") for record in records[:2]]
        run = self._apple_responder_doctor_run(instances, records)
        self.assertTrue(run.fatal)
        failures = [result.message for result in run.results if result.status == "FAIL"]
        self.assertTrue(any("_afpovertcp._tcp is advertised for 'Home' although Advertise AFP over Bonjour is off" in m and "macOS 26.x/27" in m for m in failures), failures)
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

    def test_run_doctor_checks_accepts_afp_when_advertising_is_enabled(self) -> None:
        instances = [
            BonjourServiceInstance("_smb._tcp.local.", "Home", "Home._smb._tcp.local."),
            BonjourServiceInstance("_afpovertcp._tcp.local.", "Home", "Home._afpovertcp._tcp.local."),
        ]
        run = self._apple_responder_doctor_run(instances, self._apple_responder_records(model=None), advertise_afp=True)
        messages = [result.message for result in run.results]
        self.assertIn("Bonjour IPv4: _afpovertcp._tcp advertised for 'Home' as configured", messages)
        self.assertFalse(any("_device-info._tcp model" in m for m in messages))   # nothing resolved: no verdict
        self.assertFalse(any(result.status == "FAIL" and "_afpovertcp" in result.message for result in run.results))

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
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[mock.Mock(status="PASS", message="file ops ok")],
            run_ssh_side_effect=self.run_ssh_with_active_smb_conf(),
            runtime_naming_identity=self.runtime_identity_from_values(values),
        )
        self.assertFalse(run.fatal)
        run.mocks.check_authenticated_smb_listing.assert_called_once_with(
            "admin",
            "pw",
            [SmbClientTarget("timecapsulesamba4.local", "10.0.0.2")],
            port=445,
        )
        run.mocks.check_authenticated_smb_file_ops_detailed.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
            port=445,
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
            "timecapsulesamba4.local",
            "Nicholas McBride's Time Ca",
            port=445,
        )
        self.assertIn(
            "authenticated SMB listing includes active share \"Nicholas McBride's Time Ca\"",
            [result.message for result in run.results if result.status == "PASS"],
        )

    def test_run_doctor_checks_fails_when_active_share_missing_from_smb_listing(self) -> None:
        listing_mock = mock.Mock(return_value=self.smb_listing_result(disk_shares=["Public"]))
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])

        run = self.run_doctor_with_mocks(
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

    def test_check_authenticated_smb_listing_requires_expected_share(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Public\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "server.local",
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "FAIL")
        self.assertIn("did not include expected share", result.message)

    def test_check_authenticated_smb_listing_passes_when_expected_share_present(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Data\nPublic\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", return_value=proc):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "server.local",
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("listing works", result.message)
        self.assertEqual(result.details["server"], "server.local")
        self.assertEqual(result.details["disk_shares"], ["Data", "Public"])

    def test_parse_smbclient_disk_shares_uses_machine_listing_types(self) -> None:
        output = "\n".join([
            "Disk|Data|Main storage",
            "IPC|IPC$|IPC Service",
            "Printer|lp|Printer",
            "Disk|Archive Data|",
            "Disk|Data|Duplicate",
        ])

        self.assertEqual(parse_smbclient_disk_shares(output), ["Data", "Archive Data"])

    def test_try_authenticated_smb_listing_falls_back_to_second_server_when_first_times_out(self) -> None:
        proc = subprocess.CompletedProcess(["smbclient"], 0, "Data\nPublic\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[
                    subprocess.TimeoutExpired(cmd=["smbclient"], timeout=30),
                    proc,
                ],
            ):
                result = try_authenticated_smb_listing(
                    "admin",
                    "pw",
                    ["home.local", "10.0.1.1"],
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("admin@10.0.1.1", result.message)
        self.assertEqual(result.details["server"], "10.0.1.1")

    def test_try_authenticated_smb_listing_continues_when_share_missing_on_first_server(self) -> None:
        missing_proc = subprocess.CompletedProcess(["smbclient"], 0, "Public\n", "")
        good_proc = subprocess.CompletedProcess(["smbclient"], 0, "Data\nPublic\n", "")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[missing_proc, good_proc],
            ):
                result = try_authenticated_smb_listing(
                    "admin",
                    "pw",
                    ["home.local", "10.0.1.1"],
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "PASS")
        self.assertIn("admin@10.0.1.1", result.message)
        self.assertEqual(result.details["server"], "10.0.1.1")

    def test_try_authenticated_smb_listing_records_attempt_debug_details(self) -> None:
        failed_proc = subprocess.CompletedProcess(["smbclient"], 1, "", "NT_STATUS_IO_TIMEOUT\n")
        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch(
                "timecapsulesmb.checks.smb.run_local_capture",
                side_effect=[
                    subprocess.TimeoutExpired(cmd=["smbclient"], timeout=30),
                    failed_proc,
                ],
            ):
                result = try_authenticated_smb_listing(
                    "admin",
                    "secret-password",
                    ["home.local", "10.0.1.1"],
                    expected_share_name="Data",
                )

        self.assertEqual(result.status, "FAIL")
        attempts = result.details["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["server"], "home.local")
        self.assertEqual(attempts[0]["outcome"], "timeout")
        self.assertEqual(attempts[0]["timeout_sec"], 30)
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

    def test_run_doctor_checks_does_not_retry_smb_listing_missing_expected_share(self) -> None:
        failure = CheckResult(
            "FAIL",
            "listing failed",
            {"attempts": [{"server": "home.local", "outcome": "missing_expected_share", "expected_share": "Data"}]},
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

    def test_run_doctor_checks_retries_host_unreachable_smbclient_through_ssh_tunnel(self) -> None:
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
        listing_mock = mock.Mock(side_effect=[direct_failure, self.smb_listing_result("127.0.0.1")])
        file_ops_mock = mock.Mock(return_value=[mock.Mock(status="PASS", message="file ops ok")])
        tunnel_mock = mock.MagicMock()
        tunnel_mock.return_value.__enter__.return_value = None
        tunnel_mock.return_value.__exit__.return_value = None

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.find_free_local_port": mock.Mock(return_value=2445),
                "timecapsulesmb.checks.doctor_steps.ssh_local_forward": tunnel_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": file_ops_mock,
            },
        )

        self.assertFalse(run.fatal)
        self.assertFalse(any(result.status == "FAIL" for result in run.results))
        self.assertTrue(any(result.status == "WARN" and "retrying through SSH tunnel" in result.message for result in run.results))
        tunnel_mock.assert_called_once_with(mock.ANY, local_port=2445, remote_host="10.0.0.2", remote_port=445)
        self.assertEqual(listing_mock.call_count, 2)
        self.assertEqual(listing_mock.call_args_list[1].args[2], "127.0.0.1")
        self.assertEqual(listing_mock.call_args_list[1].kwargs["port"], 2445)
        file_ops_mock.assert_called_once_with("admin", "pw", "127.0.0.1", "Data", port=2445)
        self.assertEqual(debug_fields["authenticated_smb_listing_attempts"], direct_attempts)
        self.assertEqual(debug_fields["authenticated_smb_tunnel_listing_servers"], ["127.0.0.1"])

    def test_run_doctor_checks_keeps_host_unreachable_smbclient_fatal_when_tunnel_fails(self) -> None:
        direct_failure = CheckResult(
            "FAIL",
            "authenticated SMB listing failed after 1 attempt(s): attempt 1 timecapsulesamba4.local via 10.0.0.2: NT_STATUS_HOST_UNREACHABLE",
            {
                "attempts": [
                    {
                        "server": "timecapsulesamba4.local",
                        "ip_address": "10.0.0.2",
                        "outcome": "error",
                        "failure": "NT_STATUS_HOST_UNREACHABLE",
                    }
                ]
            },
        )
        tunnel_failure = CheckResult("FAIL", "authenticated SMB listing failed through tunnel", {"attempts": []})
        listing_mock = mock.Mock(side_effect=[direct_failure, tunnel_failure])
        tunnel_mock = mock.MagicMock()
        tunnel_mock.return_value.__enter__.return_value = None
        tunnel_mock.return_value.__exit__.return_value = None

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            extra_patches={
                "timecapsulesmb.checks.doctor_steps.find_free_local_port": mock.Mock(return_value=2446),
                "timecapsulesmb.checks.doctor_steps.ssh_local_forward": tunnel_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_listing": listing_mock,
                "timecapsulesmb.checks.doctor_steps.check_authenticated_smb_file_ops_detailed": mock.Mock(return_value=[]),
            },
        )

        self.assertTrue(run.fatal)
        messages = [result.message for result in run.results]
        self.assertTrue(any("retrying through SSH tunnel" in message for message in messages))
        self.assertIn("authenticated SMB listing failed through tunnel", messages)
        self.assertIn(direct_failure.message, messages)

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
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing("admin", "pw", "server.local", expected_share_name="Data")
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
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    "127.0.0.1",
                    expected_share_name="Data",
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
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing(
                    "admin",
                    "pw",
                    SmbClientTarget("server.local", "192.168.1.217"),
                    expected_share_name="Data",
                )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.details["server"], "server.local")
        self.assertEqual(result.details["ip_address"], "192.168.1.217")
        self.assertEqual(
            captured_args,
            ["smbclient", "-s", "/dev/null", "-g", "-I", "192.168.1.217", "-L", "//server.local", "-U", "admin%pw"],
        )

    def test_try_authenticated_smb_listing_forwards_custom_port(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=30, **kwargs):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = try_authenticated_smb_listing("admin", "pw", ["127.0.0.1"], port=2445)
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
            startup_grace=False,
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
            startup_grace=False,
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
            startup_grace=False,
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
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
            port=445,
            ip_address="10.0.0.2",
        )

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
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
            port=445,
            ip_address="10.0.0.2",
        )

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
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
            port=445,
            ip_address="fd00::2",
        )

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
