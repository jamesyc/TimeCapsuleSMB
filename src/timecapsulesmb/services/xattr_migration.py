from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from timecapsulesmb.core.config import parse_env_value
from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.transport.ssh import SshConnection, run_scp, run_ssh


XATTR_UPGRADE_RECEIPT = "/mnt/Flash/xattr-upgrade.state"
XATTR_UPGRADE_FORMAT = 1
XATTR_MIGRATION_VERSION = 1
NEW_RUNTIME_VERSION_CODE = 30000
MIGRATOR_RAM_PATH = "/mnt/Memory/tc-xattr-hfs-migrate"
MIGRATION_STATUS_PATH = "/mnt/Memory/tc-xattr-upgrade.status"
_BEGIN_CONFIG = "__TC_CONFIG_BEGIN__"
_END_CONFIG = "__TC_CONFIG_END__"
_BEGIN_RECEIPT = "__TC_RECEIPT_BEGIN__"
_END_RECEIPT = "__TC_RECEIPT_END__"

MigrationState = Literal[
    "clean",
    "legacy",
    "incomplete",
    "ready",
    "installed",
    "ambiguous",
]


@dataclass(frozen=True)
class MigrationReceipt:
    state: Literal["incomplete", "complete"]
    source_release: str
    source_version_code: int
    source_backend: str
    device_identity: str
    payload_identity: str
    volumes: tuple[str, ...]


@dataclass(frozen=True)
class InstallationEvidence:
    config_text: str = ""
    receipt_text: str = ""
    rc_local: bool = False
    boot: bool = False
    manager: bool = False
    service: bool = False
    legacy_start: bool = False
    payload_marker: bool = False


@dataclass(frozen=True)
class MigrationPrerequisite:
    state: MigrationState
    allowed: bool
    release_tag: str | None
    version_code: int | None
    receipt: MigrationReceipt | None
    reason: str
    source_backend: str = "netatalk"


@dataclass(frozen=True)
class XattrMigrationPlan:
    prerequisite: MigrationPrerequisite
    migrator_path: Path
    source_backend: str
    device_identity: str
    payload_identity: str
    tdb_path: str
    volumes: tuple[MaStVolume, ...]


@dataclass(frozen=True)
class XattrMigrationStatus:
    state: str
    operation_id: str | None = None
    phase: str | None = None
    entries: int = 0
    conversions: int = 0
    warnings: int = 0
    errors: int = 0
    detail: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in {"complete", "incomplete", "cancelled", "failed", "not_running"}


class InvalidMigrationReceipt(ValueError):
    pass


def _parse_assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        values[key] = parse_env_value(raw_value)
    return values


def parse_migration_receipt(text: str) -> MigrationReceipt | None:
    if not text.strip():
        return None
    scalar: dict[str, str] = {}
    volumes: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key or any(ord(char) < 32 for char in value):
            raise InvalidMigrationReceipt("malformed xattr migration receipt")
        if key == "volume":
            if not value or value in volumes:
                raise InvalidMigrationReceipt("invalid volume identity in xattr migration receipt")
            volumes.append(value)
            continue
        if key in scalar:
            raise InvalidMigrationReceipt(f"duplicate {key} in xattr migration receipt")
        scalar[key] = value
    required = {
        "format",
        "migration",
        "state",
        "source_release",
        "source_version_code",
        "source_backend",
        "device_identity",
        "payload_identity",
    }
    if set(scalar) != required:
        raise InvalidMigrationReceipt("incomplete or unsupported xattr migration receipt")
    try:
        format_version = int(scalar["format"])
        migration_version = int(scalar["migration"])
        source_version_code = int(scalar["source_version_code"])
    except ValueError as exc:
        raise InvalidMigrationReceipt("non-numeric xattr migration receipt version") from exc
    if format_version != XATTR_UPGRADE_FORMAT or migration_version != XATTR_MIGRATION_VERSION:
        raise InvalidMigrationReceipt("unsupported xattr migration receipt version")
    state = scalar["state"]
    if state not in {"incomplete", "complete"}:
        raise InvalidMigrationReceipt("invalid xattr migration receipt state")
    if source_version_code < 0 or not scalar["device_identity"] or not scalar["payload_identity"]:
        raise InvalidMigrationReceipt("invalid xattr migration receipt identity")
    if scalar["source_backend"] not in {"stream", "netatalk"}:
        raise InvalidMigrationReceipt("invalid xattr migration source backend")
    if not volumes:
        raise InvalidMigrationReceipt("xattr migration receipt has no selected volumes")
    return MigrationReceipt(
        state=state,
        source_release=scalar["source_release"],
        source_version_code=source_version_code,
        source_backend=scalar["source_backend"],
        device_identity=scalar["device_identity"],
        payload_identity=scalar["payload_identity"],
        volumes=tuple(volumes),
    )


