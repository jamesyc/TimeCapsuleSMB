#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

DEFAULTS = {
    "TC_HOST": "root@192.168.1.101",
    "TC_NET_IFACE": "bridge0",
    "TC_SHARE_NAME": "Data",
    "TC_SAMBA_USER": "admin",
    "TC_NETBIOS_NAME": "TimeCapsule",
    "TC_PAYLOAD_DIR_NAME": "samba4",
    "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
    "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
}

REQUIRED_ENV_KEYS = [
    "TC_HOST",
    "TC_PASSWORD",
    "TC_NET_IFACE",
    "TC_SHARE_NAME",
    "TC_SAMBA_USER",
    "TC_NETBIOS_NAME",
    "TC_PAYLOAD_DIR_NAME",
    "TC_MDNS_INSTANCE_NAME",
    "TC_MDNS_HOST_LABEL",
]


@dataclass
class CheckResult:
    status: str
    message: str


def parse_env(path: Path) -> dict[str, str]:
    values = dict(DEFAULTS)
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            parsed = shlex.split(value)[0] if value else ""
        except ValueError:
            parsed = value.strip("'\"")
        values[key] = parsed
    return values


def command_exists(name: str) -> bool:
    return subprocess.run(
        ["/bin/sh", "-c", f"command -v {shlex.quote(name)} >/dev/null 2>&1"]
    ).returncode == 0


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                try:
                    sock.connect(sockaddr)
                    return True
                except OSError:
                    continue
    except Exception:
        return False
    return False


def extract_host(target: str) -> str:
    return target.split("@", 1)[1] if "@" in target else target


