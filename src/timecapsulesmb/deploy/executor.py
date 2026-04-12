from __future__ import annotations

import shlex
import tempfile
import uuid
from pathlib import Path

from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.commands import (
    enable_nbns_action,
    install_permissions_action,
    prepare_dirs_action,
    render_remote_action,
    render_remote_actions,
)
from timecapsulesmb.deploy.planner import DeploymentPlan, UninstallPlan
from timecapsulesmb.transport.ssh import run_scp, run_ssh


def remote_prepare_dirs(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    run_ssh(host, password, ssh_opts, render_remote_action(prepare_dirs_action(payload_dir)))


def remote_install_permissions(host: str, password: str, ssh_opts: str, payload_dir: str) -> None:
    run_ssh(host, password, ssh_opts, render_remote_action(install_permissions_action(payload_dir)))


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
    run_scp(host, password, ssh_opts, plan.nbns_path, plan.payload_targets["nbns-advertiser"])
    run_scp(host, password, ssh_opts, rc_local, plan.flash_targets["rc.local"])
    run_scp(host, password, ssh_opts, rendered_start, plan.flash_targets["start-samba.sh"])
    run_scp(host, password, ssh_opts, rendered_watchdog, plan.flash_targets["watchdog.sh"])
    run_scp(host, password, ssh_opts, rendered_dfree, plan.flash_targets["dfree.sh"])
    run_scp(host, password, ssh_opts, rendered_smbconf, plan.payload_targets["smb.conf.template"])


def remote_enable_nbns(host: str, password: str, ssh_opts: str, private_dir: str) -> None:
    run_ssh(host, password, ssh_opts, render_remote_action(enable_nbns_action(private_dir)))


def run_remote_actions(host: str, password: str, ssh_opts: str, actions) -> None:
    for command in render_remote_actions(list(actions)):
        run_ssh(host, password, ssh_opts, command)


def remote_uninstall_payload(host: str, password: str, ssh_opts: str, plan: UninstallPlan) -> None:
    run_ssh(host, password, ssh_opts, " && ".join(render_remote_actions(plan.remote_actions)))