def classify_migration_prerequisite(evidence: InstallationEvidence) -> MigrationPrerequisite:
    config = _parse_assignments(evidence.config_text)
    release_tag = config.get("TC_DEPLOY_RELEASE_TAG") or None
    source_backend = "stream" if config.get("FRUIT_METADATA_NETATALK", "1").lower() in {"0", "false", "no", "off"} else "netatalk"
    try:
        version_code = int(config["TC_DEPLOY_CLI_VERSION_CODE"])
    except (KeyError, ValueError):
        version_code = None
    try:
        receipt = parse_migration_receipt(evidence.receipt_text)
    except InvalidMigrationReceipt as exc:
        return MigrationPrerequisite("ambiguous", False, release_tag, version_code, None, str(exc), source_backend)

    if receipt is not None:
        if receipt.state == "incomplete":
            return MigrationPrerequisite(
                "incomplete",
                False,
                release_tag,
                version_code,
                receipt,
                "A previous xattr migration did not complete.",
                receipt.source_backend,
            )
        return MigrationPrerequisite(
            "ready",
            True,
            release_tag,
            version_code,
            receipt,
            "The explicit xattr migration completed for its selected volumes.",
            receipt.source_backend,
        )

    any_installation_evidence = any((
        evidence.rc_local,
        evidence.boot,
        evidence.manager,
        evidence.service,
        evidence.legacy_start,
        evidence.payload_marker,
        bool(evidence.config_text.strip()),
    ))
    if version_code is not None and version_code < NEW_RUNTIME_VERSION_CODE:
        return MigrationPrerequisite(
            "legacy",
            False,
            release_tag,
            version_code,
            None,
            "Installed TimeCapsuleSMB is older than 3.0.0.",
            source_backend,
        )
    if version_code is not None and version_code >= NEW_RUNTIME_VERSION_CODE:
        verified_runtime = evidence.rc_local and evidence.boot and (evidence.manager or evidence.service)
        if verified_runtime:
            return MigrationPrerequisite(
                "installed",
                True,
                release_tag,
                version_code,
                None,
                "An established 3.x runtime is installed.",
                source_backend,
            )
        return MigrationPrerequisite(
            "ambiguous",
            False,
            release_tag,
            version_code,
            None,
            "3.x version metadata exists without a coherent installed runtime.",
            source_backend,
        )
    if not any_installation_evidence:
        return MigrationPrerequisite("clean", True, None, None, None, "No prior installation evidence was found.", source_backend)
    return MigrationPrerequisite(
        "ambiguous",
        False,
        release_tag,
        version_code,
        None,
        "Legacy or mixed installation evidence exists without a trustworthy installed version.",
        source_backend,
    )


