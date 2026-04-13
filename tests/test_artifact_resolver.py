from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.artifact_resolver import resolve_artifact, resolve_required_artifacts


class ArtifactResolverTests(unittest.TestCase):
    def test_resolve_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "smbd")
        self.assertEqual(artifact.repo_relative_path, "bin/samba4/smbd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "samba4" / "smbd")

    def test_resolve_netbsd4_smbd_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "smbd-netbsd4")
        self.assertEqual(artifact.repo_relative_path, "bin/samba4-netbsd4/smbd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "samba4-netbsd4" / "smbd")

    def test_resolve_nbns_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "nbns-advertiser")
        self.assertEqual(artifact.repo_relative_path, "bin/nbns/nbns-advertiser")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "nbns" / "nbns-advertiser")

    def test_resolve_required_artifacts_returns_named_mapping(self) -> None:
        artifacts = resolve_required_artifacts(REPO_ROOT, ["smbd", "mdns-advertiser", "nbns-advertiser"])
        self.assertIn("smbd", artifacts)
        self.assertIn("mdns-advertiser", artifacts)
        self.assertIn("nbns-advertiser", artifacts)

    def test_resolve_artifact_raises_for_unknown_name(self) -> None:
        with self.assertRaises(KeyError):
            resolve_artifact(REPO_ROOT, "missing-artifact")


if __name__ == "__main__":
    unittest.main()
