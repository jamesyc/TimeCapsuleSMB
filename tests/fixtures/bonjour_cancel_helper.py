"""Run the actual app helper with owned, deliberately slow fake native children."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from tests.fixtures.bonjour import install_native, records
from timecapsulesmb.app import helper, service
from timecapsulesmb.discovery import native_dns_sd
from timecapsulesmb.telemetry import TelemetryClient


def main():
    patch = pytest.MonkeyPatch()
    observations = [dict(records()[0], name=f" Device {i}\u00a0 ", hostname=f"device-{i}.local") for i in range(4)]
    install_native(patch, Path(sys.argv[1]), observations, address_delay=20, ignore_terminate=True)
    launch = native_dns_sd._ProcessOwner.launch
    lock = threading.Lock()
    count = 0
    def report(owner, args):
        nonlocal count
        proc = launch(owner, args)
        with lock:
            print(f"CHILD {proc.pid}", file=sys.stderr, flush=True)
            count += 1
            ready = count == 9
        if ready:
            print(json.dumps(dict(schema_version=1, request_id="cancel-live", type="log", operation="discover", message="native children ready")), flush=True)
        return proc
    patch.setattr(native_dns_sd._ProcessOwner, "launch", report)
    patch.setattr(service, "ensure_install_id", lambda: None)
    patch.setattr(TelemetryClient, "from_config", lambda *a, **k: TelemetryClient(endpoint="https://invalid.invalid", token=None, context=None, enabled=False))
    return helper.main([])


if __name__ == "__main__":
    raise SystemExit(main())
