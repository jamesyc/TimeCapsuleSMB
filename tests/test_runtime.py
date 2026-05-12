from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.cli.runtime import json_text, resolve_env_connection, write_json_file
from timecapsulesmb.core.config import AppConfig, DEFAULTS


class RuntimeTests(unittest.TestCase):
    def test_resolve_env_connection_defaults_ssh_opts_when_missing(self) -> None:
        config = AppConfig.from_values({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"})
        connection = resolve_env_connection(config)

        self.assertEqual(connection.host, "root@10.0.0.2")
        self.assertEqual(connection.password, "pw")
        self.assertEqual(connection.ssh_opts, DEFAULTS["TC_SSH_OPTS"])

    def test_resolve_env_connection_preserves_configured_ssh_opts(self) -> None:
        config = AppConfig.from_values({
            "TC_HOST": "root@10.0.0.2",
            "TC_PASSWORD": "pw",
            "TC_SSH_OPTS": "-o ProxyJump=bastion",
        })
        connection = resolve_env_connection(config)

        self.assertEqual(connection.ssh_opts, "-o ProxyJump=bastion")

    def test_json_text_uses_stable_pretty_format(self) -> None:
        self.assertEqual(json_text({"b": 1, "a": {"c": 2}}), '{\n  "a": {\n    "c": 2\n  },\n  "b": 1\n}')

    def test_write_json_file_adds_trailing_newline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_json_file(path, {"b": 1, "a": 2})
            self.assertEqual(path.read_text(), '{\n  "a": 2,\n  "b": 1\n}\n')


if __name__ == "__main__":
    unittest.main()
