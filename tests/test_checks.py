from __future__ import annotations

import sys
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import subprocess

from timecapsulesmb.checks.bonjour import (
    build_bonjour_expected_identity,
    check_bonjour_host_ip,
    check_bonjour_host_link_local_ips,
    check_smb_instance,
    check_smb_service_target,
    discover_smb_services_detailed,
    resolve_smb_instance,
    resolve_smb_service_target,
    select_resolved_smb_record,
    select_smb_instance,
)
from timecapsulesmb.checks.doctor import (
    DOCTOR_CHECKS,
    DoctorCheck,
    DoctorRunContext,
    _run_doctor_registry,
    check_xattr_tdb_persistence,
    run_doctor_checks,
)
from timecapsulesmb.checks.local_tools import check_required_local_tools
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import check_smb_port, check_ssh_login, ssh_opts_use_proxy
from timecapsulesmb.checks.nbns import build_nbns_query, check_nbns_name_resolution, extract_nbns_response_ip
from timecapsulesmb.checks.smb import (
    check_authenticated_smb_file_ops_detailed,
    check_authenticated_smb_listing,
    try_authenticated_smb_listing,
)
from timecapsulesmb.checks.smb_targets import doctor_smb_servers
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import RemoteInterfaceProbeResult, RuntimeNamingIdentityProbeResult
from timecapsulesmb.discovery.bonjour import (
    BonjourDiscoveryDiagnostics,
    BonjourDiscoverySnapshot,
    BonjourResolvedService,
    BonjourServiceInstance,
)
from timecapsulesmb.transport.ssh import SshConnection, SshError


DEFAULT_SMB_PORT_CHECK = object()
REAL_SMB_PORT_CHECK = object()


