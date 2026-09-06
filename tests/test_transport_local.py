from __future__ import annotations

import sys
import unittest
import errno
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.transport.local import run_local_capture, scoped_tcp_connect_errors


class LocalTransportTests(unittest.TestCase):
    def test_scoped_connections_share_a_budget_and_cleanup_every_socket(self) -> None:
        for mode in ("success", "timeout", "gone"):
            with self.subTest(mode=mode):
                clock = [100.0]
                pending = {}
                selector = mock.MagicMock()
                selector.__enter__.return_value = selector
                selector.get_map.side_effect = lambda: pending
                selector.register.side_effect = lambda sock, _event, host: pending.setdefault(sock, SimpleNamespace(fileobj=sock, data=host))
                selector.unregister.side_effect = pending.pop
                first, second = mock.Mock(), mock.Mock()
                first.connect_ex.return_value = errno.EINPROGRESS
                second.connect_ex.return_value = errno.EINPROGRESS
                second.getsockopt.return_value = 0
                if mode == "gone":
                    second.getsockopt.side_effect = OSError("interface disappeared")

                def select(timeout):
                    self.assertLessEqual(timeout, 2.0)
                    if mode != "timeout" and second in pending:
                        clock[0] += 0.25
                        return [(pending[second], 2)]
                    clock[0] += timeout
                    return []

                selector.select.side_effect = select
                with (
                    mock.patch("timecapsulesmb.transport.local.socket.socket", side_effect=[first, second]),
                    mock.patch("timecapsulesmb.transport.local.selectors.DefaultSelector", return_value=selector),
                    mock.patch("timecapsulesmb.transport.local.time.monotonic", side_effect=lambda: clock[0]),
                ):
                    result = scoped_tcp_connect_errors(["fe80::40%17", "fe80::40%18"], 445)
                first.connect_ex.assert_called_once_with(("fe80::40", 445, 0, 17))
                second.connect_ex.assert_called_once_with(("fe80::40", 445, 0, 18))
                self.assertEqual(result["fe80::40%17"], "connection timed out")
                self.assertEqual(result["fe80::40%18"], {"success": None, "timeout": "connection timed out", "gone": "interface disappeared"}[mode])
                self.assertLessEqual(clock[0], 102.0)
                first.close.assert_called_once()
                second.close.assert_called_once()

    def test_scoped_connections_handle_immediate_results_and_bad_scopes(self) -> None:
        for code, succeeds in ((0, True), (errno.ECONNREFUSED, False)):
            with self.subTest(code=code):
                sock = mock.Mock()
                sock.connect_ex.return_value = code
                with mock.patch("timecapsulesmb.transport.local.socket.socket", return_value=sock):
                    result = scoped_tcp_connect_errors(["fe80::40%17", "fe80::40%17", "fe80::40%0"], 445)
                self.assertEqual(result["fe80::40%17"] is None, succeeds)
                self.assertIn("no usable", result["fe80::40%0"])
                sock.connect_ex.assert_called_once()
                sock.close.assert_called_once()

    def test_run_local_capture_returns_stdout(self) -> None:
        proc = run_local_capture(["/bin/sh", "-c", "printf 'ok'"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
