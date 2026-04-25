from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from timecapsulesmb.deploy.artifacts import ArtifactRecord, load_artifact_manifest
from timecapsulesmb.device.compat import (
    PAYLOAD_FAMILY_NETBSD4BE,
    PAYLOAD_FAMILY_NETBSD4LE,
    PAYLOAD_FAMILY_NETBSD6,
)


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


def resolve_payload_artifacts(repo_root: Path, payload_family: str) -> dict[str, ResolvedArtifact]:
    if payload_family == PAYLOAD_FAMILY_NETBSD4LE:
        names = {
            "smbd": "smbd-netbsd4le",
            "mdns-advertiser": "mdns-advertiser-netbsd4le",
            "nbns-advertiser": "nbns-advertiser-netbsd4le",
        }
    elif payload_family == PAYLOAD_FAMILY_NETBSD4BE:
        names = {
            "smbd": "smbd-netbsd4be",
            "mdns-advertiser": "mdns-advertiser-netbsd4be",
            "nbns-advertiser": "nbns-advertiser-netbsd4be",
        }
    elif payload_family == PAYLOAD_FAMILY_NETBSD6:
        names = {
            "smbd": "smbd",
            "mdns-advertiser": "mdns-advertiser",
            "nbns-advertiser": "nbns-advertiser",
        }
    else:
        raise KeyError(f"Unknown payload family: {payload_family}")

    return {logical_name: resolve_artifact(repo_root, artifact_name) for logical_name, artifact_name in names.items()}


def resolved_artifact_from_record(repo_root: Path, record: ArtifactRecord) -> ResolvedArtifact:
    return ResolvedArtifact(
        name=record.name,
        repo_relative_path=record.path,
        absolute_path=repo_root / record.path,
        sha256=record.sha256,
    )
