from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.deploy.artifacts import ArtifactRecord, load_artifact_manifest


@dataclass(frozen=True)
class ResolvedArtifact:
    name: str
    repo_relative_path: str
    absolute_path: Path
    sha256: str


def resolve_artifact(repo_root: Path, name: str) -> ResolvedArtifact:
    manifest = load_artifact_manifest()
    record = manifest.get(name)
    if record is None:
        raise KeyError(f"Unknown artifact: {name}")
    return resolved_artifact_from_record(repo_root, record)


def resolve_required_artifacts(repo_root: Path, names: list[str]) -> dict[str, ResolvedArtifact]:
    return {name: resolve_artifact(repo_root, name) for name in names}


def resolved_artifact_from_record(repo_root: Path, record: ArtifactRecord) -> ResolvedArtifact:
    return ResolvedArtifact(
        name=record.name,
        repo_relative_path=record.path,
        absolute_path=repo_root / record.path,
        sha256=record.sha256,
    )
