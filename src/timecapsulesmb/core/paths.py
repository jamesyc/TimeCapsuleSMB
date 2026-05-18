from __future__ import annotations

from dataclasses import dataclass
import json
import os
import platform
from importlib import resources
from pathlib import Path


TCAPSULE_DISTRIBUTION_ROOT_ENV = "TCAPSULE_DISTRIBUTION_ROOT"
TCAPSULE_CONFIG_ENV = "TCAPSULE_CONFIG"
TCAPSULE_STATE_DIR_ENV = "TCAPSULE_STATE_DIR"


class DistributionRootError(RuntimeError):
    """Raised when TimeCapsuleSMB payload artifacts cannot be located."""


@dataclass(frozen=True)
class DistributionRootValidation:
    root: Path
    ok: bool
    missing_artifacts: tuple[str, ...] = ()

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        missing = ", ".join(self.missing_artifacts)
        return (
            f"Invalid TimeCapsuleSMB distribution root: {self.root}\n"
            f"Missing checked-in payload artifact(s): {missing}\n"
            "Run tcapsule from a TimeCapsuleSMB source checkout or set "
            f"{TCAPSULE_DISTRIBUTION_ROOT_ENV} to the checkout/payload root."
        )


@dataclass(frozen=True)
class AppPaths:
    distribution_root: Path
    config_path: Path
    state_dir: Path
    package_root: Path

    @property
    def project_root(self) -> Path:
        return self.distribution_root

    @property
    def env_path(self) -> Path:
        return self.config_path

    @property
    def bootstrap_path(self) -> Path:
        return self.state_dir / ".bootstrap"

    @property
    def version_check_cache_path(self) -> Path:
        return self.state_dir / ".version-check-cache.json"


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def package_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def artifact_manifest_resource() -> object:
    return resources.files("timecapsulesmb.assets").joinpath("artifact-manifest.json")


def artifact_manifest_source() -> str:
    return str(artifact_manifest_resource())


def manifest_artifact_paths() -> tuple[str, ...]:
    raw = json.loads(artifact_manifest_resource().read_text())
    return tuple(
        entry["path"]
        for entry in raw.get("artifacts", {}).values()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    )


def _resolve_user_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _has_source_checkout_markers(path: Path) -> bool:
    return (
        (path / "bin").is_dir()
        and (path / "src" / "timecapsulesmb").is_dir()
        and ((path / "tcapsule").is_file() or (path / "pyproject.toml").is_file())
    )


def is_source_distribution_root(root: Path | str) -> bool:
    return _has_source_checkout_markers(_resolve_user_path(root))


def validate_distribution_root(root: Path | str) -> DistributionRootValidation:
    resolved = _resolve_user_path(root)
    if not resolved.exists():
        return DistributionRootValidation(resolved, False, ("<distribution root does not exist>",))
    if not resolved.is_dir():
        return DistributionRootValidation(resolved, False, ("<distribution root is not a directory>",))

    missing = tuple(
        artifact_path
        for artifact_path in manifest_artifact_paths()
        if not (resolved / artifact_path).is_file()
    )
    return DistributionRootValidation(resolved, not missing, missing)


def require_valid_distribution_root(root: Path | str) -> Path:
    validation = validate_distribution_root(root)
    if not validation.ok:
        raise DistributionRootError(validation.error)
    return validation.root


def _candidate_roots(start: Path | None = None) -> tuple[Path, ...]:
    current = (start or Path.cwd()).resolve()
    return (current, *current.parents) if current.is_dir() else (current.parent, *current.parent.parents)


def resolve_distribution_root(start: Path | None = None) -> Path:
    explicit = os.getenv(TCAPSULE_DISTRIBUTION_ROOT_ENV, "").strip()
    if explicit:
        return require_valid_distribution_root(explicit)

    failed_source_candidates: list[DistributionRootValidation] = []
    for candidate in _candidate_roots(start):
        if not _has_source_checkout_markers(candidate):
            continue
        validation = validate_distribution_root(candidate)
        if validation.ok:
            return validation.root
        failed_source_candidates.append(validation)

    package_candidate = package_project_root()
    if _has_source_checkout_markers(package_candidate):
        validation = validate_distribution_root(package_candidate)
        if validation.ok:
            return validation.root
        failed_source_candidates.append(validation)

    if failed_source_candidates:
        raise DistributionRootError(failed_source_candidates[0].error)
    raise DistributionRootError(
        "Could not find a TimeCapsuleSMB source checkout with checked-in payload artifacts.\n"
        "Run tcapsule from the repository root or set "
        f"{TCAPSULE_DISTRIBUTION_ROOT_ENV} to the checkout/payload root."
    )


def resolve_project_root(start: Path | None = None) -> Path:
    return resolve_distribution_root(start)


def _resolve_config_path(distribution_root: Path, config_path: Path | str | None) -> Path:
    if config_path is not None:
        return _resolve_user_path(config_path)
    env_config = os.getenv(TCAPSULE_CONFIG_ENV, "").strip()
    if env_config:
        return _resolve_user_path(env_config)
    if is_source_distribution_root(distribution_root):
        return distribution_root / ".env"
    return default_user_data_dir() / ".env"


def _resolve_state_dir(distribution_root: Path) -> Path:
    env_state_dir = os.getenv(TCAPSULE_STATE_DIR_ENV, "").strip()
    if env_state_dir:
        return _resolve_user_path(env_state_dir)
    if is_source_distribution_root(distribution_root):
        return distribution_root
    return default_user_data_dir()


def default_user_data_dir() -> Path:
    """Return a Homebrew-safe user data directory for durable local artifacts."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "TimeCapsuleSMB"
    xdg_data_home = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser().resolve() / "timecapsulesmb"
    return Path.home() / ".local" / "share" / "timecapsulesmb"


def resolve_app_paths(start: Path | None = None, config_path: Path | str | None = None) -> AppPaths:
    root = resolve_distribution_root(start)
    return AppPaths(
        distribution_root=root,
        config_path=_resolve_config_path(root, config_path),
        state_dir=_resolve_state_dir(root),
        package_root=package_root(),
    )
