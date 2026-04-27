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

    def test_resolve_netbsd4le_samba3_smbd_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "smbd-samba3-netbsd4le")
        self.assertEqual(artifact.repo_relative_path, "bin/samba3-netbsd4le/smbd")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "samba3-netbsd4le" / "smbd")

    def test_resolve_nbns_artifact_returns_expected_repo_path(self) -> None:
        artifact = resolve_artifact(REPO_ROOT, "nbns-advertiser")
        self.assertEqual(artifact.repo_relative_path, "bin/nbns/nbns-advertiser")
        self.assertEqual(artifact.absolute_path, REPO_ROOT / "bin" / "nbns" / "nbns-advertiser")

    def test_resolve_netbsd4le_helper_artifacts_return_expected_repo_paths(self) -> None:
        mdns = resolve_artifact(REPO_ROOT, "mdns-advertiser-netbsd4le")
        nbns = resolve_artifact(REPO_ROOT, "nbns-advertiser-netbsd4le")
        self.assertEqual(mdns.repo_relative_path, "bin/mdns-netbsd4le/mdns-advertiser")
        self.assertEqual(nbns.repo_relative_path, "bin/nbns-netbsd4le/nbns-advertiser")

    def test_resolve_explicit_netbsd4_be_artifacts_return_expected_repo_paths(self) -> None:
        smbd = resolve_artifact(REPO_ROOT, "smbd-netbsd4be")
        mdns = resolve_artifact(REPO_ROOT, "mdns-advertiser-netbsd4be")
        nbns = resolve_artifact(REPO_ROOT, "nbns-advertiser-netbsd4be")
        self.assertEqual(smbd.repo_relative_path, "bin/samba4-netbsd4be/smbd")
        self.assertEqual(mdns.repo_relative_path, "bin/mdns-netbsd4be/mdns-advertiser")
        self.assertEqual(nbns.repo_relative_path, "bin/nbns-netbsd4be/nbns-advertiser")

    def test_resolve_netbsd4_payload_returns_logical_deploy_names(self) -> None:
        artifacts = resolve_payload_artifacts(REPO_ROOT, "netbsd4le_samba4")
        self.assertEqual(artifacts["smbd"].repo_relative_path, "bin/samba4-netbsd4le/smbd")
        self.assertEqual(artifacts["mdns-advertiser"].repo_relative_path, "bin/mdns-netbsd4le/mdns-advertiser")
        self.assertEqual(artifacts["nbns-advertiser"].repo_relative_path, "bin/nbns-netbsd4le/nbns-advertiser")

    def test_resolve_netbsd6_payload_returns_current_logical_deploy_names(self) -> None:
        artifacts = resolve_payload_artifacts(REPO_ROOT, "netbsd6_samba4")
        self.assertEqual(artifacts["smbd"].repo_relative_path, "bin/samba4/smbd")
        self.assertEqual(artifacts["mdns-advertiser"].repo_relative_path, "bin/mdns/mdns-advertiser")
        self.assertEqual(artifacts["nbns-advertiser"].repo_relative_path, "bin/nbns/nbns-advertiser")

    def test_resolve_artifact_raises_for_unknown_name(self) -> None:
        with self.assertRaises(KeyError):
            resolve_artifact(REPO_ROOT, "missing-artifact")


if __name__ == "__main__":
    unittest.main()
