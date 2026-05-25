from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
import re
import tempfile


UNRESOLVED_TOKEN_PATTERN = re.compile(r"__[A-Z0-9_]+__")


class BootAssetError(RuntimeError):
    """Raised when a packaged boot asset is invalid."""


COMMON_SH_FRAGMENTS = (
    "common.d/00-env-log.sh",
    "common.d/20-airport-identity.sh",
    "common.d/30-processes.sh",
    "common.d/40-storage-discovery.sh",
    "common.d/45-network-bind.sh",
    "common.d/50-runtime-staging.sh",
    "common.d/60-advertisers.sh",
    "common.d/70-smbd-service.sh",
    "common.d/80-manager-reconcile.sh",
)


def boot_asset_root():
    return resources.files("timecapsulesmb.assets.boot.samba4")


def assemble_common_sh_text() -> str:
    root = boot_asset_root()
    parts = [root.joinpath(fragment).read_text().rstrip("\n") for fragment in COMMON_SH_FRAGMENTS]
    return "\n".join(parts) + "\n"


def load_boot_asset_text(name: str) -> str:
    if name == "common.sh":
        return assemble_common_sh_text()
    return boot_asset_root().joinpath(name).read_text()


@contextmanager
def boot_asset_path(name: str) -> Iterator[Path]:
    if name == "common.sh":
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as temp:
                temp.write(assemble_common_sh_text())
                temp_path = Path(temp.name)
            yield temp_path
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return

    with resources.as_file(boot_asset_root().joinpath(name)) as path:
        yield path


def unresolved_asset_tokens(content: str) -> tuple[str, ...]:
    return tuple(sorted(set(UNRESOLVED_TOKEN_PATTERN.findall(content))))


def require_no_unresolved_asset_tokens(
    content: str,
    *,
    allowed_tokens: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> None:
    allowed = set(allowed_tokens)
    unresolved = [token for token in unresolved_asset_tokens(content) if token not in allowed]
    if unresolved:
        raise BootAssetError(f"unresolved boot asset token(s): {', '.join(unresolved)}")