def parse_installation_evidence_output(output: str) -> InstallationEvidence:
    def section(begin: str, end: str) -> str:
        _, found, remainder = output.partition(begin + "\n")
        if not found:
            return ""
        value, found_end, _ = remainder.partition(end + "\n")
        if not found_end and remainder.rstrip().endswith(end):
            value = remainder.rstrip()[: -len(end)].rstrip("\n")
        return value

    flags: dict[str, bool] = {}
    for line in output.splitlines():
        if line.startswith("evidence:") and "=" in line:
            key, value = line[len("evidence:") :].split("=", 1)
            flags[key] = value == "1"
    return InstallationEvidence(
        config_text=section(_BEGIN_CONFIG, _END_CONFIG),
        receipt_text=section(_BEGIN_RECEIPT, _END_RECEIPT),
        rc_local=flags.get("rc_local", False),
        boot=flags.get("boot", False),
        manager=flags.get("manager", False),
        service=flags.get("service", False),
        legacy_start=flags.get("legacy_start", False),
        payload_marker=flags.get("payload_marker", False),
    )


def probe_migration_prerequisite(connection: SshConnection) -> MigrationPrerequisite:
    paths = {
        "rc_local": "/mnt/Flash/rc.local",
        "boot": "/mnt/Flash/boot.sh",
        "manager": "/mnt/Flash/manager.sh",
        "service": "/mnt/Flash/service",
        "legacy_start": "/mnt/Flash/start-samba.sh",
    }
    probes = "; ".join(
        f"if [ -e {shlex.quote(path)} ]; then echo evidence:{name}=1; else echo evidence:{name}=0; fi"
        for name, path in paths.items()
    )
    script = (
        f"{probes}; "
        "if [ -d /mnt/Flash/.samba4 ] || [ -L /root/tc-netbsd7 ] || "
        "[ -L /root/tc-netbsd4 ] || [ -L /root/tc-netbsd4le ] || [ -L /root/tc-netbsd4be ]; "
        "then echo evidence:payload_marker=1; else echo evidence:payload_marker=0; fi; "
        f"echo {_BEGIN_CONFIG}; "
        "if [ -f /mnt/Flash/tcapsulesmb.conf ]; then /usr/bin/sed -n '1,160p' /mnt/Flash/tcapsulesmb.conf; fi; "
        f"echo {_END_CONFIG}; echo {_BEGIN_RECEIPT}; "
        f"if [ -f {XATTR_UPGRADE_RECEIPT} ]; then /usr/bin/sed -n '1,80p' {XATTR_UPGRADE_RECEIPT}; fi; "
        f"echo {_END_RECEIPT}"
    )
    result = run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}")
    return classify_migration_prerequisite(parse_installation_evidence_output(result.stdout))


def _volume_identity(volume: MaStVolume) -> str:
    return f"uuid:{volume.adisk_uuid}" if volume.adisk_uuid else f"device:{volume.partition_device}"


def build_xattr_migration_plan(
    prerequisite: MigrationPrerequisite,
    *,
    migrator_path: Path,
    volumes: tuple[MaStVolume, ...],
    source_backend: str,
    device_identity: str,
    payload_volume: MaStVolume,
    payload_dir_name: str,
) -> XattrMigrationPlan:
    if prerequisite.state not in {"legacy", "incomplete"}:
        raise ValueError(f"xattr migration is not eligible in state {prerequisite.state}")
    if not volumes:
        raise ValueError("xattr migration requires at least one selected attached volume")
    if source_backend not in {"stream", "netatalk"}:
        raise ValueError("unsupported legacy metadata backend")
    if payload_volume not in volumes:
        raise ValueError("migration payload volume must be in the selected scope")
    payload_dir = f"{payload_volume.volume_root.rstrip('/')}/{payload_dir_name}"
    return XattrMigrationPlan(
        prerequisite=prerequisite,
        migrator_path=migrator_path,
        source_backend=source_backend,
        device_identity=device_identity,
        payload_identity=_volume_identity(payload_volume),
        tdb_path=f"{payload_dir}/private/xattr.tdb",
        volumes=volumes,
    )


