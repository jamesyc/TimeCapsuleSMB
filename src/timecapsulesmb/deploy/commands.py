from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable, Union

from timecapsulesmb.device.processes import (
    render_pkill_wait_pkill9_by_ucomm,
    render_pkill_wait_pkill9_watchdog,
)


@dataclass(frozen=True)
class RemoteSymlink:
    path: str
    target: str


@dataclass(frozen=True)
class RemotePermission:
    path: str
    mode: str


@dataclass(frozen=True)
class PrepareDirsAction:
    directories: tuple[str, ...]
    recreated_symlinks: tuple[RemoteSymlink, ...]


@dataclass(frozen=True)
class InstallPermissionsAction:
    permissions: tuple[RemotePermission, ...]


@dataclass(frozen=True)
class StopProcessAction:
    name: str


@dataclass(frozen=True)
class StopWatchdogAction:
    pass


@dataclass(frozen=True)
class RemovePathAction:
    path: str


@dataclass(frozen=True)
class RunScriptAction:
    path: str


RemoteAction = Union[
    PrepareDirsAction,
    InstallPermissionsAction,
    StopProcessAction,
    StopWatchdogAction,
    RemovePathAction,
    RunScriptAction,
]


def prepare_dirs_action(
    directories: Iterable[str],
    recreated_symlinks: Iterable[RemoteSymlink] = (),
) -> RemoteAction:
    return PrepareDirsAction(tuple(directories), tuple(recreated_symlinks))


def install_permissions_action(permissions: Iterable[RemotePermission]) -> RemoteAction:
    return InstallPermissionsAction(tuple(permissions))


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


def _render_install_permissions_action(action: InstallPermissionsAction) -> str:
    commands: list[str] = []
    for permission in action.permissions:
        commands.append(f"chmod {shlex.quote(permission.mode)} {shlex.quote(permission.path)}")
    return " && ".join(commands) if commands else "true"


def _render_remove_path_action(action: RemovePathAction) -> str:
    path = action.path
    if path.rstrip("/") == "/mnt/Flash" or (
        path.startswith("/mnt/Flash")
        and len(path) > len("/mnt/Flash")
        and path[len("/mnt/Flash")].isspace()
    ):
        raise ValueError(f"Refusing to remove flash root path: {path}")
    return f"rm -rf {shlex.quote(path)}"


def render_remote_action(action: RemoteAction) -> str:
    if isinstance(action, StopProcessAction):
        return render_pkill_wait_pkill9_by_ucomm(action.name, attempts=5)
    if isinstance(action, StopWatchdogAction):
        return render_pkill_wait_pkill9_watchdog(attempts=5)
    if isinstance(action, PrepareDirsAction):
        return _render_prepare_dirs_action(action)
    if isinstance(action, InstallPermissionsAction):
        return _render_install_permissions_action(action)
    if isinstance(action, RemovePathAction):
        return _render_remove_path_action(action)
    if isinstance(action, RunScriptAction):
        return f"/bin/sh {shlex.quote(action.path)}"
    raise TypeError(f"Unsupported remote action: {action!r}")


def render_remote_actions(actions: list[RemoteAction]) -> list[str]:
    return [render_remote_action(action) for action in actions]


def remote_action_to_jsonable(action: RemoteAction) -> dict[str, object]:
    if isinstance(action, StopProcessAction):
        return {"kind": "stop_process", "args": [action.name]}
    if isinstance(action, StopWatchdogAction):
        return {"kind": "stop_watchdog", "args": []}
    if isinstance(action, PrepareDirsAction):
        return {
            "kind": "prepare_dirs",
            "directories": list(action.directories),
            "recreated_symlinks": [
                {"path": link.path, "target": link.target}
                for link in action.recreated_symlinks
            ],
        }
    if isinstance(action, InstallPermissionsAction):
        return {
            "kind": "install_permissions",
            "permissions": [
                {"path": permission.path, "mode": permission.mode}
                for permission in action.permissions
            ],
        }
    if isinstance(action, RemovePathAction):
        return {"kind": "remove_path", "args": [action.path]}
    if isinstance(action, RunScriptAction):
        return {"kind": "run_script", "args": [action.path]}
    raise TypeError(f"Unsupported remote action: {action!r}")


def remote_actions_to_jsonable(actions: list[RemoteAction]) -> list[dict[str, object]]:
    return [remote_action_to_jsonable(action) for action in actions]
