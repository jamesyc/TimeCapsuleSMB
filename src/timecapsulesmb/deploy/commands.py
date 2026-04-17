from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteAction:
    kind: str
    args: tuple[str, ...]


def prepare_dirs_action(payload_dir: str) -> RemoteAction:
    return RemoteAction("prepare_dirs", (payload_dir,))


def initialize_data_root_action(data_root: str, marker_path: str) -> RemoteAction:
    return RemoteAction("initialize_data_root", (data_root, marker_path))


def install_permissions_action(payload_dir: str) -> RemoteAction:
    return RemoteAction("install_permissions", (payload_dir,))


def enable_nbns_action(private_dir: str) -> RemoteAction:
    return RemoteAction("enable_nbns", (private_dir,))


def stop_process_action(name: str) -> RemoteAction:
    return RemoteAction("stop_process", (name,))


def stop_process_full_action(pattern: str) -> RemoteAction:
    return RemoteAction("stop_process_full", (pattern,))


def remove_path_action(path: str) -> RemoteAction:
    return RemoteAction("remove_path", (path,))


def run_script_action(path: str) -> RemoteAction:
    return RemoteAction("run_script", (path,))


def render_remote_action(action: RemoteAction) -> str:
    if action.kind == "stop_process":
        name = action.args[0]
        return (
            f"pkill {shlex.quote(name)} >/dev/null 2>&1 || true; "
            "attempt=0; "
            f"while pkill -0 {shlex.quote(name)} >/dev/null 2>&1; do "
            'if [ "$attempt" -ge 10 ]; then break; fi; '
            "attempt=$((attempt + 1)); "
            "sleep 1; "
            "done"
        )

    if action.kind == "stop_process_full":
        pattern = action.args[0]
        return (
            f"pkill -f {shlex.quote(pattern)} >/dev/null 2>&1 || true; "
            "attempt=0; "
            f"while pkill -0 -f {shlex.quote(pattern)} >/dev/null 2>&1; do "
            'if [ "$attempt" -ge 10 ]; then break; fi; '
            "attempt=$((attempt + 1)); "
            "sleep 1; "
            "done"
        )

    if action.kind == "prepare_dirs":
        payload_dir = action.args[0]
        return (
            "mkdir -p {} {} {} {} {} {} && "
            "rm -rf {} {} && "
            "ln -s {} {} && "
            "ln -s {} {}"
        ).format(
            shlex.quote(payload_dir),
            shlex.quote(payload_dir + "/private"),
            shlex.quote(payload_dir + "/cache"),
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

    if action.kind == "initialize_data_root":
        data_root, marker_path = action.args
        return (
            f"mkdir -p {shlex.quote(data_root)} && "
            f"/bin/sh -c {shlex.quote(f': > {shlex.quote(marker_path)}')}"
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
            f"{shlex.quote('/mnt/Flash/dfree.sh')} "
            f"{shlex.quote('/mnt/Flash/mdns-advertiser')} && "
            f"chmod 755 {shlex.quote(payload_dir + '/cache')} && "
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

    if action.kind == "remove_path":
        return f"rm -rf {shlex.quote(action.args[0])}"

    if action.kind == "run_script":
        return f"/bin/sh {shlex.quote(action.args[0])}"

    raise ValueError(f"Unknown remote action kind: {action.kind}")


def render_remote_actions(actions: list[RemoteAction]) -> list[str]:
    return [render_remote_action(action) for action in actions]
