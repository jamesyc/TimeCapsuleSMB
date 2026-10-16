"""The Linux distribution's identity, from os-release."""

from __future__ import annotations

from pathlib import Path


OS_RELEASE_PATH = Path("/etc/os-release")


def parse_os_release(path: Path | None = None) -> dict[str, str]:
    try:
        text = (path or OS_RELEASE_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values
