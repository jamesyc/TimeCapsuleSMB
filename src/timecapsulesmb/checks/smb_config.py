from __future__ import annotations

from typing import Optional


def parse_xattr_tdb_paths(smb_conf: str) -> list[str]:
    paths: list[str] = []
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "xattr_tdb:file":
            paths.append(value.strip())
    return paths


def parse_active_netbios_name(smb_conf: str) -> Optional[str]:
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "netbios name":
            return value.strip()
    return None


def parse_active_share_names(smb_conf: str) -> list[str]:
    shares: list[str] = []
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2:
            section_name = stripped[1:-1].strip()
            if section_name and section_name.lower() != "global":
                shares.append(section_name)
    return shares
