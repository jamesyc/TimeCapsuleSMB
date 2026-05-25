from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SmbShare:
    name: str
    path: Optional[str] = None


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


def parse_active_shares(smb_conf: str) -> list[SmbShare]:
    shares: list[SmbShare] = []
    current_name: str | None = None
    current_path: str | None = None

    def append_current() -> None:
        nonlocal current_name, current_path
        if current_name and current_name.lower() != "global":
            shares.append(SmbShare(name=current_name, path=current_path))

    for line in smb_conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            append_current()
            section_name = stripped[1:-1].strip()
            current_name = section_name or None
            current_path = None
            continue
        if current_name is None or current_name.lower() == "global":
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip().lower() == "path":
            current_path = value.strip()

    append_current()
    return shares


def parse_active_share_names(smb_conf: str) -> list[str]:
    return [share.name for share in parse_active_shares(smb_conf)]


def parse_active_share_paths(smb_conf: str) -> list[str]:
    return [share.path for share in parse_active_shares(smb_conf) if share.path]


def parse_payload_dir_from_log_file(log_file: str) -> Optional[str]:
    normalized = log_file.strip()
    if not normalized:
        return None
    log_dir = posixpath.dirname(normalized.rstrip("/"))
    if posixpath.basename(log_dir) != "logs":
        return None
    payload_dir = posixpath.dirname(log_dir)
    return payload_dir or None


def parse_active_payload_dir(smb_conf: str) -> Optional[str]:
    log_file = parse_global_option(smb_conf, "log file")
    if log_file is None:
        return None
    return parse_payload_dir_from_log_file(log_file)
