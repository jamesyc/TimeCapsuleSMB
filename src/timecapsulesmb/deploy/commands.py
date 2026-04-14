from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteAction:
    kind: str
    args: tuple[str, ...]


def prepare_dirs_action(payload_dir: str) -> RemoteAction:
    return RemoteAction("prepare_dirs", (payload_dir,))


def install_permissions_action(payload_dir: str) -> RemoteAction:
    return RemoteAction("install_permissions", (payload_dir,))


def enable_nbns_action(private_dir: str) -> RemoteAction:
    return RemoteAction("enable_nbns", (private_dir,))


def stop_process_action(name: str) -> RemoteAction:
    return RemoteAction("stop_process", (name,))


def remove_path_action(path: str) -> RemoteAction:
    return RemoteAction("remove_path", (path,))


def render_remote_action(action: RemoteAction) -> str:
    if action.kind == "prepare_dirs":
        payload_dir = action.args[0]
        return (
            "mkdir -p {} {} {} {} {} && "
            "rm -rf {} {} && "
            "ln -s {} {} && "
            "ln -s {} {}"
        ).format(
            shlex.quote(payload_dir),
            shlex.quote(payload_dir + "/private"),
            shlex.quote("/mnt/Flash"),
            shlex.quote("/root"),
            shlex.quote("/mnt/Memory/samba4"),
            shlex.quote("/root/tc-netbsd4"),
            shlex.quote("/root/tc-netbsd7"),
            shlex.quote("/mnt/Memory/samba4"),
            shlex.quote("/root/tc-netbsd4"),
            shlex.quote("/mnt/Memory/samba4"),
            shlex.quote("/root/tc-netbsd7"),
        )

    if action.kind == "install_permissions":
        payload_dir = action.args[0]
        private_dir = f"{payload_dir}/private"
        return (
            f"chmod 755 {shlex.quote(payload_dir + '/smbd')} "
            f"{shlex.quote(payload_dir + '/mdns-advertiser')} "
            f"{shlex.quote(payload_dir + '/nbns-advertiser')} && "
            f"chmod 755 {shlex.quote('/mnt/Flash/rc.local')} "
            f"{shlex.quote('/mnt/Flash/start-samba.sh')} "
            f"{shlex.quote('/mnt/Flash/watchdog.sh')} "
            f"{shlex.quote('/mnt/Flash/dfree.sh')} && "
            f"chmod 700 {shlex.quote(private_dir)} && "
            f"chmod 600 {shlex.quote(private_dir + '/smbpasswd')} "
            f"{shlex.quote(private_dir + '/username.map')} "
            f"{shlex.quote(private_dir + '/adisk.uuid')} && "
            f"if [ -f {shlex.quote(private_dir + '/nbns.enabled')} ]; then "
            f"chmod 600 {shlex.quote(private_dir + '/nbns.enabled')}; "
            f"fi"
        )

    if action.kind == "enable_nbns":
        private_dir = action.args[0]
        marker_path = private_dir + "/nbns.enabled"
        return f"/bin/sh -c {shlex.quote(f': > {shlex.quote(marker_path)}')}"

    if action.kind == "stop_process":
        return f"pkill {shlex.quote(action.args[0])} >/dev/null 2>&1 || true"

    if action.kind == "remove_path":
        return f"rm -rf {shlex.quote(action.args[0])}"

    raise ValueError(f"Unknown remote action kind: {action.kind}")


def render_remote_actions(actions: list[RemoteAction]) -> list[str]:
    return [render_remote_action(action) for action in actions]
