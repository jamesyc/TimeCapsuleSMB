from __future__ import annotations

from typing import Optional


def _iter_global_option_lines(smb_conf: str):
    in_global = False
    saw_section = False
    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            saw_section = True
            in_global = stripped[1:-1].strip().lower() == "global"
            continue
        if in_global or not saw_section:
            yield stripped


def parse_global_option(smb_conf: str, option_name: str) -> Optional[str]:
    expected = option_name.strip().lower()
    for stripped in _iter_global_option_lines(smb_conf):
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == expected:
            return value.strip()
    return None


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
