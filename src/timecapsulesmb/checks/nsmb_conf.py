"""This Mac's SMB client settings (nsmb.conf) that keep Time Machine off SMB 3.

Time Machine needs SMB 3 (durable handles, the reconnect timeout). With
`protocol_vers_map=2` in /etc/nsmb.conf the Mac negotiates SMB 2.1 and
Time Machine rejects the disk: "The selected network backup disk does not
support the required capabilities" (issues #181 and #372). doctor's own SMB
checks use smbclient, which ignores nsmb.conf, so they pass regardless.

man nsmb.conf: /etc/nsmb.conf is global and wins over the user's
~/Library/Preferences/nsmb.conf. protocol_vers_map is a bitmap (1 SMB 1,
2 SMB 2, 4 SMB 3; default 7) set in [default] or a [SERVER] section; a
[SERVER:SHARE] section cannot hold it. macOS 10.12 replaced the older
smb_neg with it, and ignores smb_neg since; the app needs macOS 14.

Parsing follows Apple's lib/smb/rcfile.c: section and key names are
case-sensitive ([global] is read as [default]; spaces inside the brackets are
part of the name; spaces in a key are dropped), a comment is a line whose
first non-blank character is # or ;, and numbers are read with
strtol(value, NULL, 0), so "06" is octal and trailing text is ignored.
Sections of the same name merge, a later value for a key replaces an earlier
one, and /etc/nsmb.conf is read after the user's file, so it wins.

backupd runs as root, so backups read the global file; the user's file caps
the user's own mounts (measured: SMB 2.1 with a capped user file, 3.1.1
without), so a cap there is a warning.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.checks.models import CheckResult


GLOBAL_NSMB_CONF = Path("/etc/nsmb.conf")
SMB3_BIT = 4

NSMB_SMB3_DISABLED_CODE = "mac_smb3_disabled"
NSMB_SMB3_DISABLED_FOR_SERVER_CODE = "mac_smb3_disabled_for_server"


def user_nsmb_conf() -> Path:
    return Path.home() / "Library" / "Preferences" / "nsmb.conf"


@dataclass(frozen=True)
class NsmbSection:
    name: str
    values: dict[str, str]


def parse_nsmb_conf(text: str) -> list[NsmbSection]:
    """Sections in file order; lines before any section and comment lines are
    ignored, as the SMB library ignores them. Names keep their case."""
    sections: list[NsmbSection] = []
    for raw in text.replace("\r", "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            sections.append(NsmbSection("default" if name == "global" else name, {}))
        elif "=" in line and sections:
            key, _, value = line.partition("=")
            sections[-1].values[key.replace(" ", "").replace("\t", "")] = value.strip()
    return sections


def strtol_base0(text: str) -> int:
    """C strtol(text, NULL, 0): optional sign, 0x for hex, a leading 0 for
    octal; digits up to the first that does not fit; 0 when there are none."""
    match = re.match(r"\s*([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", text)
    if not match:
        return 0
    sign, digits = match.groups()
    value = int(digits, 16 if digits[:2].lower() == "0x" else 8 if digits[0] == "0" else 10)
    return -value if sign == "-" else value


def check_nsmb_conf(
    *,
    platform: str = sys.platform,
    global_path: Path = GLOBAL_NSMB_CONF,
    user_path: Path | None = None,
) -> tuple[list[CheckResult], dict[str, object] | None]:
    """Results for Checkup's This Mac section, and a summary for telemetry
    that carries only the SMB version settings, never server names."""
    if platform != "darwin":
        return [], None
    files = (("global", global_path), ("user", user_path or user_nsmb_conf()))
    results: list[CheckResult] = []
    summary: dict[str, object] = {}
    parsed: dict[str, list[NsmbSection]] = {}
    for scope, path in files:
        try:
            text = path.read_text(errors="replace")
        except FileNotFoundError:
            summary[scope] = {"present": False}
            continue
        except OSError as exc:
            summary[scope] = {"present": True, "readable": False}
            results.append(CheckResult("WARN", f"could not read {path}: {exc.strerror or exc}"))
            continue
        sections = parse_nsmb_conf(text)
        parsed[scope] = sections
        summary[scope] = {"present": True, "sections": _summary_sections(sections)}

    # What macOS reads: the user's file, then the global one over it.
    merged: dict[str, dict[str, tuple[str, str, Path]]] = {}
    for scope, path in reversed(files):
        for section in parsed.get(scope, ()):
            for key, value in section.values.items():
                merged.setdefault(section.name, {})[key] = (value, scope, path)

    for name, values in merged.items():
        if "protocol_vers_map" not in values or ":" in name:
            continue
        value, scope, path = values["protocol_vers_map"]
        versions = strtol_base0(value)
        # Nobody writes a value that reads as 0 or less; it is not a cap.
        if versions <= 0 or versions & SMB3_BIT:
            continue
        if name != "default":
            results.append(CheckResult(
                "WARN",
                f"{path} sets protocol_vers_map={value} for the server [{name}], which turns off SMB 3 for it; "
                "if that is this Time Capsule, Time Machine rejects it as a backup disk. Delete that line "
                f"({_edit_command(path)}), then remove and add the disk again in Time Machine settings",
                {"code": NSMB_SMB3_DISABLED_FOR_SERVER_CODE, "path": str(path), "section": name},
            ))
        elif scope == "global":
            results.append(CheckResult("FAIL", (
                f"{path} sets protocol_vers_map={value}, which turns off SMB 3 on this Mac, and Time Machine needs SMB 3: "
                "it rejects the disk because it \"does not support the necessary functions\". Delete that line "
                f"({_edit_command(path)}), then remove and add the disk again in Time Machine settings; "
                "restart the Mac if it is still rejected"
            ), {"code": NSMB_SMB3_DISABLED_CODE, "path": str(path)}))
        else:
            results.append(CheckResult("WARN", (
                f"{path} sets protocol_vers_map={value}, which turns off SMB 3 for this user's Finder connections; "
                "Time Machine needs SMB 3. If adding the disk in Time Machine settings fails, delete that line, "
                "then remove and add the disk again"
            ), {"code": NSMB_SMB3_DISABLED_CODE, "path": str(path)}))

    if not any(result.status in ("FAIL", "WARN") for result in results):
        results.append(CheckResult("PASS", "this Mac's SMB client allows SMB 3 (nsmb.conf does not turn it off)"))
    return results, summary


def _edit_command(path: Path) -> str:
    return f"sudo nano {path}" if path == GLOBAL_NSMB_CONF else f"nano {path}"


def _summary_sections(sections: list[NsmbSection]) -> list[dict[str, str]]:
    # Server names stay out: sections are often named after work servers.
    summary: list[dict[str, str]] = []
    for section in sections:
        if "protocol_vers_map" in section.values:
            kind = "default" if section.name == "default" else "share" if ":" in section.name else "server"
            summary.append({"kind": kind, "protocol_vers_map": section.values["protocol_vers_map"]})
    return summary
