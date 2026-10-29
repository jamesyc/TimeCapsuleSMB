#!/usr/bin/env python3
"""Check a pushed release tag before the release job builds it.

The tag must be the one release.py names, and its version code must be above
the code of the newest published release, prereleases included: Sparkle never
offers an equal or lower code, so a release at or below a hidden build's code
would never reach the people running that hidden build. Codes rise in
publishing order because this check makes them, so being above the newest is
being above all.

A CLI_VERSION with a -N suffix is a hidden build, published as a prerelease.
When GITHUB_OUTPUT is set, a passing check appends prerelease=true or
prerelease=false for the release step. Under GitHub Actions, each failure is
also printed as an error annotation, so it shows on the run's summary page.
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_PY = REPO_ROOT / "src" / "timecapsulesmb" / "core" / "release.py"


def read_release(path: Path, wanted: dict[str, type]) -> dict[str, object]:
    """The literals a release.py assigns to the wanted names, read without
    running the file.

    The last plain top-level assignment wins; an annotated assignment
    counts.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    value_nodes: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                value_nodes[target.id] = node.value
    values: dict[str, object] = {}
    for name, kind in wanted.items():
        if name not in value_nodes:
            raise ValueError(f"{path}: {name} is not assigned")
        try:
            value = ast.literal_eval(value_nodes[name])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}: {name} is not a literal") from exc
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ValueError(f"{path}: {name} is {value!r}, not {kind.__name__}")
        values[name] = value
    return values


def report(passed: bool, line: str) -> None:
    print(f"{'PASS' if passed else 'FAIL'} {line}")
    if not passed and os.environ.get("GITHUB_ACTIONS") == "true":
        # Workflow commands decode these three, so a message must encode them.
        message = line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Release check::{message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a release tag against release.py and the newest published release."
    )
    parser.add_argument("tag", help="The pushed tag, such as v3.8.0.")
    parser.add_argument("--newest-release-py", type=Path, required=True,
                        help="release.py at the newest published release's tag, prereleases included.")
    parser.add_argument("--release-py", type=Path, default=RELEASE_PY, help="This commit's release.py.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        current = read_release(args.release_py, {"CLI_VERSION": str, "CLI_VERSION_CODE": int})
    except ValueError as exc:
        report(False, f"release.py: {exc}")
        return 1

    version = current["CLI_VERSION"]
    code = current["CLI_VERSION_CODE"]
    results: list[tuple[bool, str]] = []
    release_tag = f"v{version}"
    if args.tag == release_tag:
        results.append((True, f"tag: {args.tag} is release.py's tag"))
    else:
        results.append((False, f"tag: {args.tag} is not release.py's tag {release_tag}"))
    try:
        newest = read_release(args.newest_release_py, {"CLI_VERSION_CODE": int})["CLI_VERSION_CODE"]
    except ValueError as exc:
        results.append((False, f"version code: newest release: {exc}"))
    else:
        if code > newest:
            results.append((True, f"version code: {code} is above the newest release's {newest}"))
        else:
            results.append((False, f"version code: {code} is not above the newest release's {newest}"))

    for passed, line in results:
        report(passed, line)
    if not all(passed for passed, _ in results):
        return 1

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"prerelease={'true' if '-' in version else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
