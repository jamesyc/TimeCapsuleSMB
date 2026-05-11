from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
import re


UNRESOLVED_TOKEN_PATTERN = re.compile(r"__[A-Z0-9_]+__")


class BootAssetError(RuntimeError):
    """Raised when a packaged boot asset is invalid."""


def load_boot_asset_text(name: str) -> str:
    return resources.files("timecapsulesmb.assets.boot.samba4").joinpath(name).read_text()


@contextmanager
def boot_asset_path(name: str) -> Iterator[Path]:
    with resources.as_file(resources.files("timecapsulesmb.assets.boot.samba4").joinpath(name)) as path:
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
