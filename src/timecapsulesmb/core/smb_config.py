from __future__ import annotations

import functools
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SmbShare:
    name: str
    path: Optional[str] = None


# Samba's parser (lib/util/params.c) compresses each run of whitespace in a
# section name to one space, so "[Nicholas  McBride's]" serves a share named
# "Nicholas McBride's". Mirror it so share names match what smbd lists.
_SECTION_WHITESPACE = re.compile(r"[ \t\v\f\r]+")


def _section_name(header: str) -> str:
    return _SECTION_WHITESPACE.sub(" ", header).strip(" ")


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
            section_name = _section_name(stripped[1:-1])
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


@functools.lru_cache(maxsize=None)
def apple_composites() -> dict[tuple[str, str], str]:
    """The pairs Apple's SMB server and the Mac kernel compose (xnu's
    vfs_utfconv): the Unicode 3.2 primary composites in the Basic Multilingual
    Plane, Hangul syllables aside. build/native/storage/gen_compose_table.py
    writes the device's copy from this table."""
    # Unicode 3.2's own decompositions: ucd_3_2_0.normalize() composes with
    # the current tables and would add pairs Apple's table lacks (Balinese).
    pairs = {}
    for code in range(0x10000):
        if 0xAC00 <= code <= 0xD7A3:
            continue
        decomposition = unicodedata.ucd_3_2_0.decomposition(chr(code))
        parts = decomposition.split()
        if len(parts) == 2 and not decomposition.startswith("<"):
            first, second = (chr(int(part, 16)) for part in parts)
            # Composition exclusions decompose but never compose again.
            if unicodedata.normalize("NFC", first + second) == chr(code):
                pairs[first, second] = chr(code)
    return pairs


def _apple_compose_pair(base: str, mark: str) -> str | None:
    b, m = ord(base), ord(mark)
    if 0x1100 <= b < 0x1113 and 0x1161 <= m < 0x1176:
        return chr(0xAC00 + ((b - 0x1100) * 21 + m - 0x1161) * 28)
    if 0xAC00 <= b < 0xD7A4 and (b - 0xAC00) % 28 == 0 and 0x11A8 <= m < 0x11C3:
        return chr(b + m - 0x11A7)
    return apple_composites().get((base, mark))


def apple_precompose(name: str) -> str:
    """NAME as a Mac sends it in a tree connect, and as the device names the
    share's smb.conf section: each character combined into the one before it
    when Unicode 3.2 has a composite for the pair, the table Apple's SMB server
    and the Mac kernel use (utf8_decodestr with UTF_PRECOMPOSED)."""
    out: list[str] = []
    for char in name:
        composite = _apple_compose_pair(out[-1], char) if out else None
        if composite:
            out[-1] = composite
        else:
            out.append(char)
    return "".join(out)


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