class CheckTests(unittest.TestCase):
    def smb_listing_result(self, server: str = "timecapsulesamba4.local") -> CheckResult:
        return CheckResult("PASS", "listing ok", {"server": server})

    def doctor_config(self, values: dict[str, str], *, exists: bool = True) -> AppConfig:
        return AppConfig.from_values(
            values,
            path=REPO_ROOT / ".env",
            exists=exists,
            file_values=values if exists else {},
        )

    def doctor_context(self) -> DoctorRunContext:
        return DoctorRunContext(
            config=self.doctor_config({}),
            repo_root=REPO_ROOT,
            connection=None,
            precomputed_interface_probe=None,
            precomputed_probe_state=None,
            skip_ssh=False,
            skip_bonjour=False,
            skip_smb=False,
            on_result=None,
            debug_fields=None,
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
        command_exists=None,
        read_active_smb_conf: str | None = None,
        runtime_share_names: list[str] | None = None,
        xattr_result=None,
        smbd_probe=None,
        mdns_probe=None,
        remote_interface_probe=None,
        connection=None,
        precomputed_interface_probe=None,
        precomputed_probe_state=None,
        skip_ssh: bool = False,
        skip_bonjour: bool = False,
        skip_smb: bool = False,
        debug_fields=None,
        on_result=None,
        runtime_naming_identity: RuntimeNamingIdentityProbeResult | None = None,
        extra_patches: dict[str, object] | None = None,
    ):
        resolved_values = values or self.valid_doctor_values()
        mocks = SimpleNamespace()
        with ExitStack() as stack:
            mocks.check_required_local_tools = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[] if local_tools is None else local_tools)
            )
            mocks.check_required_artifacts = stack.enter_context(
                mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[] if artifacts is None else artifacts)
            )
            if ssh_login is not None:
                mocks.check_ssh_login = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=ssh_login))
            if smb_port is DEFAULT_SMB_PORT_CHECK:
                mocks.check_smb_port = stack.enter_context(
                    mock.patch(
                        "timecapsulesmb.checks.doctor.check_smb_port",
                        return_value=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
                    )
                )
            elif smb_port is not REAL_SMB_PORT_CHECK:
                mocks.check_smb_port = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=smb_port))
            if smb_instance is not None:
                mocks.check_smb_instance = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=smb_instance))
            if smb_listing is not None:
                mocks.check_authenticated_smb_listing = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=smb_listing)
                )
            if smb_file_ops is not None:
                mocks.check_authenticated_smb_file_ops_detailed = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=smb_file_ops)
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
                mocks.command_exists = stack.enter_context(mock.patch("timecapsulesmb.checks.doctor.command_exists", return_value=command_exists))
            if read_active_smb_conf is not None:
                mocks.read_active_smb_conf_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.read_active_smb_conf_conn", return_value=read_active_smb_conf)
                )
            mocks.read_runtime_share_names_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor.read_runtime_share_names_conn",
                    return_value=["Data"] if runtime_share_names is None else runtime_share_names,
                )
            )
            if xattr_result is not None:
                mocks.check_xattr_tdb_persistence = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.check_xattr_tdb_persistence", return_value=xattr_result)
                )
            if smbd_probe is not None:
                mocks.probe_managed_smbd_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.probe_managed_smbd_conn", return_value=smbd_probe)
                )
            if mdns_probe is not None:
                mocks.probe_managed_mdns_takeover_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mdns_probe)
                )
            if remote_interface_probe is not None:
                mocks.probe_remote_interface_conn = stack.enter_context(
                    mock.patch("timecapsulesmb.checks.doctor.probe_remote_interface_conn", return_value=remote_interface_probe)
                )
            mocks.probe_remote_runtime_naming_identity_conn = stack.enter_context(
                mock.patch(
                    "timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn",
                    return_value=runtime_naming_identity or self.runtime_identity_from_values(resolved_values),
                )
            )
            for index, (target, replacement) in enumerate((extra_patches or {}).items()):
                setattr(mocks, f"extra_{index}", stack.enter_context(mock.patch(target, replacement)))

            results, fatal = run_doctor_checks(
                self.doctor_config(resolved_values, exists=exists),
                repo_root=REPO_ROOT,
                connection=connection,
                precomputed_interface_probe=precomputed_interface_probe,
                precomputed_probe_state=precomputed_probe_state,
                skip_ssh=skip_ssh,
                skip_bonjour=skip_bonjour,
                skip_smb=skip_smb,
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
        default_bonjour_record = BonjourResolvedService(
            name="Time Capsule Samba 4",
            hostname="timecapsulesamba4.local",
            service_type="_smb._tcp.local.",
            port=445,
            ipv4=["10.0.0.2"],
        )
        default_bonjour_snapshot = BonjourDiscoverySnapshot(
            instances=[default_bonjour_instance],
            resolved=[default_bonjour_record],
        )
        default_bonjour_diagnostics = BonjourDiscoveryDiagnostics(
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
            instances=[default_bonjour_instance],
            resolved=[default_bonjour_record],
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                return_value=(default_bonjour_snapshot, None, default_bonjour_diagnostics),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.resolve_smb_instance",
                return_value=(default_bonjour_record, None),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.probe_remote_interface_conn",
                return_value=RemoteInterfaceProbeResult(iface="bridge0", exists=True, detail="interface bridge0 exists"),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.probe_connection_state",
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
                "timecapsulesmb.checks.doctor.probe_managed_smbd_conn",
                return_value=mock.Mock(
                    ready=True,
                    detail="managed smbd ready",
                    lines=(
                        "PASS:managed runtime smb.conf present",
                        "PASS:managed smbd parent process is running",
                        "PASS:smbd bound to IPv4 TCP 445",
                    ),
                ),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn",
                return_value=mock.Mock(ready=True, detail="managed mDNS takeover active"),
            )
        )
        self._exit_stack.enter_context(
            mock.patch(
                "timecapsulesmb.checks.doctor.check_bonjour_host_ip",
                return_value=mock.Mock(
                    status="PASS",
                    message="resolved Bonjour host timecapsulesamba4.local to 10.0.0.2 from service record",
                ),
            )
        )
        self._exit_stack.enter_context(
            mock.patch("timecapsulesmb.checks.doctor.read_runtime_share_names_conn", return_value=["Data"])
        )

    def tearDown(self) -> None:
        self._exit_stack.close()

    def test_doctor_registry_declares_satisfied_dependencies(self) -> None:
        expected_ids = [
            "config_validation",
            "connection_context",
            "ssh_login",
            "runtime_naming_identity",
            "device_compatibility",
            "managed_smbd",
            "managed_mdns",
            "active_smb_conf",
            "direct_smb_port",
            "bonjour",
            "bonjour_debug_fields",
            "bonjour_naming_info",
            "active_smb_conf_info",
            "nbns",
            "authenticated_smb",
            "fatal_runtime_log_tails",
        ]
        self.assertEqual([check.id for check in DOCTOR_CHECKS], expected_ids)

        provided = {"config", "repo_root"}
        for check in DOCTOR_CHECKS:
            missing = [dependency for dependency in check.requires if dependency not in provided]
            self.assertEqual(missing, [], f"{check.id} has unsatisfied dependencies")
            provided.update(check.provides)

    def test_doctor_registry_runner_rejects_missing_dependency(self) -> None:
        check = DoctorCheck(
            id="bad_check",
            requires=("missing_dependency",),
            provides=(),
            run=lambda _context: None,
        )

        with self.assertRaisesRegex(AssertionError, "bad_check.*missing_dependency"):
            _run_doctor_registry(self.doctor_context(), (check,))

    def test_doctor_registry_runner_stops_when_context_requests_stop(self) -> None:
        calls: list[str] = []

        def stop_check(context: DoctorRunContext) -> None:
            calls.append("stop")
            context.add_result(CheckResult("FAIL", "stopped"))
            context.stop = True

        def later_check(_context: DoctorRunContext) -> None:
            calls.append("later")

        checks = (
            DoctorCheck("stop_check", ("config",), ("stopped",), stop_check),
            DoctorCheck("later_check", ("stopped",), ("later",), later_check),
        )

        context = self.doctor_context()
        _run_doctor_registry(context, checks)

        self.assertEqual(calls, ["stop"])
        self.assertEqual([result.message for result in context.results], ["stopped"])

    def test_check_smb_port_reports_local_socket_error(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.tcp_connect_error", return_value="[Errno 113] No route to host"):
            result = check_smb_port("10.0.0.2")

        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.message, "SMB not reachable at 10.0.0.2:445 ([Errno 113] No route to host)")
        self.assertEqual(result.details, {"error": "[Errno 113] No route to host"})

    def test_run_doctor_checks_adds_socket_debug_when_direct_smb_is_unreachable(self) -> None:
        debug_fields: dict[str, object] = {}
        socket_debug = "smbd:\nroot smbd 101 10 internet stream tcp 0x0 *:445\nnbns-advertiser:\n(no internet sockets reported)"
        socket_debug_mock = mock.Mock(return_value=socket_debug)

        self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("WARN", "SMB not reachable at 10.0.0.2:445 ([Errno 113] No route to host)"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor.read_remote_service_socket_diagnostics_conn": socket_debug_mock,
            },
        )

        self.assertEqual(debug_fields["remote_service_sockets"], socket_debug)
        socket_debug_mock.assert_called_once()

    def test_run_doctor_checks_reports_info_when_optional_nbns_fails(self) -> None:
        debug_fields: dict[str, object] = {}
        socket_debug_mock = mock.Mock(return_value="smbd:\n(no internet sockets reported)\nnbns-advertiser:\nroot nbns-advertiser 201 7 internet dgram udp 0x0 *:137")

        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=CheckResult("PASS", "SMB reachable at 10.0.0.2:445"),
            xattr_result=CheckResult("PASS", "xattr ok"),
            read_active_smb_conf="[global]\n    netbios name = TimeCapsule\n[Data]\n",
            skip_smb=True,
            debug_fields=debug_fields,
            extra_patches={
                "timecapsulesmb.checks.doctor.nbns_flash_config_enabled_conn": mock.Mock(return_value=True),
                "timecapsulesmb.checks.doctor.read_interface_ipv4_conn": mock.Mock(return_value="10.0.0.2"),
                "timecapsulesmb.checks.doctor.check_nbns_name_resolution": mock.Mock(
                    return_value=CheckResult("FAIL", "NBNS query for 'TimeCapsule' timed out against 10.0.0.2:137")
                ),
                "timecapsulesmb.checks.doctor.read_remote_service_socket_diagnostics_conn": socket_debug_mock,
            },
        )

        self.assertFalse(run.fatal)
        nbns_result = next(result for result in run.results if "optional NBNS check failed" in result.message)
        self.assertEqual(nbns_result.status, "INFO")
        self.assertIn("timed out against 10.0.0.2:137", nbns_result.message)
        self.assertNotIn("remote_service_sockets", debug_fields)
        socket_debug_mock.assert_not_called()

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

    def test_run_doctor_checks_adds_bonjour_debug_on_instance_mismatch(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "Home",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Home",
            "TC_MDNS_HOST_LABEL": "home",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local."],
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
        debug_fields: dict[str, object] = {}
        native_diagnostics = {"status": "ok"}

        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                        return_value=(BonjourDiscoverySnapshot([], []), None, diagnostics),
                    ):
                        with mock.patch("timecapsulesmb.checks.doctor.browse_native_dns_sd", return_value=native_diagnostics) as native_mock:
                            results, fatal = run_doctor_checks(
                                self.doctor_config(values),
                                repo_root=REPO_ROOT,
                                skip_ssh=True,
                                skip_smb=True,
                                debug_fields=debug_fields,
                            )

        self.assertTrue(fatal)
        self.assertTrue(any(result.status == "FAIL" and "no resolved _smb._tcp service matched target IP 10.0.0.2" in result.message for result in results))
        self.assertEqual(debug_fields["bonjour_expected"], {"instance_name": None, "host_label": None, "target_ip": "10.0.0.2"})
        self.assertIs(debug_fields["bonjour_zeroconf"], diagnostics)
        self.assertIs(debug_fields["bonjour_native_dns_sd"], native_diagnostics)
        native_mock.assert_called_once_with()

    def test_run_doctor_checks_does_not_run_native_dns_sd_when_bonjour_matches(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "Home",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        debug_fields: dict[str, object] = {}

        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.browse_native_dns_sd", side_effect=AssertionError("native dns-sd should not run")):
                        results, fatal = run_doctor_checks(
                            self.doctor_config(values),
                            repo_root=REPO_ROOT,
                            skip_ssh=True,
                            skip_smb=True,
                            debug_fields=debug_fields,
                        )

        self.assertFalse(fatal)
        self.assertEqual(debug_fields, {})
        self.assertTrue(any(result.status == "PASS" and "discovered _smb._tcp" in result.message for result in results))

    def test_run_doctor_checks_uses_ip_only_bonjour_fallback_when_runtime_name_probe_fails(self) -> None:
        run = self.run_doctor_with_mocks(
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            skip_smb=True,
            extra_patches={
                "timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn": mock.Mock(side_effect=RuntimeError("probe failed")),
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "WARN" and "runtime naming identity probe skipped: probe failed" in result.message for result in run.results))
        self.assertTrue(any(result.status == "PASS" and "discovered _smb._tcp service matching target IP 10.0.0.2" in result.message for result in run.results))

    def test_run_doctor_checks_warns_when_bonjour_record_includes_link_local_ip(self) -> None:
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
                "timecapsulesmb.checks.doctor.discover_smb_services_detailed": mock.Mock(
                    return_value=(BonjourDiscoverySnapshot([instance], [record]), None, None)
                ),
            },
        )

        self.assertFalse(run.fatal)
        self.assertTrue(
            any(
                result.status == "WARN"
                and "Bonjour host timecapsulesamba4.local also advertised link-local IPv4 169.254.44.9" in result.message
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
                "timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn": mock.Mock(side_effect=RuntimeError("probe failed")),
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

    def test_run_doctor_checks_keeps_original_result_when_native_dns_sd_diagnostic_fails(self) -> None:
        values = {
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "Home",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Home",
            "TC_MDNS_HOST_LABEL": "home",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        diagnostics = BonjourDiscoveryDiagnostics(
            service="_smb",
            service_types=["_smb._tcp.local."],
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
        debug_fields: dict[str, object] = {}

        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                        return_value=(BonjourDiscoverySnapshot([], []), None, diagnostics),
                    ):
                        with mock.patch("timecapsulesmb.checks.doctor.browse_native_dns_sd", side_effect=RuntimeError("dns-sd broke")):
                            results, fatal = run_doctor_checks(
                                self.doctor_config(values),
                                repo_root=REPO_ROOT,
                                skip_ssh=True,
                                skip_smb=True,
                                debug_fields=debug_fields,
                            )

        self.assertTrue(fatal)
        self.assertTrue(any(result.status == "FAIL" and "no resolved _smb._tcp service matched target IP 10.0.0.2" in result.message for result in results))
        self.assertEqual(debug_fields["bonjour_native_dns_sd_error"], "RuntimeError: dns-sd broke")

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login"):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port"):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing"):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[]):
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
        self.assertEqual([r.message for r in results], ["missing local tool smbclient", "found local tool ssh"])

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

        discover_mock.assert_called_once_with("_smb", timeout=3.5, target_ip=None)
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

        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip=None)
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

        discover_mock.assert_called_once_with(None, timeout=3.5, target_ip="10.0.1.77")
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
        resolve_mock.assert_called_once_with(instance, timeout_ms=3000, target_ip=None)
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

        resolve_mock.assert_called_once_with(instance, timeout_ms=3000, target_ip="10.0.1.77")
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

    def test_check_bonjour_host_ip_fails_when_dns_resolves_wrong_ip(self) -> None:
        addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.1.99", 0))]
        with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
            result = check_bonjour_host_ip("home.local", expected_ip="10.0.1.1")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("expected 10.0.1.1", result.message)

    def test_check_bonjour_host_link_local_ips_returns_none_for_lan_only(self) -> None:
        result = check_bonjour_host_link_local_ips(
            "home.local",
            expected_ip="10.0.1.1",
            record_ips=["10.0.1.1"],
        )
        self.assertIsNone(result)

    def test_check_bonjour_host_link_local_ips_warns_for_link_local_extra_ip(self) -> None:
        result = check_bonjour_host_link_local_ips(
            "home.local",
            expected_ip="10.0.1.1",
            record_ips=["10.0.1.1", "169.254.44.9"],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "WARN")
        self.assertIn("169.254.44.9", result.message)
        self.assertIn("stale mDNS cache", result.message)

    def test_check_bonjour_host_link_local_ips_ignores_non_link_local_extra_ip(self) -> None:
        result = check_bonjour_host_link_local_ips(
            "home.local",
            expected_ip="10.0.1.1",
            record_ips=["10.0.1.1", "10.0.1.2"],
        )
        self.assertIsNone(result)

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

    def test_run_doctor_checks_infos_missing_sshpass_for_netbsd6(self) -> None:
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
        self.assertFalse(any(result.status == "FAIL" and "sshpass" in result.message for result in run.results))
        self.assertTrue(any(result.status == "INFO" and "sshpass not installed" in result.message for result in run.results))

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor.check_smb_port",
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
            with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                    with mock.patch(
                        "timecapsulesmb.checks.doctor.check_smb_port",
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
            remote_interface_probe=RemoteInterfaceProbeResult(
                iface="bridge0",
                exists=False,
                detail="interface bridge0 was not found on the device",
            ),
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS takeover active"),
        )
        self.assertFalse(run.fatal)
        self.assertFalse(any("TC_NET_IFACE is invalid" in result.message for result in run.results))
        run.mocks.probe_remote_interface_conn.assert_not_called()

    def test_run_doctor_checks_uses_precomputed_connection_and_interface_probe(self) -> None:
        connection = SshConnection("root@10.0.0.9", "pw", "-o injected")
        interface_probe = RemoteInterfaceProbeResult(
            iface="bridge0",
            exists=True,
            detail="interface bridge0 exists",
        )
        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            command_exists=True,
            read_active_smb_conf="",
            xattr_result=CheckResult("WARN", "xattr skipped"),
            smb_port=CheckResult("PASS", "445 ok"),
            remote_interface_probe=RemoteInterfaceProbeResult(
                iface="bridge0",
                exists=True,
                detail="unused interface probe",
            ),
            connection=connection,
            precomputed_interface_probe=interface_probe,
            skip_bonjour=True,
            skip_smb=True,
        )
        self.assertFalse(run.fatal)
        self.assertTrue(any(result.status == "PASS" and result.message == "ssh ok" for result in run.results))
        run.mocks.check_ssh_login.assert_called_once_with(connection)
        run.mocks.probe_remote_interface_conn.assert_not_called()

    def test_run_doctor_checks_does_not_reprobe_precomputed_interface(self) -> None:
        connection = SshConnection("root@10.0.0.9", "pw", "-o injected")
        stale_interface_probe = RemoteInterfaceProbeResult(
            iface="bridge1",
            exists=True,
            detail="interface bridge1 exists",
        )
        fresh_interface_probe = RemoteInterfaceProbeResult(
            iface="bridge0",
            exists=True,
            detail="interface bridge0 exists",
        )
        run = self.run_doctor_with_mocks(
            ssh_login=CheckResult("PASS", "ssh ok"),
            command_exists=True,
            read_active_smb_conf="",
            xattr_result=CheckResult("WARN", "xattr skipped"),
            smb_port=CheckResult("PASS", "445 ok"),
            remote_interface_probe=fresh_interface_probe,
            connection=connection,
            precomputed_interface_probe=stale_interface_probe,
            skip_bonjour=True,
            skip_smb=True,
        )
        run.mocks.probe_remote_interface_conn.assert_not_called()

    def test_run_doctor_checks_reports_managed_mdns_takeover_state(self) -> None:
        debug_fields: dict[str, object] = {}
        log_tail_mock = mock.Mock(return_value={
            "remote_rc_local_log_tail": "rc log",
            "remote_mdns_log_tail": "mdns log",
        })
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=False, detail="managed mDNS takeover not active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
            debug_fields=debug_fields,
            extra_patches={"timecapsulesmb.checks.doctor.read_runtime_log_tails_conn": log_tail_mock},
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any("managed mDNS takeover is not active" in result.message for result in run.results))
        self.assertEqual(debug_fields["remote_rc_local_log_tail"], "rc log")
        self.assertEqual(debug_fields["remote_mdns_log_tail"], "mdns log")
        log_tail_mock.assert_called_once()

    def test_run_doctor_checks_reports_managed_smbd_subchecks(self) -> None:
        smbd_probe = mock.Mock(
            ready=False,
            detail="smbd is not bound to IPv4 TCP 445",
            lines=(
                "PASS:managed runtime smb.conf present",
                "PASS:managed smbd parent process is running",
                "FAIL:smbd is not bound to IPv4 TCP 445",
            ),
        )
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            smbd_probe=smbd_probe,
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS takeover active"),
            run_ssh_stdout="[global]\n xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n",
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any(result.status == "PASS" and result.message == "managed smbd parent process is running" for result in run.results))
        self.assertTrue(any(result.status == "FAIL" and result.message == "smbd is not bound to IPv4 TCP 445" for result in run.results))
        self.assertFalse(any(result.message.startswith("managed smbd is not ready") for result in run.results))

    def test_run_doctor_checks_reports_supported_device_compatibility(self) -> None:
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_instance=[],
            smb_listing=self.smb_listing_result(),
            smb_file_ops=[],
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS takeover active"),
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
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS takeover active"),
            precomputed_probe_state=precomputed,
            extra_patches={
                "timecapsulesmb.checks.doctor.probe_connection_state": mock.Mock(
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
            mdns_probe=mock.Mock(ready=True, detail="managed mDNS takeover active"),
            extra_patches={"timecapsulesmb.checks.doctor.probe_connection_state": mock.Mock(return_value=probe_state)},
        )
        self.assertTrue(run.fatal)
        self.assertTrue(any(result.status == "FAIL" and "unknown-endian" in result.message for result in run.results))

    def test_ssh_opts_use_proxy_detects_proxycommand_and_proxyjump(self) -> None:
        self.assertTrue(ssh_opts_use_proxy("-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertTrue(ssh_opts_use_proxy("-o proxycommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertTrue(ssh_opts_use_proxy("-J bastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-Jbastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-o ProxyJump=bastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-o proxyjump=bastion.example.com"))
        self.assertTrue(ssh_opts_use_proxy("-oProxyCommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertTrue(ssh_opts_use_proxy("-oproxycommand=ssh\\ -W\\ %h:%p\\ bastion"))
        self.assertFalse(ssh_opts_use_proxy("-o HostKeyAlgorithms=+ssh-rsa"))

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

    def test_run_doctor_checks_proxy_target_skips_local_network_checks(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")) as ssh_mock:
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port") as smb_port_mock:
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance") as bonjour_mock:
                            with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1445):
                                with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward") as tunnel_mock:
                                    tunnel_mock.return_value.__enter__.return_value = None
                                    tunnel_mock.return_value.__exit__.return_value = None
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()) as smb_listing_mock:
                                        with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]) as smb_file_ops_mock:
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="enabled\n")):
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution") as nbns_mock:
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        ssh_mock.assert_called_once()
        self.assertEqual(ssh_mock.call_args.args[0], SshConnection("root@192.168.1.118", "pw", values["TC_SSH_OPTS"]))
        smb_port_mock.assert_not_called()
        bonjour_mock.assert_not_called()
        nbns_mock.assert_not_called()
        tunnel_mock.assert_called_once_with(
            mock.ANY,
            local_port=1445,
            remote_host="192.168.1.118",
            remote_port=445,
        )
        self.assertEqual(tunnel_mock.call_args.args[0].host, "root@192.168.1.118")
        self.assertEqual(tunnel_mock.call_args.args[0].ssh_opts, values["TC_SSH_OPTS"])
        smb_listing_mock.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            expected_share_name="Data",
            port=1445,
        )
        smb_file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "127.0.0.1",
            "Data",
            port=1445,
        )
        messages = [result.message for result in results if result.status == "SKIP"]
        self.assertTrue(any("direct SMB port check skipped" in message for message in messages))
        self.assertTrue(any("Bonjour check skipped" in message for message in messages))
        self.assertTrue(any("NBNS check skipped" in message for message in messages))
        self.assertFalse(any("authenticated SMB checks skipped" in message for message in messages))

    def test_run_doctor_checks_compact_jump_option_skips_local_network_checks(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-Jbastion.example.com -o HostKeyAlgorithms=+ssh-rsa",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port") as smb_port_mock:
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance") as bonjour_mock:
                            with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1446):
                                with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward") as tunnel_mock:
                                    tunnel_mock.return_value.__enter__.return_value = None
                                    tunnel_mock.return_value.__exit__.return_value = None
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()) as smb_listing_mock:
                                        with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]) as smb_file_ops_mock:
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="enabled\n")):
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution") as nbns_mock:
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        smb_port_mock.assert_not_called()
        bonjour_mock.assert_not_called()
        nbns_mock.assert_not_called()
        tunnel_mock.assert_called_once()
        smb_listing_mock.assert_called_once()
        smb_file_ops_mock.assert_called_once()
        messages = [result.message for result in results if result.status == "SKIP"]
        self.assertTrue(any("direct SMB port check skipped" in message for message in messages))
        self.assertTrue(any("Bonjour check skipped" in message for message in messages))
        self.assertTrue(any("NBNS check skipped" in message for message in messages))
        self.assertFalse(any("authenticated SMB checks skipped" in message for message in messages))

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.nbns_flash_config_enabled_conn") as nbns_config_mock:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[mock.Mock(status="PASS", message="bonjour ok")]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                        results, fatal = run_doctor_checks(
                                            self.doctor_config(values),
                                            repo_root=REPO_ROOT,
                                            on_result=lambda result: emitted.append(result.message),
                                        )
        self.assertFalse(fatal)
        self.assertEqual([result.message for result in results], emitted)

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=smb_results):
                                    with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                        results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT, skip_bonjour=True)
        self.assertFalse(fatal)
        self.assertEqual([result.message for result in results[-10:]], [result.message for result in smb_results])

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch(
                            "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                            return_value=(BonjourDiscoverySnapshot([bonjour_instance], [bonjour_record]), None, None),
                        ):
                            with mock.patch("timecapsulesmb.checks.doctor.resolve_smb_instance", return_value=(bonjour_record, None)):
                                with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.2", 0))]):
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                        with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                            with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=self.runtime_identity_from_values(values)):
                                                with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=active_smb_conf)):
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        info_messages = [result.message for result in results if result.status == "INFO"]
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

        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch(
                            "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                            return_value=(BonjourDiscoverySnapshot(instances, records), None, None),
                        ):
                            with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.217", 0))]):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result("james-s-airport-time-capsule.local")):
                                    with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=probed_identity):
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout=active_smb_conf)):
                                                results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)

        self.assertTrue(fatal)
        messages = [result.message for result in results]
        self.assertIn(
            "advertised Bonjour service targets for \"James's AirPort Time Capsule\": _airport=Jamess-AirPort-Time-Capsule.local; _smb=james-s-airport-time-capsule.local; _adisk=james-s-airport-time-capsule.local; _device-info=james-s-airport-time-capsule.local",
            messages,
        )
        self.assertIn(
            "Bonjour services for \"James's AirPort Time Capsule\" advertise inconsistent host targets: _airport=Jamess-AirPort-Time-Capsule.local; _smb=james-s-airport-time-capsule.local; _adisk=james-s-airport-time-capsule.local; _device-info=james-s-airport-time-capsule.local",
            messages,
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

        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch(
                            "timecapsulesmb.checks.doctor.discover_smb_services_detailed",
                            return_value=(BonjourDiscoverySnapshot([bonjour_instance], [bonjour_record]), None, None),
                        ):
                            with mock.patch("timecapsulesmb.checks.doctor.resolve_smb_instance", side_effect=AssertionError("fallback resolve should not run")):
                                with mock.patch("timecapsulesmb.core.net.socket.getaddrinfo", return_value=addrinfo):
                                        with mock.patch("timecapsulesmb.checks.doctor.check_bonjour_host_ip", side_effect=check_bonjour_host_ip):
                                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                                    with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=self.runtime_identity_from_values(values)):
                                                        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                                            results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)

        self.assertFalse(fatal)
        pass_messages = [result.message for result in results if result.status == "PASS"]
        self.assertIn("discovered _smb._tcp instance 'Home'", pass_messages)
        self.assertIn("resolved _smb._tcp instance 'Home' to home.local:445", pass_messages)
        self.assertIn("resolved Bonjour host home.local to 10.0.1.1", pass_messages)

    def test_run_doctor_checks_passes_expected_share_to_listing(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch(
                                "timecapsulesmb.checks.doctor.check_authenticated_smb_listing",
                                return_value=self.smb_listing_result(),
                            ) as listing_mock:
                                with mock.patch(
                                    "timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed",
                                    return_value=[mock.Mock(status="PASS", message="file ops ok")],
                                ) as file_ops_mock:
                                    with mock.patch(
                                        "timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn",
                                        return_value=self.runtime_identity_from_values(values),
                                    ):
                                        with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                            run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        listing_mock.assert_called_once_with(
            "admin",
            "pw",
            ["timecapsulesamba4.local", "10.0.0.2"],
            expected_share_name="Data",
        )
        file_ops_mock.assert_called_once_with(
            "admin",
            "pw",
            "timecapsulesamba4.local",
            "Data",
        )

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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=probed_identity):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result("time-capsule.local")) as listing_mock:
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[]):
                                    with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                        results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT, skip_bonjour=True)
        self.assertFalse(any("TC_MDNS_HOST_LABEL" in result.message for result in results))
        self.assertFalse(fatal)
        called_servers = listing_mock.call_args.args[2]
        self.assertIn("time-capsule.local", called_servers)
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

    def test_run_doctor_checks_adds_smb_listing_attempts_to_debug_fields(self) -> None:
        debug_fields: dict[str, object] = {}
        listing_attempts = [
            {"server": "timecapsulesamba4.local", "outcome": "timeout", "timeout_sec": 30},
            {"server": "10.0.0.2", "outcome": "timeout", "timeout_sec": 30},
        ]
        run = self.run_doctor_with_mocks(
            ssh_login=mock.Mock(status="PASS", message="ssh ok"),
            smb_port=mock.Mock(status="PASS", message="445 ok"),
            smb_listing=CheckResult(
                "FAIL",
                "authenticated SMB listing failed: timed out via 10.0.0.2",
                {"attempts": listing_attempts},
            ),
            smb_file_ops=[],
            debug_fields=debug_fields,
        )

        self.assertTrue(run.fatal)
        self.assertEqual(
            debug_fields["authenticated_smb_listing_servers"],
            ["timecapsulesamba4.local", "10.0.0.2"],
        )
        self.assertEqual(debug_fields["authenticated_smb_listing_expected_share"], "Data")
        self.assertEqual(debug_fields["authenticated_smb_listing_attempts"], listing_attempts)

    def test_check_authenticated_smb_file_ops_detailed_reports_each_step(self) -> None:
        def fake_run_local_capture(args, timeout=15):
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
        def fake_run_local_capture(args, timeout=15):
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

    def test_check_authenticated_smb_listing_uses_neutral_smbclient_config(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = check_authenticated_smb_listing("admin", "pw", "server.local", expected_share_name="Data")
        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(captured_args)
        self.assertEqual(captured_args[:3], ["smbclient", "-s", "/dev/null"])

    def test_check_authenticated_smb_listing_places_custom_port_before_dash_l_target(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=20):
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
            ["smbclient", "-s", "/dev/null", "-g", "-p", "1445", "-L", "//127.0.0.1", "-U", "admin%pw"],
        )

    def test_try_authenticated_smb_listing_forwards_custom_port(self) -> None:
        captured_args = None

        def fake_run_local_capture(args, timeout=30):
            nonlocal captured_args
            captured_args = args
            return subprocess.CompletedProcess(args, 0, "Data\n", "")

        with mock.patch("timecapsulesmb.checks.smb.command_exists", return_value=True):
            with mock.patch("timecapsulesmb.checks.smb.run_local_capture", side_effect=fake_run_local_capture):
                result = try_authenticated_smb_listing("admin", "pw", ["127.0.0.1"], port=2445)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(captured_args[3:6], ["-g", "-p", "2445"])

    def test_extract_nbns_response_ip_reads_first_answer_ipv4(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00\x01,\x00\x06\x00\x00"
            + b"\xc0\xa8\x01\xd9"
        )
        self.assertEqual(extract_nbns_response_ip(packet), "192.168.1.217")

    def test_extract_nbns_response_ip_returns_none_for_truncated_name(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA"
        )
        self.assertIsNone(extract_nbns_response_ip(packet))

    def test_extract_nbns_response_ip_returns_none_for_truncated_answer_header(self) -> None:
        packet = (
            b"\x13\x37\x85\x00\x00\x01\x00\x01\x00\x00\x00\x00"
            + b"\x20" + b"FEEFFDFECACACACACACACACACACACAAA" + b"\x00"
            + b"\x00\x20\x00\x01"
            + b"\xc0\x0c\x00\x20\x00\x01\x00\x00"
        )
        self.assertIsNone(extract_nbns_response_ip(packet))

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

    def test_run_doctor_checks_skips_nbns_when_flash_config_disabled(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                        results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if "NBNS responder not enabled" in result.message)
        self.assertEqual(nbns_result.status, "SKIP")
        nbns_index = results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)

    def test_run_doctor_checks_checks_nbns_when_flash_config_enabled(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mock.Mock(ready=True, detail="managed mDNS takeover active")):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=self.runtime_identity_from_values(values)):
                                            with mock.patch(
                                                "timecapsulesmb.device.probe.run_ssh",
                                                side_effect=[
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                    mock.Mock(stdout=""),
                                                ],
                                            ) as run_ssh_mock:
                                                with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.message == "nbns ok")
        self.assertEqual(nbns_result.status, "PASS")
        nbns_index = results.index(nbns_result)
        listing_index = next(i for i, result in enumerate(results) if result.message == "listing ok")
        self.assertLess(nbns_index, listing_index)
        self.assertEqual(run_ssh_mock.call_count, 3)
        nbns_mock.assert_called_once_with("TimeCapsule", "10.0.0.2", "10.0.0.2")

    def test_run_doctor_checks_resolves_nbns_expected_ip_from_hostname(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mock.Mock(ready=True, detail="managed mDNS takeover active")):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=self.runtime_identity_from_values(values)):
                                            with mock.patch(
                                                "timecapsulesmb.device.probe.run_ssh",
                                                side_effect=[
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                ],
                                            ):
                                                with mock.patch("timecapsulesmb.checks.doctor.resolve_host_ipv4s", return_value=("192.168.1.217",)):
                                                    with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                        results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        self.assertEqual(next(result for result in results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "timecapsule.local", "192.168.1.217")

    def test_run_doctor_checks_uses_interface_ip_for_nbns_expected_ip(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mock.Mock(ready=True, detail="managed mDNS takeover active")):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_remote_runtime_naming_identity_conn", return_value=self.runtime_identity_from_values(values)):
                                            with mock.patch(
                                                "timecapsulesmb.device.probe.run_ssh",
                                                side_effect=[
                                                    mock.Mock(stdout="[global]\n    netbios name = TimeCapsule\nxattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n[Data]\n"),
                                                    mock.Mock(stdout="xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb\n"),
                                                    mock.Mock(stdout="enabled\n"),
                                                    mock.Mock(stdout="192.168.1.217\n"),
                                                ],
                                            ):
                                                with mock.patch("timecapsulesmb.checks.doctor.resolve_host_ipv4s", return_value=("192.168.1.217",)):
                                                    with mock.patch("timecapsulesmb.checks.doctor.check_nbns_name_resolution", return_value=mock.Mock(status="PASS", message="nbns ok")) as nbns_mock:
                                                        results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        self.assertEqual(next(result for result in results if result.message == "nbns ok").status, "PASS")
        nbns_mock.assert_called_once_with("TimeCapsule", "wan.example.com", "192.168.1.217")

    def test_run_doctor_checks_warns_when_nbns_flash_config_probe_fails(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch(
                                        "timecapsulesmb.checks.doctor.probe_remote_interface_conn",
                                        return_value=RemoteInterfaceProbeResult(iface="bridge0", exists=True, detail="interface bridge0 exists"),
                                    ):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mock.Mock(ready=True, detail="managed mDNS takeover active")):
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                                with mock.patch("timecapsulesmb.checks.doctor.nbns_flash_config_enabled_conn", side_effect=RuntimeError("flash config probe failed")):
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("flash config probe failed", nbns_result.message)

    def test_run_doctor_checks_warns_when_nbns_flash_config_probe_raises_transport_error(self) -> None:
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
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.check_smb_port", return_value=mock.Mock(status="PASS", message="445 ok")):
                        with mock.patch("timecapsulesmb.checks.doctor.check_smb_instance", return_value=[]):
                            with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_listing", return_value=self.smb_listing_result()):
                                with mock.patch("timecapsulesmb.checks.doctor.check_authenticated_smb_file_ops_detailed", return_value=[mock.Mock(status="PASS", message="file ops ok")]):
                                    with mock.patch(
                                        "timecapsulesmb.checks.doctor.probe_remote_interface_conn",
                                        return_value=RemoteInterfaceProbeResult(iface="bridge0", exists=True, detail="interface bridge0 exists"),
                                    ):
                                        with mock.patch("timecapsulesmb.checks.doctor.probe_managed_mdns_takeover_conn", return_value=mock.Mock(ready=True, detail="managed mDNS takeover active")):
                                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                                with mock.patch("timecapsulesmb.checks.doctor.nbns_flash_config_enabled_conn", side_effect=SshError("ssh failed")):
                                                    results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT)
        self.assertFalse(fatal)
        nbns_result = next(result for result in results if result.status == "WARN" and result.message.startswith("NBNS check skipped:"))
        self.assertIn("ssh failed", nbns_result.message)

    def test_check_authenticated_smb_file_ops_detailed_passes_custom_port_to_smbclient(self) -> None:
        captured_args: list[list[str]] = []

        def fake_run_local_capture(args, timeout=15):
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

    def test_run_doctor_checks_proxy_target_reports_tunnel_failure_as_fatal(self) -> None:
        values = {
            "TC_HOST": "root@192.168.1.118",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyCommand=ssh\\ -W\\ %h:%p\\ bastion",
            "TC_NET_IFACE": "bridge0",
            "TC_SAMBA_USER": "admin",
            "TC_NETBIOS_NAME": "TimeCapsule",
            "TC_PAYLOAD_DIR_NAME": "samba4",
            "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
            "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
            "TC_MDNS_DEVICE_MODEL": "TimeCapsule8,119",
            "TC_AIRPORT_SYAP": "119",
        }
        with mock.patch("timecapsulesmb.checks.doctor.check_required_local_tools", return_value=[]):
            with mock.patch("timecapsulesmb.checks.doctor.check_required_artifacts", return_value=[]):
                with mock.patch("timecapsulesmb.checks.doctor.check_ssh_login", return_value=mock.Mock(status="PASS", message="ssh ok")):
                    with mock.patch("timecapsulesmb.checks.doctor.find_free_local_port", return_value=1445):
                        with mock.patch("timecapsulesmb.checks.doctor.ssh_local_forward", side_effect=SshError("tunnel failed")):
                            with mock.patch("timecapsulesmb.device.probe.run_ssh", return_value=mock.Mock(stdout="")):
                                results, fatal = run_doctor_checks(self.doctor_config(values), repo_root=REPO_ROOT, skip_bonjour=True)
        self.assertTrue(fatal)
        smb_result = next(result for result in results if result.message.startswith("authenticated SMB checks failed through SSH tunnel:"))
        self.assertEqual(smb_result.status, "FAIL")
        self.assertIn("tunnel failed", smb_result.message)


if __name__ == "__main__":
    unittest.main()
