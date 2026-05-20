from __future__ import annotations

# Compatibility shim for callers that imported or monkeypatched the original
# monolithic module. New code should import from timecapsulesmb.app.ops.

from collections.abc import Callable

from timecapsulesmb.app.events import EventSink
from timecapsulesmb.app.ops import configure as _configure
from timecapsulesmb.app.ops import deploy as _deploy
from timecapsulesmb.app.ops import doctor as _doctor
from timecapsulesmb.app.ops import maintenance as _maintenance
from timecapsulesmb.app.ops import readiness as _readiness
from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.device.storage import build_dry_run_payload_home
from timecapsulesmb.services.app import (
    AppOperationError,
    OperationResult,
    bool_param as _bool_param,
    config_path as _config_path,
    confirm_param as _confirm_param,
    float_param as _float_param,
    int_param as _int_param,
    jsonable as _jsonable,
    optional_int_param as _optional_int_param,
    require_string_param as _require_string_param,
    string_param as _string_param,
)


discover_snapshot = _readiness.discover_snapshot

probe_connection_state = _configure.probe_connection_state
enable_ssh = _configure.enable_ssh

load_env_config = _deploy.load_env_config
resolve_validated_managed_target = _deploy.resolve_validated_managed_target
resolve_app_paths = _deploy.resolve_app_paths
validate_artifacts = _deploy.validate_artifacts
resolve_payload_artifacts = _deploy.resolve_payload_artifacts
run_remote_actions = _deploy.run_remote_actions
wait_for_mast_volumes_conn = _deploy.wait_for_mast_volumes_conn
select_payload_home_with_diagnostics_conn = _deploy.select_payload_home_with_diagnostics_conn
verify_payload_home_conn = _deploy.verify_payload_home_conn
upload_deployment_payload = _deploy.upload_deployment_payload
flush_remote_filesystem_writes = _deploy.flush_remote_filesystem_writes
wait_for_ssh_state_conn = _deploy.wait_for_ssh_state_conn

resolve_env_connection = _maintenance.resolve_env_connection
remote_uninstall_payload = _maintenance.remote_uninstall_payload
read_mast_volumes_conn = _maintenance.read_mast_volumes_conn
mounted_mast_volumes_conn = _maintenance.mounted_mast_volumes_conn
run_ssh = _maintenance.run_ssh
probe_managed_runtime_conn = _maintenance.probe_managed_runtime_conn
load_optional_env_config = _maintenance.load_optional_env_config
repair_xattrs_cli = _maintenance.repair_xattrs_cli
sys = _maintenance.sys

run_doctor_checks = _doctor.run_doctor_checks


def _sync_compat_bindings() -> None:
    _readiness.discover_snapshot = discover_snapshot
    _readiness.resolve_app_paths = resolve_app_paths

    _configure.probe_connection_state = probe_connection_state
    _configure.enable_ssh = enable_ssh
    _configure.resolve_app_paths = resolve_app_paths

    _deploy.load_env_config = load_env_config
    _deploy.resolve_validated_managed_target = resolve_validated_managed_target
    _deploy.resolve_app_paths = resolve_app_paths
    _deploy.validate_artifacts = validate_artifacts
    _deploy.resolve_payload_artifacts = resolve_payload_artifacts
    _deploy.run_remote_actions = run_remote_actions
    _deploy.wait_for_mast_volumes_conn = wait_for_mast_volumes_conn
    _deploy.select_payload_home_with_diagnostics_conn = select_payload_home_with_diagnostics_conn
    _deploy.verify_payload_home_conn = verify_payload_home_conn
    _deploy.upload_deployment_payload = upload_deployment_payload
    _deploy.flush_remote_filesystem_writes = flush_remote_filesystem_writes
    _deploy.wait_for_ssh_state_conn = wait_for_ssh_state_conn

    _maintenance.load_env_config = load_env_config
    _maintenance.resolve_env_connection = resolve_env_connection
    _maintenance.remote_uninstall_payload = remote_uninstall_payload
    _maintenance.read_mast_volumes_conn = read_mast_volumes_conn
    _maintenance.mounted_mast_volumes_conn = mounted_mast_volumes_conn
    _maintenance.run_ssh = run_ssh
    _maintenance.wait_for_ssh_state_conn = wait_for_ssh_state_conn
    _maintenance.run_remote_actions = run_remote_actions
    _maintenance.probe_managed_runtime_conn = probe_managed_runtime_conn
    _maintenance.load_optional_env_config = load_optional_env_config
    _maintenance.repair_xattrs_cli = repair_xattrs_cli
    _maintenance.sys = sys

    _doctor.load_env_config = load_env_config
    _doctor.resolve_app_paths = resolve_app_paths
    _doctor.resolve_env_connection = resolve_env_connection
    _doctor.run_doctor_checks = run_doctor_checks


def discover_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _readiness.discover_operation(params, sink)


def paths_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _readiness.paths_operation(params, sink)


def validate_install_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _readiness.validate_install_operation(params, sink)


def configure_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _configure.configure_operation(params, sink)


def deploy_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _deploy.deploy_operation(params, sink)


def activate_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _maintenance.activate_operation(params, sink)


def uninstall_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _maintenance.uninstall_operation(params, sink)


def fsck_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _maintenance.fsck_operation(params, sink)


def repair_xattrs_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _maintenance.repair_xattrs_operation(params, sink)


def doctor_operation(params: dict[str, object], sink: EventSink) -> OperationResult:
    _sync_compat_bindings()
    return _doctor.doctor_operation(params, sink)


_selected_record_host = _readiness.selected_record_host
_selected_record_properties = _readiness.selected_record_properties
_snapshot_payload = _readiness.snapshot_payload
_wait_for_ssh_port = _configure.wait_for_ssh_port
_require_supported_payload = _deploy.require_supported_payload
_load_config_and_target = _deploy.load_config_and_target
_verify_payload_upload = _deploy.verify_payload_upload
_verify_runtime = _deploy.verify_runtime
_request_reboot_and_wait = _deploy.request_reboot_and_wait
_request_ssh_reboot = _deploy.request_ssh_reboot
_observe_reboot_cycle = _maintenance.observe_reboot_cycle
_RepairContext = _maintenance.RepairExecutionContext
_StreamLogCapture = _maintenance.LineLogCapture


OPERATIONS: dict[str, Callable[[dict[str, object], EventSink], OperationResult]] = {
    "activate": activate_operation,
    "configure": configure_operation,
    "deploy": deploy_operation,
    "discover": discover_operation,
    "doctor": doctor_operation,
    "fsck": fsck_operation,
    "paths": paths_operation,
    "repair-xattrs": repair_xattrs_operation,
    "uninstall": uninstall_operation,
    "validate-install": validate_install_operation,
}
