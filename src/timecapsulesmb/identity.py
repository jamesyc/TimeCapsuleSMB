from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.core.config import REPO_ROOT, parse_env_value


BOOTSTRAP_PATH = REPO_ROOT / ".bootstrap"


@dataclass(frozen=True)
class InstallIdentity:
    install_id: str | None
    telemetry_enabled: bool


def parse_bootstrap_values(path: Path = BOOTSTRAP_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = parse_env_value(value)
    return values


def load_install_identity(path: Path = BOOTSTRAP_PATH) -> InstallIdentity:
    values = parse_bootstrap_values(path)
    telemetry_raw = values.get("TELEMETRY", "").strip().lower()
    telemetry_enabled = telemetry_raw != "false"
    return InstallIdentity(
        install_id=values.get("INSTALL_ID") or None,
        telemetry_enabled=telemetry_enabled,
    )


def render_bootstrap_text(install_id: str, *, telemetry_enabled: bool = True) -> str:
    lines = [f"INSTALL_ID={install_id}"]
    if not telemetry_enabled:
        lines.append("TELEMETRY=false")
    lines.append("")
    return "\n".join(lines)


def ensure_install_id(path: Path = BOOTSTRAP_PATH) -> str:
    identity = load_install_identity(path)
    if identity.install_id:
        return identity.install_id
    install_id = str(uuid.uuid4())
    path.write_text(render_bootstrap_text(install_id, telemetry_enabled=identity.telemetry_enabled))
    return install_id
