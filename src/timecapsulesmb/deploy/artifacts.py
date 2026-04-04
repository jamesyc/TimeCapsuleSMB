from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class ArtifactRecord:
    name: str
    path: str
    sha256: str


def load_artifact_manifest() -> dict[str, ArtifactRecord]:
    manifest_text = resources.files("timecapsulesmb.assets").joinpath("artifact-manifest.json").read_text()
    raw = json.loads(manifest_text)
    return {
        name: ArtifactRecord(name=name, path=entry["path"], sha256=entry["sha256"])
        for name, entry in raw["artifacts"].items()
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_artifacts(repo_root: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for record in load_artifact_manifest().values():
        path = repo_root / record.path
        if not path.exists():
            results.append((record.name, False, f"missing {record.path}"))
            continue
        actual = sha256_file(path)
        if actual != record.sha256:
            results.append((record.name, False, f"checksum mismatch for {record.path}"))
            continue
        results.append((record.name, True, f"validated {record.path}"))
    return results
