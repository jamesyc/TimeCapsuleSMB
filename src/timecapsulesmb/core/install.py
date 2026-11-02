"""How this copy of tcapsule was installed, which decides the commands its
messages give.

The Homebrew formula's `tcapsule` wrapper sets TCAPSULE_CLIENT=homebrew, which
telemetry also reports as the client. Anything else is a source checkout run
through `.venv/bin/tcapsule`, or the helper the Mac app starts, which sets
TCAPSULE_CLIENT=macos_gui (HelperLocator.helperEnvironment)."""
from __future__ import annotations

import os
import shlex

# `brew upgrade tcapsule` alone fetches the tap only when Homebrew last
# updated over a day ago, so it can miss a release that is already there.
HOMEBREW_UPGRADE_COMMAND = "brew update && brew upgrade tcapsule"


APP_CLIENT = "macos_gui"


def installed_with_homebrew() -> bool:
    return os.getenv("TCAPSULE_CLIENT", "").strip() == "homebrew"


def running_in_app() -> bool:
    return os.getenv("TCAPSULE_CLIENT", "").strip() == APP_CLIENT


def tcapsule_command(*args: str) -> str:
    """The command a person types to run tcapsule with `args`."""
    program = "tcapsule" if installed_with_homebrew() else ".venv/bin/tcapsule"
    return shlex.join([program, *args]) if args else f"{program} <command>"


def reinstall_command() -> str:
    """The command that installs tcapsule's Python packages again."""
    return "brew reinstall tcapsule" if installed_with_homebrew() else "./tcapsule bootstrap"
