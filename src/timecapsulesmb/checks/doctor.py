from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional

from timecapsulesmb.checks.doctor_debug import (
    _add_bonjour_debug_fields,
    _doctor_add_fatal_runtime_log_tails,
    _doctor_add_mast_probe_on_disk_failure,
)
from timecapsulesmb.checks.doctor_state import DoctorInputs, DoctorOptions, DoctorSink
from timecapsulesmb.checks.doctor_steps import (
    DoctorNetworkProbe,
    _add_active_smb_conf_results,
    _add_bonjour_results,
    _doctor_check_usb_printer,
    _build_doctor_target,
    _doctor_add_bonjour_naming_info,
    _doctor_check_active_smb_conf,
    _doctor_check_admin_password,
    _doctor_check_authenticated_smb,
    _doctor_check_local_smbclient,
    _doctor_check_deployed_config,
    _doctor_check_running_migration,
    _doctor_check_deployed_version,
    _doctor_device_blocks,
    _doctor_check_device_smb,
    _doctor_check_local_network_permission,
    _doctor_report_device_not_checked,
    _doctor_airport_mac,
    _doctor_check_device_compatibility,
    _doctor_check_direct_smb_port,
    _doctor_check_managed_mdns,
    _doctor_check_managed_rsync,
    _doctor_check_device_hostname,
    _doctor_check_disk_errors_after_failure,
    _doctor_check_disk_errors_under_verdict,
    _doctor_check_disk_mounting,
    _doctor_check_disk_payload,
    _doctor_manager_starting,
    _doctor_refresh_payload_problems,
    _doctor_check_diskd_rpc,
    _doctor_check_managed_smbd,
    _doctor_check_nbns,
    _doctor_check_runtime_naming_identity,
    _doctor_check_runtime_ram_root,
    _doctor_check_ssh_login,
    _doctor_check_stuck_processes,
    _doctor_check_apple_acpd,
    _doctor_check_unshared_network,
    _doctor_report_unanswered_device,
    _doctor_probe_startup_age,
    _doctor_wait_for_startup,
    _doctor_validate_config,
)
from timecapsulesmb.checks.models import DOMAIN_COMPUTER, CheckResult
from timecapsulesmb.core.smb_config import parse_active_share_names
from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.device.probe import AirportAcpReading, ManagerVerdicts, ProbedDeviceState
from timecapsulesmb.transport.ssh import SshConnection


