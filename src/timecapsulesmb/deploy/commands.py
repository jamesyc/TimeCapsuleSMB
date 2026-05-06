from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class PrepareDirsAction:
    payload_dir: str


@dataclass(frozen=True)
class InitializeDataRootAction:
    data_root: str
    marker_path: str


@dataclass(frozen=True)
class InstallPermissionsAction:
    payload_dir: str


@dataclass(frozen=True)
class EnableNbnsAction:
    private_dir: str


@dataclass(frozen=True)
class StopProcessAction:
    name: str


@dataclass(frozen=True)
class StopProcessFullAction:
    pattern: str


@dataclass(frozen=True)
class RemovePathAction:
    path: str


@dataclass(frozen=True)
class RunScriptAction:
    path: str


RemoteAction = Union[
    PrepareDirsAction,
    InitializeDataRootAction,
    InstallPermissionsAction,
    EnableNbnsAction,
    StopProcessAction,
    StopProcessFullAction,
    RemovePathAction,
    RunScriptAction,
]


def _render_process_present(pattern: str, *, full: bool) -> str:
    if full:
        ps_match = f"*{pattern}*"
        return (
            "found=1; "
            "if ps ax -o command= >/tmp/tcapsule-ps.$$ 2>/dev/null; then "
            "found=0; "
            "while IFS= read line; do "
            f'case "$line" in {ps_match}) found=1; break ;; esac; '
            "done </tmp/tcapsule-ps.$$; "
            "rm -f /tmp/tcapsule-ps.$$; "
            "fi; "
            '[ \"$found\" -eq 1 ]'
        )

    return (
        "found=1; "
        "if ps ax -o ucomm= >/tmp/tcapsule-ps.$$ 2>/dev/null; then "
        "found=0; "
        "while IFS= read line; do "
        f'case \"$line\" in {shlex.quote(pattern)}) found=1; break ;; esac; '
        "done </tmp/tcapsule-ps.$$; "
        "rm -f /tmp/tcapsule-ps.$$; "
        "fi; "
        '[ \"$found\" -eq 1 ]'
    )


def prepare_dirs_action(payload_dir: str) -> RemoteAction:
    return PrepareDirsAction(payload_dir)


def initialize_data_root_action(data_root: str, marker_path: str) -> RemoteAction:
    return InitializeDataRootAction(data_root, marker_path)


def install_permissions_action(payload_dir: str) -> RemoteAction:
    return InstallPermissionsAction(payload_dir)


def enable_nbns_action(private_dir: str) -> RemoteAction:
    return EnableNbnsAction(private_dir)


def stop_process_action(name: str) -> RemoteAction:
    return StopProcessAction(name)


def stop_process_full_action(pattern: str) -> RemoteAction:
    return StopProcessFullAction(pattern)


def remove_path_action(path: str) -> RemoteAction:
    return RemovePathAction(path)


def run_script_action(path: str) -> RemoteAction:
    return RunScriptAction(path)


def _render_stop_process_action(action: StopProcessAction) -> str:
    name = action.name
    return (
        f"pkill {shlex.quote(name)} >/dev/null 2>&1 || true; "
        "attempt=0; "
        f"while /bin/sh -c {shlex.quote(_render_process_present(name, full=False))} >/dev/null 2>&1; do "
        'if [ "$attempt" -ge 10 ]; then break; fi; '
        "attempt=$((attempt + 1)); "
        "sleep 1; "
        "done"
    )


def _render_stop_process_full_action(action: StopProcessFullAction) -> str:
    pattern = action.pattern
    return (
        f"pkill -f {shlex.quote(pattern)} >/dev/null 2>&1 || true; "
        "attempt=0; "
        f"while /bin/sh -c {shlex.quote(_render_process_present(pattern, full=True))} >/dev/null 2>&1; do "
        'if [ "$attempt" -ge 10 ]; then break; fi; '
        "attempt=$((attempt + 1)); "
        "sleep 1; "
        "done"
    )


