from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    project_root: Path
    env_path: Path
    package_root: Path


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def package_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _has_project_markers(path: Path) -> bool:
    if (path / "tcapsule").is_file() and (path / "src" / "timecapsulesmb").is_dir():
        return True
    if (path / "pyproject.toml").is_file() and (path / "src" / "timecapsulesmb").is_dir():
        return True
    if (path / ".env").is_file() and (
        (path / "tcapsule").is_file()
        or (path / "bin").is_dir()
        or (path / "src" / "timecapsulesmb").is_dir()
    ):
        return True
    return False


def resolve_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    candidates = (current, *current.parents) if current.is_dir() else (current.parent, *current.parent.parents)
    for candidate in candidates:
        if _has_project_markers(candidate):
            return candidate
    return package_project_root()


def resolve_app_paths(start: Path | None = None) -> AppPaths:
    root = resolve_project_root(start)
    return AppPaths(
        project_root=root,
        env_path=root / ".env",
        package_root=package_root(),
    )
