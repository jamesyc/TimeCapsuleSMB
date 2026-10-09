"""transport.http against a real HTTP server on loopback."""
from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from timecapsulesmb.transport.http import USER_AGENT, HttpError, http_get, http_post_json


class _Server:
    """Answers each path with a canned (status, body); /drop closes without answering."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.requests: list[dict[str, object]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                server.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                if self.path == "/drop":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, body = routes[self.path]
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *_args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpGetTests(unittest.TestCase):
    def test_returns_the_body_and_sends_our_user_agent_and_headers(self) -> None:
        with _Server({"/v": (200, b"payload")}) as server:
            data = http_get(f"{server.url}/v", timeout=5, max_bytes=7, headers={"Accept": "application/json"})
        self.assertEqual(data, b"payload")
        headers = server.requests[0]["headers"]
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Accept"], "application/json")

    def test_a_body_one_byte_over_the_limit_fails(self) -> None:
        with _Server({"/v": (200, b"payload!")}) as server:
            with self.assertRaisesRegex(HttpError, "larger than 7 bytes"):
                http_get(f"{server.url}/v", timeout=5, max_bytes=7)

    def test_an_error_status_fails(self) -> None:
        with _Server({"/missing": (404, b"no")}) as server:
            with self.assertRaisesRegex(HttpError, "404"):
                http_get(f"{server.url}/missing", timeout=5, max_bytes=100)

    def test_a_refused_connection_fails(self) -> None:
        with self.assertRaises(HttpError):
            http_get(f"http://127.0.0.1:{_closed_port()}/v", timeout=5, max_bytes=100)

    def test_a_dropped_connection_fails(self) -> None:
        # urllib raises http.client.RemoteDisconnected, not an OSError.
        with _Server({}) as server:
            with self.assertRaises(HttpError):
                http_get(f"{server.url}/drop", timeout=5, max_bytes=100)

    def test_an_unusable_url_fails(self) -> None:
        with self.assertRaises(HttpError):
            http_get("not a url", timeout=5, max_bytes=100)


class HttpPostJsonTests(unittest.TestCase):
    def test_posts_the_body_with_json_and_caller_headers(self) -> None:
        body = json.dumps({"event": "x"}).encode("utf-8")
        with _Server({"/events": (202, b"")}) as server:
            status = http_post_json(
                f"{server.url}/events", body, timeout=5, headers={"Authorization": "Bearer t"},
            )
        self.assertEqual(status, 202)
        request = server.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["body"], body)
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["headers"]["Authorization"], "Bearer t")
        self.assertEqual(request["headers"]["User-Agent"], USER_AGENT)

    def test_an_error_status_is_returned_not_raised(self) -> None:
        with _Server({"/events": (503, b"busy"), "/bad": (400, b"no")}) as server:
            self.assertEqual(http_post_json(f"{server.url}/events", b"{}", timeout=5), 503)
            self.assertEqual(http_post_json(f"{server.url}/bad", b"{}", timeout=5), 400)

    def test_no_answer_raises(self) -> None:
        with self.assertRaises(HttpError):
            http_post_json(f"http://127.0.0.1:{_closed_port()}/events", b"{}", timeout=5)
        with _Server({}) as server:
            with self.assertRaises(HttpError):
                http_post_json(f"{server.url}/drop", b"{}", timeout=5)


if __name__ == "__main__":
    unittest.main()