def parse_xattr_migration_status(output: str) -> XattrMigrationStatus:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key):
            values[key] = value

    def count(name: str) -> int:
        try:
            value = int(values.get(name, "0"))
        except ValueError:
            return 0
        return max(value, 0)

    return XattrMigrationStatus(
        state=values.get("state", "not_running"),
        operation_id=values.get("operation_id") or None,
        phase=values.get("phase") or None,
        entries=count("entries"),
        conversions=count("conversions"),
        warnings=count("warnings"),
        errors=count("errors"),
        detail=values.get("detail", ""),
    )


def stage_xattr_migrator(connection: SshConnection, migrator_path: Path) -> None:
    capacity = run_ssh(connection, "/bin/df -k /mnt/Memory", timeout=30)
    try:
        available = int(capacity.stdout.splitlines()[-1].split()[3]) * 1024
    except (IndexError, ValueError) as exc:
        raise RuntimeError("could not measure migration RAM capacity") from exc
    required = migrator_path.stat().st_size + 64 * 1024
    if available < required:
        raise RuntimeError(
            f"not enough free migration RAM (available {available} bytes, need {required} bytes)"
        )
    run_scp(connection, migrator_path, f"{MIGRATOR_RAM_PATH}.new", timeout=300)
    script = (
        f"chmod 755 {MIGRATOR_RAM_PATH}.new && "
        f"{MIGRATOR_RAM_PATH}.new --version >/dev/null && "
        f"mv -f {MIGRATOR_RAM_PATH}.new {MIGRATOR_RAM_PATH}"
    )
    run_ssh(connection, f"/bin/sh -c {shlex.quote(script)}")


def start_xattr_migration(connection: SshConnection, plan: XattrMigrationPlan) -> XattrMigrationStatus:
    stage_xattr_migrator(connection, plan.migrator_path)
    source_release = plan.prerequisite.release_tag
    if source_release is None and plan.prerequisite.receipt is not None:
        source_release = plan.prerequisite.receipt.source_release
    source_version = plan.prerequisite.version_code
    if source_version is None and plan.prerequisite.receipt is not None:
        source_version = plan.prerequisite.receipt.source_version_code
    args = [
        MIGRATOR_RAM_PATH,
        "maintain",
        "--receipt",
        XATTR_UPGRADE_RECEIPT,
        "--status",
        MIGRATION_STATUS_PATH,
        "--source-release",
        source_release or "unknown",
        "--source-version",
        str(source_version or 0),
        "--backend",
        plan.source_backend,
        "--device-id",
        plan.device_identity,
        "--payload-id",
        plan.payload_identity,
        "--tdb",
        plan.tdb_path,
    ]
    for volume in plan.volumes:
        args.extend(("--volume", _volume_identity(volume), volume.volume_root))
    result = run_ssh(connection, shlex.join(args), timeout=120)
    status = parse_xattr_migration_status(result.stdout)
    if status.state not in {"running", "complete"}:
        raise RuntimeError(status.detail or "the device did not start xattr migration")
    return status


def query_xattr_migration(connection: SshConnection) -> XattrMigrationStatus:
    command = f"{MIGRATOR_RAM_PATH} status --status {MIGRATION_STATUS_PATH} --receipt {XATTR_UPGRADE_RECEIPT}"
    result = run_ssh(connection, command, check=False, timeout=30)
    if result.returncode not in {0, 3}:
        raise RuntimeError("could not query xattr migration status")
    return parse_xattr_migration_status(result.stdout)


def cancel_xattr_migration(connection: SshConnection) -> XattrMigrationStatus:
    command = f"{MIGRATOR_RAM_PATH} cancel --status {MIGRATION_STATUS_PATH}"
    result = run_ssh(connection, command, check=False, timeout=30)
    if result.returncode not in {0, 3}:
        raise RuntimeError("could not request xattr migration cancellation")
    return parse_xattr_migration_status(result.stdout)


def follow_xattr_migration(
    connection: SshConnection,
    *,
    on_status=None,
    interval_seconds: int = 2,
) -> XattrMigrationStatus:
    while True:
        status = query_xattr_migration(connection)
        if on_status is not None:
            on_status(status)
        if status.terminal:
            return status
        time.sleep(interval_seconds)
