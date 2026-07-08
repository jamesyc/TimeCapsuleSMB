from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.doctor_debug import (
    _add_bonjour_debug_fields,
    _doctor_add_fatal_runtime_log_tails,
    _doctor_add_mast_probe_on_disk_failure,
)
from timecapsulesmb.checks.doctor_state import DoctorBonjourResult, DoctorInputs, DoctorOptions, DoctorSink
from timecapsulesmb.checks.doctor_steps import (
    _add_active_smb_conf_results,
    _add_bonjour_results,
    _build_doctor_target,
    _doctor_add_bonjour_naming_info,
    _doctor_apply_startup_grace,
    _doctor_check_active_smb_conf,
    _doctor_check_authenticated_smb,
    _doctor_check_deployed_config,
    _doctor_check_deployed_version,
    _doctor_check_device_compatibility,
    _doctor_check_direct_smb_port,
    _doctor_check_managed_mdns,
    _doctor_check_managed_smbd,
    _doctor_check_network_plan,
    _doctor_check_nbns,
    _doctor_check_runtime_naming_identity,
    _doctor_check_runtime_ram_root,
    _doctor_check_ssh_login,
    _doctor_probe_startup_age,
    _doctor_validate_config,
    check_xattr_tdb_persistence,
)
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.smb_config import parse_active_share_names
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.probe import ProbedDeviceState, RemoteInterfaceProbeResult
from timecapsulesmb.transport.ssh import SshConnection


def run_doctor_checks(
    config: AppConfig,
    *,
    repo_root: Path,
    connection: SshConnection | None = None,
    precomputed_interface_probe: RemoteInterfaceProbeResult | None = None,
    precomputed_probe_state: ProbedDeviceState | None = None,
    skip_ssh: bool = False,
    skip_bonjour: bool = False,
    skip_smb: bool = False,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    debug_fields: dict[str, object] | None = None,
) -> tuple[list[CheckResult], bool]:
    options = DoctorOptions(
        skip_ssh=skip_ssh,
        skip_bonjour=skip_bonjour,
        skip_smb=skip_smb,
    )
    inputs = DoctorInputs(
        config=config,
        repo_root=repo_root,
        connection=connection,
        precomputed_interface_probe=precomputed_interface_probe,
        precomputed_probe_state=precomputed_probe_state,
        options=options,
    )
    sink = DoctorSink(
        on_result=on_result,
        debug_fields=debug_fields,
    )

    if _doctor_validate_config(inputs, sink).stop:
        return sink.results, sink.fatal()

    target = _build_doctor_target(inputs)
    remote = _doctor_check_ssh_login(target, options, sink)

    if _doctor_check_deployed_config(target, remote, sink).stop:
        return sink.results, sink.fatal()
    if _doctor_check_deployed_version(target, remote, sink).stop:
        return sink.results, sink.fatal()
    if _doctor_check_runtime_ram_root(target, remote, sink).stop:
        return sink.results, sink.fatal()

    startup_age = _doctor_probe_startup_age(target, remote, sink)

    naming = _doctor_check_runtime_naming_identity(target, remote, sink)
    _doctor_check_device_compatibility(inputs, target, remote, sink)
    _doctor_check_managed_smbd(target, remote, sink)
    _doctor_check_managed_mdns(target, remote, sink)
    smb_config = _doctor_check_active_smb_conf(target, remote, sink)
    network_plan = _doctor_check_network_plan(target, remote, smb_config, sink)
    _doctor_check_direct_smb_port(target, remote, network_plan, sink)
    bonjour_result = _add_bonjour_results(
        inputs.config,
        naming.identity,
        proxied_ssh=target.proxied_ssh,
        skip_bonjour=inputs.options.skip_bonjour,
        network_plan=network_plan.plan,
        active_share_names=parse_active_share_names(smb_config.text or ""),
        add_result=sink.add,
    )
    _add_bonjour_debug_fields(
        sink.debug_fields,
        bonjour_debug_needed=bonjour_result.debug_needed,
        bonjour_expected_debug=bonjour_result.expected_debug,
        bonjour_zeroconf_debug=bonjour_result.zeroconf_debug,
        bonjour_native_fallback_debug=bonjour_result.native_fallback_debug,
        bonjour_backend_debug=bonjour_result.backend_debug,
    )
    _doctor_add_bonjour_naming_info(bonjour_result, sink)
    _add_active_smb_conf_results(smb_config.text, smb_config.reason, sink.add)
    _doctor_check_nbns(target, remote, smb_config, naming, network_plan, sink)
    _doctor_check_authenticated_smb(inputs, target, smb_config, naming, bonjour_result, network_plan, sink)
    _doctor_add_mast_probe_on_disk_failure(target, remote, sink)
    _doctor_add_fatal_runtime_log_tails(target, remote, sink)
    _doctor_apply_startup_grace(sink, startup_age)
    return sink.results, sink.fatal()
