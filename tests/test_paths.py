from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.paths import (
    DistributionRootError,
    TCAPSULE_CONFIG_ENV,
    TCAPSULE_DISTRIBUTION_ROOT_ENV,
    TCAPSULE_STATE_DIR_ENV,
    default_user_data_dir,
    manifest_artifact_paths,
    resolve_app_paths,
    resolve_distribution_root,
    validate_distribution_root,
)


class PathResolutionTests(unittest.TestCase):
    def _write_manifest_payloads(self, root: Path) -> None:
        for relative_path in manifest_artifact_paths():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"payload")

    def _write_source_checkout_markers(self, root: Path) -> None:
        (root / "tcapsule").write_text("#!/usr/bin/env python3\n")
        (root / "src" / "timecapsulesmb").mkdir(parents=True)

    def _clear_path_env(self):
        return mock.patch.dict(
            os.environ,
            {
                TCAPSULE_DISTRIBUTION_ROOT_ENV: "",
                TCAPSULE_CONFIG_ENV: "",
                TCAPSULE_STATE_DIR_ENV: "",
            },
        )

    def test_resolve_distribution_root_discovers_parent_source_checkout_with_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            self._write_source_checkout_markers(root)
            self._write_manifest_payloads(root)

            with self._clear_path_env():
                self.assertEqual(resolve_distribution_root(nested), root)
                app_paths = resolve_app_paths(nested)

        self.assertEqual(app_paths.distribution_root, root)
        self.assertEqual(app_paths.config_path, root / ".env")
        self.assertEqual(app_paths.state_dir, root)

    def test_explicit_distribution_root_only_requires_manifest_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._write_manifest_payloads(root)
            with mock.patch.dict(os.environ, {TCAPSULE_DISTRIBUTION_ROOT_ENV: str(root)}):
                self.assertEqual(resolve_distribution_root(Path("/")), root)

    def test_explicit_distribution_root_reports_missing_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {TCAPSULE_DISTRIBUTION_ROOT_ENV: str(root)}):
                with self.assertRaises(DistributionRootError) as ctx:
                    resolve_distribution_root(root)

        self.assertIn("Invalid TimeCapsuleSMB distribution root", str(ctx.exception))
        self.assertIn("Missing checked-in payload artifact", str(ctx.exception))
        self.assertIn("bin/", str(ctx.exception))

    def test_config_path_precedence_prefers_explicit_then_env_then_repo_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._write_manifest_payloads(root)
            env_config = root / "env-config"
            cli_config = root / "cli-config"
            with mock.patch.dict(
                os.environ,
                {
                    TCAPSULE_DISTRIBUTION_ROOT_ENV: str(root),
                    TCAPSULE_CONFIG_ENV: str(env_config),
                    TCAPSULE_STATE_DIR_ENV: "",
                },
            ):
                self.assertEqual(resolve_app_paths().config_path, env_config)
                self.assertEqual(resolve_app_paths(config_path=cli_config).config_path, cli_config)

            with mock.patch.dict(
                os.environ,
                {
                    TCAPSULE_DISTRIBUTION_ROOT_ENV: str(root),
                    TCAPSULE_CONFIG_ENV: "",
                    TCAPSULE_STATE_DIR_ENV: "",
                },
            ):
                self.assertEqual(resolve_app_paths().config_path, root / ".env")

    def test_state_dir_override_moves_bootstrap_and_version_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "dist").resolve()
            state = Path(tmp, "state").resolve()
            self._write_manifest_payloads(root)
            with mock.patch.dict(
                os.environ,
                {
                    TCAPSULE_DISTRIBUTION_ROOT_ENV: str(root),
                    TCAPSULE_CONFIG_ENV: "",
                    TCAPSULE_STATE_DIR_ENV: str(state),
                },
            ):
                app_paths = resolve_app_paths()

        self.assertEqual(app_paths.state_dir, state)
        self.assertEqual(app_paths.bootstrap_path, state / ".bootstrap")
        self.assertEqual(app_paths.version_check_cache_path, state / ".version-check-cache.json")

    def test_default_user_data_dir_uses_platform_location(self) -> None:
        with mock.patch("timecapsulesmb.core.paths.platform.system", return_value="Darwin"):
            self.assertEqual(default_user_data_dir(), Path.home() / "Library" / "Application Support" / "TimeCapsuleSMB")

        with mock.patch("timecapsulesmb.core.paths.platform.system", return_value="Linux"):
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg"}):
                self.assertEqual(default_user_data_dir(), Path("/tmp/xdg/timecapsulesmb").resolve())

        with mock.patch("timecapsulesmb.core.paths.platform.system", return_value="Linux"):
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
                self.assertEqual(default_user_data_dir(), Path.home() / ".local" / "share" / "timecapsulesmb")

    def test_validate_distribution_root_returns_all_missing_manifest_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            validation = validate_distribution_root(root)

        self.assertFalse(validation.ok)
        self.assertGreaterEqual(len(validation.missing_artifacts), 1)
        self.assertTrue(any(path.startswith("bin/") for path in validation.missing_artifacts))


if __name__ == "__main__":
    unittest.main()
