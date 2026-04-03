#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
BOOT_DIR = REPO_ROOT / "boot" / "samba4"
BIN_DIR = REPO_ROOT / "bin" / "samba4"
MDNS_BIN_DIR = REPO_ROOT / "bin" / "mdns"

DEFAULTS = {
    "TC_HOST": "root@192.168.1.101",
    "TC_SSH_OPTS": "-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "TC_NET_IFACE": "bridge0",
    "TC_SHARE_NAME": "Data",
    "TC_SAMBA_USER": "admin",
    "TC_NETBIOS_NAME": "TimeCapsule",
    "TC_PAYLOAD_DIR_NAME": "samba4",
    "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
    "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
}


@dataclass
class _MD4:
    A: int = 0x67452301
    B: int = 0xEFCDAB89
    C: int = 0x98BADCFE
    D: int = 0x10325476
    count: int = 0
    buffer: bytes = b""

    @staticmethod
    def _rol(value: int, bits: int) -> int:
        value &= 0xFFFFFFFF
        return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF

    @staticmethod
    def _f(x: int, y: int, z: int) -> int:
        return (x & y) | (~x & z)

    @staticmethod
    def _g(x: int, y: int, z: int) -> int:
        return (x & y) | (x & z) | (y & z)

    @staticmethod
    def _h(x: int, y: int, z: int) -> int:
        return x ^ y ^ z

    def update(self, data: bytes) -> None:
        self.count += len(data)
        self.buffer += data
        while len(self.buffer) >= 64:
            self._process(self.buffer[:64])
            self.buffer = self.buffer[64:]

    def _process(self, block: bytes) -> None:
        x = [int.from_bytes(block[i:i + 4], "little") for i in range(0, 64, 4)]
        a, b, c, d = self.A, self.B, self.C, self.D

        for k, s in ((0, 3), (1, 7), (2, 11), (3, 19), (4, 3), (5, 7), (6, 11), (7, 19),
                     (8, 3), (9, 7), (10, 11), (11, 19), (12, 3), (13, 7), (14, 11), (15, 19)):
            if k % 4 == 0:
                a = self._rol((a + self._f(b, c, d) + x[k]) & 0xFFFFFFFF, s)
            elif k % 4 == 1:
                d = self._rol((d + self._f(a, b, c) + x[k]) & 0xFFFFFFFF, s)
            elif k % 4 == 2:
                c = self._rol((c + self._f(d, a, b) + x[k]) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._f(c, d, a) + x[k]) & 0xFFFFFFFF, s)

        for k, s in ((0, 3), (4, 5), (8, 9), (12, 13), (1, 3), (5, 5), (9, 9), (13, 13),
                     (2, 3), (6, 5), (10, 9), (14, 13), (3, 3), (7, 5), (11, 9), (15, 13)):
            if k in (0, 1, 2, 3):
                a = self._rol((a + self._g(b, c, d) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif k in (4, 5, 6, 7):
                d = self._rol((d + self._g(a, b, c) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif k in (8, 9, 10, 11):
                c = self._rol((c + self._g(d, a, b) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._g(c, d, a) + x[k] + 0x5A827999) & 0xFFFFFFFF, s)

        order = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]
        shifts = [3, 9, 11, 15] * 4
        for idx, k in enumerate(order):
            s = shifts[idx]
            if idx % 4 == 0:
                a = self._rol((a + self._h(b, c, d) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif idx % 4 == 1:
                d = self._rol((d + self._h(a, b, c) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif idx % 4 == 2:
                c = self._rol((c + self._h(d, a, b) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            else:
                b = self._rol((b + self._h(c, d, a) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)

        self.A = (self.A + a) & 0xFFFFFFFF
        self.B = (self.B + b) & 0xFFFFFFFF
        self.C = (self.C + c) & 0xFFFFFFFF
        self.D = (self.D + d) & 0xFFFFFFFF

    def digest(self) -> bytes:
        clone = _MD4(self.A, self.B, self.C, self.D, self.count, self.buffer)
        bit_len = clone.count * 8
        clone.update(b"\x80")
        while len(clone.buffer) % 64 != 56:
            clone.buffer += b"\x00"
        clone.buffer += bit_len.to_bytes(8, "little")
        while clone.buffer:
            clone._process(clone.buffer[:64])
            clone.buffer = clone.buffer[64:]
        return (
            clone.A.to_bytes(4, "little")
            + clone.B.to_bytes(4, "little")
            + clone.C.to_bytes(4, "little")
            + clone.D.to_bytes(4, "little")
        )


def nt_hash_hex(password: str) -> str:
    md4 = _MD4()
    md4.update(password.encode("utf-16le"))
    return md4.digest().hex().upper()


def render_smbpasswd(username: str, password: str) -> tuple[str, str]:
    nt_hash = nt_hash_hex(password)
    lct = f"{int(time.time()):08X}"
    smbpasswd_line = f"root:0:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:{nt_hash}:[U          ]:LCT-{lct}:\n"
    username_map = f"root = {username}\n"
    return smbpasswd_line, username_map


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


def require(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise SystemExit(f"Missing required setting in .env: {key}")
    return value


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


def run_ssh(host: str, password: str, ssh_opts: str, remote_cmd: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        import pexpect
    except Exception as e:
        raise SystemExit(f"pexpect is required for deploy.py: {e}")

    cmd = ["ssh", *shlex.split(ssh_opts), host, remote_cmd]
    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", timeout=120)
    output = []
    try:
        while True:
            idx = child.expect(["[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=120)
            if idx == 0:
                child.sendline(password)
            elif idx == 1:
                output.append(child.before or "")
                break
            else:
                output.append(child.before or "")
                raise SystemExit("Timed out waiting for ssh command to finish.")
    finally:
        try:
            child.close()
        except Exception:
            pass

    rc = child.exitstatus if child.exitstatus is not None else (child.signalstatus or 1)
    stdout = "".join(output)
    if check and rc != 0:
        raise SystemExit(stdout.strip() or f"ssh command failed with rc={rc}")
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")


def run_scp(host: str, password: str, ssh_opts: str, src: Path, dest: str) -> None:
    try:
        import pexpect
    except Exception as e:
        raise SystemExit(f"pexpect is required for deploy.py: {e}")

    cmd = ["scp", "-O", *shlex.split(ssh_opts), str(src), f"{host}:{dest}"]
    child = pexpect.spawn(cmd[0], cmd[1:], encoding="utf-8", timeout=120)
    try:
        while True:
            idx = child.expect(["[Pp]assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=120)
            if idx == 0:
                child.sendline(password)
            elif idx == 1:
                break
            else:
                raise SystemExit(f"Timed out copying {src} to {dest}")
    finally:
        try:
            child.close()
        except Exception:
            pass

    rc = child.exitstatus if child.exitstatus is not None else (child.signalstatus or 1)
    if rc != 0:
        raise SystemExit(child.before or f"scp failed with rc={rc}")


def render_template(path: Path, replacements: dict[str, str]) -> str:
    content = path.read_text()
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def remote_discover_volume(host: str, password: str, ssh_opts: str) -> str:
    script = r'''
for dev in dk2 dk3; do
  if [ -b "/dev/$dev" ]; then
    volume="/Volumes/$dev"
    [ -d "$volume" ] || mkdir -p "$volume"
    /sbin/mount_hfs "/dev/$dev" "$volume" >/dev/null 2>&1 || true
    if [ -d "$volume" ]; then
      echo "$volume"
      exit 0
    fi
  fi
done
exit 1
'''
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}")
    volume = proc.stdout.strip().splitlines()[-1].strip()
    if not volume:
        raise SystemExit("Failed to discover a Time Capsule volume root on the device.")
    return volume


def remote_prepare_dirs(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    cmd = f"mkdir -p {shlex.quote(payload_dir)} {shlex.quote(payload_dir + '/private')} /mnt/Flash"
    run_ssh(host, password, ssh_opts, cmd)


def remote_install_permissions(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    private_dir = f"{payload_dir}/private"
    cmd = (
        "chmod 755 /mnt/Flash/rc.local /mnt/Flash/start-samba.sh /mnt/Flash/dfree.sh && "
        f"chmod 700 {shlex.quote(private_dir)} && "
        f"chmod 600 {shlex.quote(private_dir + '/smbpasswd')} {shlex.quote(private_dir + '/username.map')}"
    )
    run_ssh(host, password, ssh_opts, cmd)


def remote_install_auth_files(host: str, password: str, ssh_opts: str, payload_dir: str, samba_user: str, samba_password: str) -> None:
    smbpasswd_text, username_map_text = render_smbpasswd(samba_user, samba_password)
    private_dir = f"{payload_dir}/private"
    with tempfile.TemporaryDirectory(prefix="tc-deploy-auth-") as tmp:
        tmpdir = Path(tmp)
        smbpasswd_path = tmpdir / "smbpasswd"
        username_map_path = tmpdir / "username.map"
        smbpasswd_path.write_text(smbpasswd_text)
        username_map_path.write_text(username_map_text)
        run_scp(host, password, ssh_opts, smbpasswd_path, f"{private_dir}/smbpasswd")
        run_scp(host, password, ssh_opts, username_map_path, f"{private_dir}/username.map")


def wait_for_ssh_state(hostname: str, *, expected_up: bool, timeout_seconds: int = 180) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if tcp_open(hostname, 22) == expected_up:
            return True
        time.sleep(5)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the checked-in Samba 4 payload to a Time Capsule.")
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot after deployment")
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    args = parser.parse_args(argv)

    values = parse_env(ENV_PATH)
    host = require(values, "TC_HOST")
    password = values.get("TC_PASSWORD", "")
    if not password:
        password = getpass.getpass("Time Capsule root password: ")
    ssh_opts = values["TC_SSH_OPTS"]

    smbd_path = BIN_DIR / "smbd"
    mdns_path = MDNS_BIN_DIR / "mdns-smbd-advertiser"
    if not smbd_path.exists():
        raise SystemExit(f"Missing Samba payload: {smbd_path}")
    if not mdns_path.exists():
        raise SystemExit(f"Missing mDNS payload: {mdns_path}")

    start_script_replacements = {
        "__PAYLOAD_DIR_NAME__": shell_quote(values["TC_PAYLOAD_DIR_NAME"]),
        "__SMB_SHARE_NAME__": shell_quote(values["TC_SHARE_NAME"]),
        "__SMB_NETBIOS_NAME__": shell_quote(values["TC_NETBIOS_NAME"]),
        "__NET_IFACE__": shell_quote(values["TC_NET_IFACE"]),
        "__MDNS_INSTANCE_NAME__": shell_quote(values["TC_MDNS_INSTANCE_NAME"]),
        "__MDNS_HOST_LABEL__": shell_quote(values["TC_MDNS_HOST_LABEL"]),
    }

    smbconf_replacements = {
        "__PAYLOAD_DIR_NAME__": values["TC_PAYLOAD_DIR_NAME"],
        "__SMB_SHARE_NAME__": values["TC_SHARE_NAME"],
        "__SMB_SAMBA_USER__": values["TC_SAMBA_USER"],
        "__SMB_NETBIOS_NAME__": values["TC_NETBIOS_NAME"],
        "__NET_IFACE__": values["TC_NET_IFACE"],
    }

    if args.dry_run:
        print(f"Would deploy {smbd_path} to {host}")
        return 0

    volume_root = remote_discover_volume(host, password, ssh_opts)
    payload_dir = f"{volume_root}/{values['TC_PAYLOAD_DIR_NAME']}"
    remote_prepare_dirs(host, password, ssh_opts, payload_dir)

    with tempfile.TemporaryDirectory(prefix="tc-deploy-") as tmp:
        tmpdir = Path(tmp)
        rendered_start = tmpdir / "start-samba.sh"
        rendered_smbconf = tmpdir / "smb.conf.template"
        rendered_start.write_text(render_template(BOOT_DIR / "start-samba.sh", start_script_replacements))
        rendered_smbconf.write_text(render_template(BOOT_DIR / "smb.conf.template", smbconf_replacements))

        run_scp(host, password, ssh_opts, smbd_path, f"{payload_dir}/smbd")
        run_scp(host, password, ssh_opts, mdns_path, f"{payload_dir}/mdns-smbd-advertiser")
        run_scp(host, password, ssh_opts, BOOT_DIR / "rc.local", "/mnt/Flash/rc.local")
        run_scp(host, password, ssh_opts, rendered_start, "/mnt/Flash/start-samba.sh")
        run_scp(host, password, ssh_opts, BOOT_DIR / "dfree.sh", "/mnt/Flash/dfree.sh")
        run_scp(host, password, ssh_opts, rendered_smbconf, f"{payload_dir}/smb.conf.template")

    remote_install_auth_files(host, password, ssh_opts, payload_dir, values["TC_SAMBA_USER"], password)
    remote_install_permissions(host, password, ssh_opts, payload_dir)

    print(f"Deployed Samba payload to {payload_dir}")
    print("Updated /mnt/Flash boot files.")

    if args.no_reboot:
        print("Skipping reboot.")
        return 0

    if not args.yes:
        answer = input("This will reboot the Time Capsule now. Continue? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print("Deployment complete without reboot.")
            return 0

    run_ssh(host, password, ssh_opts, "/sbin/reboot", check=False)
    hostname = extract_host(host)
    print("Reboot requested. Waiting for the device to go down...")
    wait_for_ssh_state(hostname, expected_up=False, timeout_seconds=60)
    print("Waiting for the device to come back up...")
    if wait_for_ssh_state(hostname, expected_up=True, timeout_seconds=240):
        print("Device is back online.")
        return 0

    print("Timed out waiting for SSH after reboot.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
