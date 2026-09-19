from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import MANAGED_PAYLOAD_DIR_NAME
from timecapsulesmb.deploy.artifact_resolver import resolve_payload_artifacts
from timecapsulesmb.deploy.artifacts import sha256_file
from timecapsulesmb.device.compat import require_compatibility
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.services import storage as storage_service
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.runtime import ManagedTargetState
from timecapsulesmb.services.xattr_migration import (
    MigrationPrerequisite,
    XattrMigrationStatus,
    build_xattr_migration_plan,
    cancel_xattr_migration,
    follow_xattr_migration,
    probe_migration_prerequisite,
    query_xattr_migration,
    start_xattr_migration,
)
from timecapsulesmb.transport.ssh import run_ssh


@dataclass(frozen=True)
class MigrateXattrResult:
    prerequisite: MigrationPrerequisite
    status: XattrMigrationStatus
    selected_volumes: tuple[str, ...] = ()


class MigrateXattrError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _payload_volume(connection, volumes: tuple[MaStVolume, ...]) -> MaStVolume | None:
    for volume in volumes:
        path = f"{volume.volume_root.rstrip('/')}/{MANAGED_PAYLOAD_DIR_NAME}"
        result = run_ssh(connection, f"test -d {shlex.quote(path)}", check=False, timeout=30)
        if result.returncode == 0:
            return volume
        if result.returncode not in {0, 1}:
            raise MigrateXattrError("Could not inspect the legacy payload.", code="migration_preflight_failed")
    return None


def _verified_migrator(distribution_root: Path, payload_family: str) -> Path:
    artifact = resolve_payload_artifacts(distribution_root, payload_family)["xattr_migrator"]
    if not artifact.absolute_path.is_file() or sha256_file(artifact.absolute_path) != artifact.sha256:
        raise MigrateXattrError("The matching xattr migrator is missing or corrupt.", code="artifact_validation_failed")
    return artifact.absolute_path


def inspect_xattr_migration(target: ManagedTargetState) -> MigrateXattrResult:
    prerequisite = probe_migration_prerequisite(target.connection)
    try:
        status = query_xattr_migration(target.connection)
    except RuntimeError:
        state = "complete" if prerequisite.state in {"ready", "installed"} else "not_running"
        status = XattrMigrationStatus(state=state, detail=prerequisite.reason)
    return MigrateXattrResult(prerequisite, status)


def run_xattr_migration(
    target: ManagedTargetState,
    distribution_root: Path,
    *,
    callbacks: OperationCallbacks | None = None,
    follow: bool = True,
    mount_wait_seconds: int = 30,
) -> MigrateXattrResult:
    callbacks = callbacks or OperationCallbacks()
    connection = target.connection
    callbacks.stage("migration_preflight")
    prerequisite = probe_migration_prerequisite(connection)
    if prerequisite.state in {"clean", "ready", "installed"}:
        return MigrateXattrResult(
            prerequisite,
            XattrMigrationStatus(state="complete", detail=prerequisite.reason),
        )
    if prerequisite.state == "ambiguous":
        raise MigrateXattrError(prerequisite.reason, code="migration_state_ambiguous")
    compatibility = require_compatibility(
        target.probe_state.compatibility if target.probe_state is not None else None,
        fallback_error="Could not determine the device architecture.",
    )
    if compatibility.payload_family is None:
        raise MigrateXattrError("No matching xattr migrator is available.", code="unsupported_device")
    migrator = _verified_migrator(distribution_root, compatibility.payload_family)

    callbacks.stage("migration_select_volumes")
    mounted = storage_service.mount_mast_volumes_with_diagnostics(
        connection,
        callbacks=callbacks,
        wait_seconds=mount_wait_seconds,
        mount_stage="migration_mount_volumes",
    )
    if not mounted:
        raise MigrateXattrError("No attached HFS volumes are available for migration.", code="migration_no_volumes")
    payload_volume = _payload_volume(connection, mounted)
    if payload_volume is None:
        raise MigrateXattrError("The legacy TimeCapsuleSMB payload is not on an attached volume.", code="migration_payload_missing")
    probe = target.probe_state.probe_result if target.probe_state is not None else None
    syap = getattr(probe, "airport_syap", None)
    device_identity = f"syAP:{syap}" if syap else f"host:{connection.host}"
    plan = build_xattr_migration_plan(
        prerequisite,
        migrator_path=migrator,
        volumes=mounted,
        source_backend=prerequisite.source_backend,
        device_identity=device_identity,
        payload_volume=payload_volume,
        payload_dir_name=MANAGED_PAYLOAD_DIR_NAME,
    )
    callbacks.stage("migration_start")
    status = start_xattr_migration(connection, plan)
    if follow and not status.terminal:
        callbacks.stage("migration_follow")

        def report(current: XattrMigrationStatus) -> None:
            callbacks.update(
                migration_operation_id=current.operation_id,
                migration_phase=current.phase,
                migration_entries=current.entries,
                migration_conversions=current.conversions,
                migration_warnings=current.warnings,
                migration_errors=current.errors,
            )

        status = follow_xattr_migration(connection, on_status=report)
    if status.state != "complete":
        raise MigrateXattrError(
            status.detail or "Metadata migration did not complete.",
            code="migration_incomplete",
        )
    return MigrateXattrResult(
        prerequisite,
        status,
        tuple(volume.volume_root for volume in mounted),
    )


def cancel_running_xattr_migration(target: ManagedTargetState) -> MigrateXattrResult:
    prerequisite = probe_migration_prerequisite(target.connection)
    return MigrateXattrResult(prerequisite, cancel_xattr_migration(target.connection))
