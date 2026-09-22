from __future__ import annotations

import errno
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.checks.network import RouteSelection, select_route_to_address


class NetworkCheckTests(unittest.TestCase):
    def test_route_selection_preserves_client_link_local_scope(self) -> None:
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.getsockname.return_value = ("fe80::9", 43210, 0, 17)
        with (
            mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=sock),
            mock.patch("timecapsulesmb.core.net.socket.if_nametoindex", return_value=17),
            mock.patch("timecapsulesmb.core.net.socket.if_indextoname", return_value="en0"),
        ):
            result = select_route_to_address("fe80::2%en0")
        self.assertEqual(result, RouteSelection("available", source="fe80::9%en0"))
        sock.connect.assert_called_once_with(("fe80::2", 445, 0, 17))

    def test_route_selection_distinguishes_unavailable_and_unknown_errors(self) -> None:
        unavailable = mock.MagicMock()
        unavailable.__enter__.return_value = unavailable
        unavailable.connect.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
        with mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=unavailable):
            unavailable_result = select_route_to_address("fd00::2")

        unknown = mock.MagicMock()
        unknown.__enter__.return_value = unknown
        unknown.connect.side_effect = OSError(errno.EACCES, "Permission denied")
        with mock.patch("timecapsulesmb.checks.network.socket.socket", return_value=unknown):
            unknown_result = select_route_to_address("fd00::2")

        self.assertEqual(unavailable_result.state, "unavailable")
        self.assertEqual(unavailable_result.error_number, errno.ENETUNREACH)
        self.assertEqual(unknown_result.state, "unknown")
        self.assertEqual(unknown_result.error_number, errno.EACCES)

    def test_unscoped_link_local_never_uses_the_default_route(self) -> None:
        with mock.patch("timecapsulesmb.checks.network.socket.socket") as socket_mock:
            result = select_route_to_address("fe80::40")
        self.assertEqual(result.state, "unavailable")
        self.assertIn("scope", result.error or "")
        socket_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
