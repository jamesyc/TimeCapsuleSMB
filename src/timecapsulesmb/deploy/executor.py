from __future__ import annotations

import shlex
import tempfile
import uuid
from pathlib import Path

from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.planner import DeploymentPlan, UninstallPlan
from timecapsulesmb.transport.ssh import run_scp, run_ssh


def remote_prepare_dirs(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    cmd = f"mkdir -p {shlex.quote(payload_dir)} {shlex.quote(payload_dir + '/private')} /mnt/Flash"
    run_ssh(host, password, ssh_opts, cmd)


def remote_install_permissions(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    private_dir = f"{payload_dir}/private"
    cmd = (
        "chmod 755 /mnt/Flash/rc.local /mnt/Flash/start-samba.sh /mnt/Flash/watchdog.sh /mnt/Flash/dfree.sh && "
        f"chmod 700 {shlex.quote(private_dir)} && "
        f"chmod 600 {shlex.quote(private_dir + '/smbpasswd')} {shlex.quote(private_dir + '/username.map')} {shlex.quote(private_dir + '/adisk.uuid')}"
    )
    run_ssh(host, password, ssh_opts, cmd)


def remote_install_auth_files(host: str, password: str, ssh_opts: str, private_dir: str, samba_user: str, samba_password: str) -> None:
    smbpasswd_text, username_map_text = render_smbpasswd(samba_user, samba_password)
    with tempfile.TemporaryDirectory(prefix="tc-deploy-auth-") as tmp:
        tmpdir = Path(tmp)
        smbpasswd_path = tmpdir / "smbpasswd"
        username_map_path = tmpdir / "username.map"
        smbpasswd_path.write_text(smbpasswd_text)
        username_map_path.write_text(username_map_text)
        run_scp(host, password, ssh_opts, smbpasswd_path, f"{private_dir}/smbpasswd")
        run_scp(host, password, ssh_opts, username_map_path, f"{private_dir}/username.map")


def remote_ensure_adisk_uuid(host: str, password: str, ssh_opts: str, private_dir: str) -> str:
    remote_path = f"{private_dir}/adisk.uuid"
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(f'if [ -f {shlex.quote(remote_path)} ]; then cat {shlex.quote(remote_path)}; fi')}",
        check=False,
    )
    existing = proc.stdout.strip()
    if existing:
        return existing

    adisk_uuid = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="tc-deploy-adisk-") as tmp:
        tmpdir = Path(tmp)
        uuid_path = tmpdir / "adisk.uuid"
        uuid_path.write_text(f"{adisk_uuid}\n")
        run_scp(host, password, ssh_opts, uuid_path, remote_path)
    return adisk_uuid


def upload_deployment_payload(
    plan: DeploymentPlan,
    *,
    host: str,
    password: str,
    ssh_opts: str,
    rc_local: Path,
    rendered_start: Path,
    rendered_dfree: Path,
    rendered_watchdog: Path,
    rendered_smbconf: Path,
) -> None:
    run_scp(host, password, ssh_opts, plan.smbd_path, plan.payload_targets["smbd"])
    run_scp(host, password, ssh_opts, plan.mdns_path, plan.payload_targets["mdns-smbd-advertiser"])
    run_scp(host, password, ssh_opts, rc_local, plan.flash_targets["rc.local"])
    run_scp(host, password, ssh_opts, rendered_start, plan.flash_targets["start-samba.sh"])
    run_scp(host, password, ssh_opts, rendered_watchdog, plan.flash_targets["watchdog.sh"])
    run_scp(host, password, ssh_opts, rendered_dfree, plan.flash_targets["dfree.sh"])
    run_scp(host, password, ssh_opts, rendered_smbconf, plan.payload_targets["smb.conf.template"])


def remote_uninstall_payload(host: str, password: str, ssh_opts: str, plan: UninstallPlan) -> None:
    cmd_parts = [*plan.stop_commands]
    cmd_parts.extend(f"rm -rf {shlex.quote(target)}" for target in plan.remove_targets)
    run_ssh(host, password, ssh_opts, " && ".join(cmd_parts))
