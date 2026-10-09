"""HTTP requests from this computer: Apple's firmware, the version check, telemetry.

They run in this process with urllib. That leaves Network Extension state in
it on some Macs, which is safe only because nothing here forks (see
core.process). SSL_CERT_FILE, which the app sets to certifi's bundle, and the
system's proxy settings both reach urllib.
"""
from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Mapping

from timecapsulesmb.core.release import CLI_VERSION


USER_AGENT = f"TimeCapsuleSMB/{CLI_VERSION}"
# What urlopen raises: URLError and timeouts are OSErrors; a dropped
# connection is an HTTPException; an unusable URL is a ValueError.
_REQUEST_ERRORS = (OSError, http.client.HTTPException, ValueError)


class HttpError(OSError):
    """A request failed: no connection, a timeout, an error status, or too large a response."""


def http_get(url: str, *, timeout: float, max_bytes: int, headers: Mapping[str, str] | None = None) -> bytes:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except _REQUEST_ERRORS as exc:
        raise HttpError(f"{url}: {exc}") from exc
    if len(data) > max_bytes:
        raise HttpError(f"{url}: the response is larger than {max_bytes} bytes")
    return data


def http_post_json(url: str, body: bytes, *, timeout: float, headers: Mapping[str, str] | None = None) -> int:
    """POST a JSON body; return the HTTP status, an error status included."""
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except _REQUEST_ERRORS as exc:
        raise HttpError(f"{url}: {exc}") from exc