def run_local_capture(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def capture_dns_sd_browse(service_type: str, duration_seconds: int = 5) -> str:
    script = (
        f'dns-sd -B {shlex.quote(service_type)} local. & '
        f'pid=$!; sleep {duration_seconds}; kill "$pid" >/dev/null 2>&1 || true; '
        f'wait "$pid" >/dev/null 2>&1 || true'
    )
    proc = run_local_capture(["/bin/sh", "-c", script], timeout=duration_seconds + 5)
    return proc.stdout


def capture_dns_sd_lookup(instance_name: str, service_type: str, duration_seconds: int = 5) -> str:
    script = (
        f'dns-sd -L {shlex.quote(instance_name)} {shlex.quote(service_type)} local. & '
        f'pid=$!; sleep {duration_seconds}; kill "$pid" >/dev/null 2>&1 || true; '
        f'wait "$pid" >/dev/null 2>&1 || true'
    )
    proc = run_local_capture(["/bin/sh", "-c", script], timeout=duration_seconds + 5)
    return proc.stdout


def parse_browse_instance(output: str) -> str | None:
    for line in output.splitlines():
        if " Add " in line and "_smb._tcp." in line:
            marker = "_smb._tcp."
            idx = line.find(marker)
            if idx != -1:
                return line[idx + len(marker):].strip()
    return None


def parse_lookup_target(output: str) -> str | None:
    for line in output.splitlines():
        if " can be reached at " in line:
            return line.split(" can be reached at ", 1)[1].strip()
    return None


def print_result(result: CheckResult) -> None:
    print(f"{result.status} {result.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local diagnostics for the current TimeCapsuleSMB setup.")
    parser.add_argument("--skip-ssh", action="store_true", help="Skip SSH reachability checks")
    parser.add_argument("--skip-bonjour", action="store_true", help="Skip Bonjour browse/resolve checks")
    parser.add_argument("--skip-smb", action="store_true", help="Skip authenticated SMB listing check")
    args = parser.parse_args(argv)

    results: list[CheckResult] = []
    fatal = False

    values = parse_env(ENV_PATH)

    if not ENV_PATH.exists():
        results.append(CheckResult("FAIL", f"missing {ENV_PATH}"))
        fatal = True
    else:
        missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key, "")]
        if missing:
            results.append(CheckResult("FAIL", f".env is missing required keys: {', '.join(missing)}"))
            fatal = True
        else:
            results.append(CheckResult("PASS", ".env contains all required keys"))

    for tool in ("dns-sd", "smbutil", "ssh"):
        if command_exists(tool):
            results.append(CheckResult("PASS", f"found local tool {tool}"))
        else:
            status = "WARN" if tool == "ssh" else "FAIL"
            results.append(CheckResult(status, f"missing local tool {tool}"))
            if status == "FAIL":
                fatal = True

    smbd_path = REPO_ROOT / "bin" / "samba4" / "smbd"
    mdns_path = REPO_ROOT / "bin" / "mdns" / "mdns-smbd-advertiser"
    for path in (smbd_path, mdns_path):
        if path.exists():
            results.append(CheckResult("PASS", f"found {path.relative_to(REPO_ROOT)}"))
        else:
            results.append(CheckResult("FAIL", f"missing {path.relative_to(REPO_ROOT)}"))
            fatal = True

    host = extract_host(values["TC_HOST"])

    if not args.skip_ssh:
        if tcp_open(host, 22):
            results.append(CheckResult("PASS", f"SSH reachable at {host}:22"))
        else:
            results.append(CheckResult("FAIL", f"SSH not reachable at {host}:22"))
            fatal = True

    if tcp_open(host, 445):
        results.append(CheckResult("PASS", f"SMB reachable at {host}:445"))
    else:
        results.append(CheckResult("WARN", f"SMB not reachable at {host}:445"))

    discovered_instance = None
    if not args.skip_bonjour and command_exists("dns-sd"):
        try:
            browse_output = capture_dns_sd_browse("_smb._tcp")
            discovered_instance = parse_browse_instance(browse_output)
            if discovered_instance:
                if discovered_instance == values["TC_MDNS_INSTANCE_NAME"]:
                    results.append(CheckResult("PASS", f"discovered _smb._tcp instance {discovered_instance!r}"))
                else:
                    results.append(CheckResult("WARN", f"discovered _smb._tcp instance {discovered_instance!r}, expected {values['TC_MDNS_INSTANCE_NAME']!r}"))
            else:
                results.append(CheckResult("FAIL", "could not discover any _smb._tcp instance"))
                fatal = True

            lookup_name = discovered_instance or values["TC_MDNS_INSTANCE_NAME"]
            lookup_output = capture_dns_sd_lookup(lookup_name, "_smb._tcp")
            target = parse_lookup_target(lookup_output)
            if target:
                results.append(CheckResult("PASS", f"resolved {_quote(lookup_name)} to {target}"))
            else:
                results.append(CheckResult("FAIL", f"could not resolve {_quote(lookup_name)}"))
                fatal = True
        except Exception as e:
            results.append(CheckResult("FAIL", f"Bonjour check failed: {e}"))
            fatal = True

    if not args.skip_smb and command_exists("smbutil"):
        server = f"{values['TC_MDNS_HOST_LABEL']}.local"
        proc = run_local_capture(
            ["smbutil", "view", f"//{values['TC_SAMBA_USER']}:{values['TC_PASSWORD']}@{server}"],
            timeout=20,
        )
        if proc.returncode == 0:
            results.append(CheckResult("PASS", f"authenticated SMB listing works for {values['TC_SAMBA_USER']}@{server}"))
        else:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            msg = detail[-1] if detail else f"failed with rc={proc.returncode}"
            results.append(CheckResult("FAIL", f"authenticated SMB listing failed: {msg}"))
            fatal = True

    for result in results:
        print_result(result)

    if fatal:
        print("\nSummary: doctor found one or more fatal problems.")
        return 1

    print("\nSummary: doctor checks passed.")
    return 0


def _quote(value: str) -> str:
    return repr(value)


if __name__ == "__main__":
    raise SystemExit(main())
