from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.artifact_resolver import resolve_artifact, resolve_payload_artifacts


class ArtifactResolverTests(unittest.TestCase):
    def test_resolve_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "smbd")
        self.assertEqual(artifact.repo_relative_path, "bin/samba4/smbd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "samba4" / "smbd")

    def test_resolve_netbsd4le_smbd_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "smbd-netbsd4le")
        self.assertEqual(artifact.repo_relative_path, "bin/samba4-netbsd4le/smbd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "samba4-netbsd4le" / "smbd")

    def test_resolve_discovery_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "discovery")
        self.assertEqual(artifact.repo_relative_path, "bin/discovery/discoveryd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "discovery" / "discoveryd")

    def test_resolve_netbsd4le_helper_artifacts_return_expected_repo_paths(self) -> None:
        discovery = resolve_artifact(REPO_ROOT, "discovery-netbsd4le")
        self.assertEqual(discovery.repo_relative_path, "bin/discovery-netbsd4le/discoveryd")

    def test_resolve_explicit_netbsd4_be_artifacts_return_expected_repo_paths(self) -> None:
        smbd = resolve_artifact(REPO_ROOT, "smbd-netbsd4be")
        discovery = resolve_artifact(REPO_ROOT, "discovery-netbsd4be")
        self.assertEqual(smbd.repo_relative_path, "bin/samba4-netbsd4be/smbd")
        self.assertEqual(discovery.repo_relative_path, "bin/discovery-netbsd4be/discoveryd")

    def test_resolve_netbsd4_payload_returns_logical_deploy_names(self) -> None:
        artifacts = resolve_payload_artifacts(REPO_ROOT, "netbsd4le_samba4")
        self.assertEqual(artifacts["smbd"].repo_relative_path, "bin/samba4-netbsd4le/smbd")
        self.assertEqual(
            artifacts["xattr_migrator"].repo_relative_path,
            "bin/xattr-migrate-netbsd4le/xattr-hfs-migrate",
        )
        self.assertEqual(artifacts["discovery"].repo_relative_path, "bin/discovery-netbsd4le/discoveryd")
        self.assertNotIn("samba-dcerpcd", artifacts)
        self.assertNotIn("rpcd_classic", artifacts)

    def test_resolve_netbsd6_payload_returns_current_logical_deploy_names(self) -> None:
        artifacts = resolve_payload_artifacts(REPO_ROOT, "netbsd6_samba4")
        self.assertEqual(artifacts["smbd"].repo_relative_path, "bin/samba4/smbd")
        self.assertEqual(
            artifacts["xattr_migrator"].repo_relative_path,
            "bin/xattr-migrate/xattr-hfs-migrate",
        )
        self.assertEqual(artifacts["discovery"].repo_relative_path, "bin/discovery/discoveryd")
        self.assertNotIn("samba-dcerpcd", artifacts)
        self.assertNotIn("rpcd_classic", artifacts)

    def test_resolve_artifact_raises_for_unknown_name(self) -> None:
        with self.assertRaises(KeyError):
            resolve_artifact(REPO_ROOT, "missing-artifact")


if __name__ == "__main__":
    unittest.main()