def _render_prepare_dirs_action(action: PrepareDirsAction) -> str:
    payload_dir = action.payload_dir
    return (
        "mkdir -p {} {} {} {} {} {} && "
        "rm -rf {} {} {} {} && "
        "ln -s {} {} && "
        "ln -s {} {} && "
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
        shlex.quote("/root/tc-netbsd4le"),
        shlex.quote("/root/tc-netbsd4be"),
        shlex.quote("/root/tc-netbsd7"),
        shlex.quote("/mnt/Memory/samba4"),
        shlex.quote("/root/tc-netbsd4"),
        shlex.quote("/mnt/Memory/samba4"),
        shlex.quote("/root/tc-netbsd4le"),
        shlex.quote("/mnt/Memory/samba4"),
        shlex.quote("/root/tc-netbsd4be"),
        shlex.quote("/mnt/Memory/samba4"),
        shlex.quote("/root/tc-netbsd7"),
    )


def _render_initialize_data_root_action(action: InitializeDataRootAction) -> str:
    data_root = action.data_root
    marker_path = action.marker_path
    return (
        f"mkdir -p {shlex.quote(data_root)} && "
        f"/bin/sh -c {shlex.quote(f': > {shlex.quote(marker_path)}')}"
    )


def _render_install_permissions_action(action: InstallPermissionsAction) -> str:
    payload_dir = action.payload_dir
    private_dir = f"{payload_dir}/private"
    return (
        f"chmod 755 {shlex.quote(payload_dir + '/smbd')} "
        f"{shlex.quote(payload_dir + '/mdns-advertiser')} "
        f"{shlex.quote(payload_dir + '/nbns-advertiser')} && "
        f"chmod 755 {shlex.quote('/mnt/Flash/rc.local')} "
        f"{shlex.quote('/mnt/Flash/common.sh')} "
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


def _render_enable_nbns_action(action: EnableNbnsAction) -> str:
    marker_path = action.private_dir + "/nbns.enabled"
    return f"/bin/sh -c {shlex.quote(f': > {shlex.quote(marker_path)}')}"


def _render_remove_path_action(action: RemovePathAction) -> str:
    return f"rm -rf {shlex.quote(action.path)}"


def _render_run_script_action(action: RunScriptAction) -> str:
    return f"/bin/sh {shlex.quote(action.path)}"


def render_remote_action(action: RemoteAction) -> str:
    if isinstance(action, StopProcessAction):
        return _render_stop_process_action(action)
    if isinstance(action, StopProcessFullAction):
        return _render_stop_process_full_action(action)
    if isinstance(action, PrepareDirsAction):
        return _render_prepare_dirs_action(action)
    if isinstance(action, InitializeDataRootAction):
        return _render_initialize_data_root_action(action)
    if isinstance(action, InstallPermissionsAction):
        return _render_install_permissions_action(action)
    if isinstance(action, EnableNbnsAction):
        return _render_enable_nbns_action(action)
    if isinstance(action, RemovePathAction):
        return _render_remove_path_action(action)
    if isinstance(action, RunScriptAction):
        return _render_run_script_action(action)
    raise TypeError(f"Unsupported remote action: {action!r}")


def render_remote_actions(actions: list[RemoteAction]) -> list[str]:
    return [render_remote_action(action) for action in actions]


def _action_json(kind: str, *args: str) -> dict[str, object]:
    return {"kind": kind, "args": list(args)}


def remote_action_to_jsonable(action: RemoteAction) -> dict[str, object]:
    if isinstance(action, StopProcessAction):
        return _action_json("stop_process", action.name)
    if isinstance(action, StopProcessFullAction):
        return _action_json("stop_process_full", action.pattern)
    if isinstance(action, PrepareDirsAction):
        return _action_json("prepare_dirs", action.payload_dir)
    if isinstance(action, InitializeDataRootAction):
        return _action_json("initialize_data_root", action.data_root, action.marker_path)
    if isinstance(action, InstallPermissionsAction):
        return _action_json("install_permissions", action.payload_dir)
    if isinstance(action, EnableNbnsAction):
        return _action_json("enable_nbns", action.private_dir)
    if isinstance(action, RemovePathAction):
        return _action_json("remove_path", action.path)
    if isinstance(action, RunScriptAction):
        return _action_json("run_script", action.path)
    raise TypeError(f"Unsupported remote action: {action!r}")


def remote_actions_to_jsonable(actions: list[RemoteAction]) -> list[dict[str, object]]:
    return [remote_action_to_jsonable(action) for action in actions]
