from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import parse_env_value
from timecapsulesmb.core.paths import package_project_root, resolve_app_paths


BOOTSTRAP_PATH = package_project_root() / ".bootstrap"


@dataclass(frozen=True)
class InstallIdentity:
    install_id: str | None
    telemetry_enabled: bool


def default_bootstrap_path() -> Path:
    return resolve_app_paths().bootstrap_path


def parse_bootstrap_values(path: Path | None = None) -> dict[str, str]:
    resolved_path = path or default_bootstrap_path()
    values: dict[str, str] = {}
    try:
        text = resolved_path.read_text()
    except FileNotFoundError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = parse_env_value(value)
    return values


TELEMETRY_ENABLED_VALUES = frozenset({"true", "1", "yes", "on"})


def load_install_identity(path: Path | None = None) -> InstallIdentity:
    values = parse_bootstrap_values(path)
    telemetry_raw = values.get("TELEMETRY", "").strip().lower()
    telemetry_enabled = telemetry_raw in TELEMETRY_ENABLED_VALUES
    return InstallIdentity(
        install_id=values.get("INSTALL_ID") or None,
        telemetry_enabled=telemetry_enabled,
    )


def render_bootstrap_text(install_id: str, *, telemetry_enabled: bool = False) -> str:
    lines = [
        f"INSTALL_ID={install_id}",
        f"TELEMETRY={'true' if telemetry_enabled else 'false'}",
        "",
    ]
    return "\n".join(lines)


def ensure_install_id(path: Path | None = None) -> str:
    resolved_path = path or default_bootstrap_path()
    identity = load_install_identity(resolved_path)
    if identity.install_id:
        return identity.install_id
    install_id = str(uuid.uuid4())
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(render_bootstrap_text(install_id, telemetry_enabled=identity.telemetry_enabled))
    return install_id


def set_telemetry_enabled(enabled: bool, path: Path | None = None) -> InstallIdentity:
    resolved_path = path or default_bootstrap_path()
    install_id = ensure_install_id(resolved_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(render_bootstrap_text(install_id, telemetry_enabled=enabled))
    return load_install_identity(resolved_path)
