from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.deploy.artifacts import ArtifactRecord, load_artifact_manifest, sha256_file, validate_artifacts


class ArtifactTests(unittest.TestCase):
    def test_load_artifact_manifest_contains_expected_records(self) -> None:
        manifest = load_artifact_manifest()
        self.assertIn("smbd", manifest)
        self.assertIn("smbd-netbsd4", manifest)
        self.assertIn("smbd-samba3-netbsd4", manifest)
        self.assertIn("mdns-advertiser", manifest)
        self.assertIn("mdns-advertiser-netbsd4", manifest)
        self.assertIn("nbns-advertiser", manifest)
        self.assertIn("nbns-advertiser-netbsd4", manifest)

    def test_sha256_file_matches_known_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            path.write_bytes(b"abc")
            self.assertEqual(sha256_file(path), hashlib.sha256(b"abc").hexdigest())

    def test_validate_artifacts_reports_missing_file(self) -> None:
        record = ArtifactRecord(name="missing", path="bin/missing", sha256="deadbeef")
        with mock.patch("timecapsulesmb.deploy.artifacts.load_artifact_manifest", return_value={"missing": record}):
            results = validate_artifacts(REPO_ROOT)
        self.assertEqual(results[0][1], False)
        self.assertIn("missing bin/missing", results[0][2])

    def test_validate_artifacts_reports_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "bin" / "fake"
            file_path.parent.mkdir(parents=True)
            file_path.write_text("not-right")
            record = ArtifactRecord(name="fake", path="bin/fake", sha256="00")
            with mock.patch("timecapsulesmb.deploy.artifacts.load_artifact_manifest", return_value={"fake": record}):
                results = validate_artifacts(root)
        self.assertEqual(results[0][1], False)
        self.assertIn("checksum mismatch", results[0][2])


if __name__ == "__main__":
    unittest.main()
