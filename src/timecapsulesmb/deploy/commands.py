from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable, Union


@dataclass(frozen=True)
class RemoteSymlink:
    path: str
    target: str


@dataclass(frozen=True)
class RemotePermission:
    path: str
    mode: str
    optional: bool = False


@dataclass(frozen=True)
class PrepareDirsAction:
    directories: tuple[str, ...]
    recreated_symlinks: tuple[RemoteSymlink, ...]


@dataclass(frozen=True)
class InitializeDataRootAction:
    data_root: str
    marker_path: str


@dataclass(frozen=True)
class InstallPermissionsAction:
    permissions: tuple[RemotePermission, ...]


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


def prepare_dirs_action(
    directories: Iterable[str],
    recreated_symlinks: Iterable[RemoteSymlink] = (),
) -> RemoteAction:
    return PrepareDirsAction(tuple(directories), tuple(recreated_symlinks))


def initialize_data_root_action(data_root: str, marker_path: str) -> RemoteAction:
    return InitializeDataRootAction(data_root, marker_path)


def install_permissions_action(permissions: Iterable[RemotePermission]) -> RemoteAction:
    return InstallPermissionsAction(tuple(permissions))


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
    commands: list[str] = []
    if action.directories:
        commands.append("mkdir -p {}".format(" ".join(shlex.quote(path) for path in action.directories)))
    if action.recreated_symlinks:
        commands.append("rm -rf {}".format(" ".join(shlex.quote(link.path) for link in action.recreated_symlinks)))
        commands.extend(
            f"ln -s {shlex.quote(link.target)} {shlex.quote(link.path)}"
            for link in action.recreated_symlinks
        )
    return " && ".join(commands) if commands else "true"


def _render_initialize_data_root_action(action: InitializeDataRootAction) -> str:
    data_root = action.data_root
    marker_path = action.marker_path
    return (
        f"mkdir -p {shlex.quote(data_root)} && "
        f"/bin/sh -c {shlex.quote(f': > {shlex.quote(marker_path)}')}"
    )


def _render_install_permissions_action(action: InstallPermissionsAction) -> str:
    commands: list[str] = []
    for permission in action.permissions:
        chmod = f"chmod {shlex.quote(permission.mode)} {shlex.quote(permission.path)}"
        if permission.optional:
            commands.append(f"if [ -e {shlex.quote(permission.path)} ]; then {chmod}; fi")
        else:
            commands.append(chmod)
    return " && ".join(commands) if commands else "true"


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
        return {
            "kind": "prepare_dirs",
            "directories": list(action.directories),
            "recreated_symlinks": [
                {"path": link.path, "target": link.target}
                for link in action.recreated_symlinks
            ],
        }
    if isinstance(action, InitializeDataRootAction):
        return _action_json("initialize_data_root", action.data_root, action.marker_path)
    if isinstance(action, InstallPermissionsAction):
        return {
            "kind": "install_permissions",
            "permissions": [
                {"path": permission.path, "mode": permission.mode, "optional": permission.optional}
                for permission in action.permissions
            ],
        }
    if isinstance(action, RemovePathAction):
        return _action_json("remove_path", action.path)
    if isinstance(action, RunScriptAction):
        return _action_json("run_script", action.path)
    raise TypeError(f"Unsupported remote action: {action!r}")


def remote_actions_to_jsonable(actions: list[RemoteAction]) -> list[dict[str, object]]:
    return [remote_action_to_jsonable(action) for action in actions]
