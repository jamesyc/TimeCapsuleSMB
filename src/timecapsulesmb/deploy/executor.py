from __future__ import annotations

import shlex
import tempfile
import uuid
from pathlib import Path

from timecapsulesmb.deploy.auth import render_smbpasswd
from timecapsulesmb.deploy.commands import (
    enable_nbns_action,
    initialize_data_root_action,
    install_permissions_action,
    prepare_dirs_action,
    render_remote_action,
    render_remote_actions,
)
from timecapsulesmb.deploy.planner import DeploymentPlan, UninstallPlan
from timecapsulesmb.transport.ssh import SshConnection, run_scp_conn, run_ssh_conn


def remote_prepare_dirs(connection: SshConnection, payload_dir: str) -> None:
    run_ssh_conn(connection, render_remote_action(prepare_dirs_action(payload_dir)))


def remote_initialize_data_root(connection: SshConnection, data_root: str, marker_path: str) -> None:
    run_ssh_conn(connection, render_remote_action(initialize_data_root_action(data_root, marker_path)))


def remote_install_permissions(connection: SshConnection, payload_dir: str) -> None:
    run_ssh_conn(connection, render_remote_action(install_permissions_action(payload_dir)))


def remote_install_auth_files(connection: SshConnection, private_dir: str, samba_user: str, samba_password: str) -> None:
    smbpasswd_text, username_map_text = render_smbpasswd(samba_user, samba_password)
    with tempfile.TemporaryDirectory(prefix="tc-deploy-auth-") as tmp:
        tmpdir = Path(tmp)
        smbpasswd_path = tmpdir / "smbpasswd"
        username_map_path = tmpdir / "username.map"
        smbpasswd_path.write_text(smbpasswd_text)
        username_map_path.write_text(username_map_text)
        run_scp_conn(connection, smbpasswd_path, f"{private_dir}/smbpasswd")
        run_scp_conn(connection, username_map_path, f"{private_dir}/username.map")


def remote_ensure_adisk_uuid(connection: SshConnection, private_dir: str) -> str:
    remote_path = f"{private_dir}/adisk.uuid"
    proc = run_ssh_conn(
        connection,
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
        run_scp_conn(connection, uuid_path, remote_path)
    return adisk_uuid


def upload_deployment_payload(
    plan: DeploymentPlan,
    *,
    connection: SshConnection,
    rc_local: Path,
    common_sh: Path,
    rendered_start: Path,
    rendered_dfree: Path,
    rendered_watchdog: Path,
    rendered_smbconf: Path,
) -> None:
    run_scp_conn(connection, plan.smbd_path, plan.payload_targets["smbd"])
    run_scp_conn(connection, plan.mdns_path, plan.payload_targets["mdns-advertiser"])
    run_scp_conn(connection, plan.mdns_path, plan.flash_targets["mdns-advertiser"])
    run_scp_conn(connection, plan.nbns_path, plan.payload_targets["nbns-advertiser"])
    run_scp_conn(connection, rc_local, plan.flash_targets["rc.local"])
    run_scp_conn(connection, common_sh, plan.flash_targets["common.sh"])
    run_scp_conn(connection, rendered_start, plan.flash_targets["start-samba.sh"])
    run_scp_conn(connection, rendered_watchdog, plan.flash_targets["watchdog.sh"])
    run_scp_conn(connection, rendered_dfree, plan.flash_targets["dfree.sh"])
    run_scp_conn(connection, rendered_smbconf, plan.payload_targets["smb.conf.template"])


def remote_enable_nbns(connection: SshConnection, private_dir: str) -> None:
    run_ssh_conn(connection, render_remote_action(enable_nbns_action(private_dir)))


def run_remote_actions(connection: SshConnection, actions) -> None:
    for command in render_remote_actions(list(actions)):
        run_ssh_conn(connection, command)


def remote_uninstall_payload(connection: SshConnection, plan: UninstallPlan) -> None:
    # Use for loop to avoid rc=255 bug on NetBSD 4 Time Capsules
    for command in render_remote_actions(plan.remote_actions):
        run_ssh_conn(connection, command)