def run_doctor_checks(
    config: AppConfig,
    *,
    repo_root: Path,
    connection: SshConnection | None = None,
    precomputed_probe_state: ProbedDeviceState | None = None,
    device_reading: AirportAcpReading | None = None,
    skip_ssh: bool = False,
    skip_bonjour: bool = False,
    skip_smb: bool = False,
    local_network_permission: str | None = None,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    on_stage: Optional[Callable[[str, str | None], None]] = None,
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
        precomputed_probe_state=precomputed_probe_state,
        options=options,
        device_reading=device_reading,
        local_network_permission=local_network_permission,
    )
    sink = DoctorSink(
        on_result=on_result,
        debug_fields=debug_fields,
        on_stage=on_stage,
    )

    # This computer's own setup first: it decides whether anything else can run.
    with sink.in_section(DOMAIN_COMPUTER):
        config_decision = _doctor_validate_config(inputs, sink)
        _doctor_check_local_network_permission(inputs, sink)
        if config_decision.stop:
            return sink.results, sink.fatal()
        target = _build_doctor_target(inputs)
        remote = _doctor_check_ssh_login(target, options, sink)
        password_match = _doctor_check_admin_password(inputs, target, remote, sink)
    # The device, measured on the device over SSH: its verdict does not depend
    # on the network between this computer and the device.
    _doctor_report_device_not_checked(remote, sink)
    processes = _doctor_check_stuck_processes(target, remote, sink)
    if _doctor_check_apple_acpd(target, remote, processes, sink, inputs.device_reading).stop:
        # Kernel disk errors remain meaningful without ACPd. Its missing
        # inventory makes manager verdicts unreliable, so do not use them.
        _doctor_check_disk_errors_after_failure(target, remote, None, sink, samba_serving=False)
        # The manager's log shows what it could not read without ACPd.
        _doctor_add_fatal_runtime_log_tails(target, remote, sink, processes)
        return sink.results, sink.fatal()

    if _doctor_check_running_migration(target, remote, sink).stop:
        return sink.results, sink.fatal()
    if _doctor_check_deployed_config(target, remote, sink).stop:
        return sink.results, sink.fatal()
    if _doctor_check_deployed_version(target, remote, sink).stop:
        return sink.results, sink.fatal()
    if _doctor_check_runtime_ram_root(target, remote, sink).stop:
        return sink.results, sink.fatal()

    manager = _doctor_probe_startup_age(target, remote, sink)
    if (_doctor_check_disk_errors_under_verdict(target, remote, manager, sink).stop
            or _doctor_check_disk_payload(manager, sink, final=not _doctor_manager_starting(manager)).stop):
        # The manager's log shows what removed the payload, such as an Erase.
        _doctor_add_fatal_runtime_log_tails(target, remote, sink, processes)
        return sink.results, sink.fatal()
    if _doctor_check_disk_mounting(manager, sink).stop:
        _doctor_add_mast_probe_on_disk_failure(target, remote, sink, always=True)
        _doctor_add_fatal_runtime_log_tails(target, remote, sink, processes)
        return sink.results, sink.fatal()
    startup_age = manager.manager_started_seconds_ago if manager is not None else None
    verdicts = ManagerVerdicts(manager.payload_problems if manager is not None else ())
    readiness = _doctor_wait_for_startup(target, remote, sink, startup_age, verdicts)
    manager = _doctor_refresh_payload_problems(target, remote, manager, verdicts)
    if (_doctor_check_disk_errors_under_verdict(target, remote, manager, sink).stop
            or _doctor_check_disk_payload(manager, sink).stop):
        _doctor_add_fatal_runtime_log_tails(target, remote, sink, processes)
        return sink.results, sink.fatal()

    naming = _doctor_check_runtime_naming_identity(target, remote, sink)
    probed_mac = _doctor_check_device_compatibility(inputs, target, remote, sink)
    _doctor_check_device_hostname(target, remote, sink)
    smbd_serving = _doctor_check_managed_smbd(target, remote, sink, readiness.smbd)
    native_nbns, bonjour_registered, link_plan = _doctor_check_managed_mdns(target, remote, sink, readiness.mdns)
    _doctor_check_managed_rsync(target, remote, sink, manager, readiness.rsync)
    _doctor_check_diskd_rpc(target, remote, sink)
    smb_config = _doctor_check_active_smb_conf(target, remote, sink)
    if remote.remote_checks_enabled:
        _add_active_smb_conf_results(smb_config.text, smb_config.reason, sink.add)
    device_smb = _doctor_check_device_smb(inputs, target, remote, smb_config, processes, password_match, sink)
    # Everything below checks the device from this computer over the network;
    # what the device already reported down is not counted against it.
    sink.blocked_by_device = _doctor_device_blocks(smbd_serving, native_nbns, bonjour_registered)
    with sink.in_section(DOMAIN_COMPUTER):
        _doctor_check_local_smbclient(inputs, sink)
        if remote.device_unreachable:
            _doctor_report_unanswered_device(inputs, target, sink)
        elif not _doctor_check_unshared_network(remote, link_plan, sink):
            network = DoctorNetworkProbe(target, remote, sink.debug_fields)
            bonjour_result = _add_bonjour_results(
                inputs.config,
                naming.identity,
                skip_bonjour=inputs.options.skip_bonjour,
                active_share_names=parse_active_share_names(smb_config.text or ""),
                add_result=sink.add,
                network=network,
                airport_mac=_doctor_airport_mac(inputs, probed_mac),
            )
            direct_smb = _doctor_check_direct_smb_port(target, remote, bonjour_result.addresses, sink)
            _add_bonjour_debug_fields(
                sink.debug_fields,
                bonjour_debug_needed=bonjour_result.debug_needed,
                bonjour_expected_debug=bonjour_result.expected_debug,
                bonjour_discovery_debug=bonjour_result.discovery_debug,
            )
            _doctor_add_bonjour_naming_info(bonjour_result, sink)
            _doctor_check_usb_printer(target, remote, bonjour_result, sink, network)
            _doctor_check_nbns(target, remote, smb_config, naming, direct_smb, sink, native_nbns, network)
            _doctor_check_authenticated_smb(
                inputs, target, remote, smb_config, naming, bonjour_result, direct_smb, processes, device_smb, sink,
            )
    _doctor_add_mast_probe_on_disk_failure(target, remote, sink)
    _doctor_check_disk_errors_after_failure(target, remote, manager, sink, samba_serving=smbd_serving is True)
    _doctor_add_fatal_runtime_log_tails(target, remote, sink, processes)
    return sink.results, sink.fatal()
