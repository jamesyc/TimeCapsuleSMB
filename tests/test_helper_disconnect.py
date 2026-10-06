"""The real helper process when the app goes away mid-operation.

The app quitting closes the helper's stdout. The helper must finish the stage
in progress and the stages after it that the app could not cancel, stop at
the next one it could, and still deliver its finished telemetry.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

REPO = Path(__file__).resolve().parents[1]


class TelemetryServer:
    def __init__(self, *, hold_started: float = 0.0) -> None:
        self.events: list[dict[str, object]] = []
        received = self.events

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if event.get("phase") == "started":
                    # A slow reply keeps the helper's background send running
                    # when its operation ends.
                    time.sleep(hold_started)
                received.append(event)
                self.send_response(202)
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "TelemetryServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/events"

    def finished(self) -> dict[str, object]:
        [event] = [event for event in self.events if event.get("phase") == "finished"]
        return event


def start_helper(tmp_path: Path, telemetry: TelemetryServer, stages: tuple[str, ...]) -> subprocess.Popen[bytes]:
    markers = tmp_path / "markers"
    markers.mkdir()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}",
        "TCAPSULE_STATE_DIR": str(tmp_path / "state"),
        "TCAPSULE_TELEMETRY_URL": telemetry.url,
        "TCAPSULE_CLIENT": "macos_gui",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.fixtures.disconnect_helper", str(markers), *stages],
        cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps({"operation": "deploy", "request_id": "r", "params": {}}).encode())
    process.stdin.close()
    return process


def read_until_stage(process: subprocess.Popen[bytes], stage: str) -> list[dict[str, object]]:
    assert process.stdout is not None
    events = []
    for line in process.stdout:
        events.append(json.loads(line))
        if events[-1].get("type") == "stage" and events[-1].get("stage") == stage:
            return events
    raise AssertionError(f"helper exited before {stage}: {process.stderr.read() if process.stderr else ''!r}")


# Each case: the stages, those that still run after the app quits during the
# first, and the stage the helper stops before.
CASES = {
    # The copy finishes and the old software is removed (the app offers no
    # Cancel there either); the helper stops before the Flash capacity check.
    "migration": (("migrate_xattrs_copy", "replace_software", "check_flash_capacity", "upload_payload"),
                  ("migrate_xattrs_copy", "replace_software"), "check_flash_capacity"),
    # The boot hook written to Flash is always flushed.
    "flash_write": (("enable_boot", "flush_boot_hook", "wait_for_reboot_down"),
                    ("enable_boot", "flush_boot_hook"), "wait_for_reboot_down"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_closed_pipe_runs_to_the_next_cancellable_stage_then_stops_with_telemetry(tmp_path: Path, case: str) -> None:
    stages, ran, stopped_before = CASES[case]
    with TelemetryServer(hold_started=0.5) as telemetry:
        process = start_helper(tmp_path, telemetry, stages)
        read_until_stage(process, stages[0])
        # The app quits: nobody reads the helper's output any more.
        assert process.stdout is not None
        process.stdout.close()
        markers = tmp_path / "markers"
        for stage in stages:
            (markers / f"{stage}.release").touch()
        assert process.wait(timeout=60) == 130
        stderr = process.stderr.read().decode() if process.stderr else ""

        finished = telemetry.finished()
        # The started event is delivered before the finished one, not cut off
        # by the helper exiting.
        phases = [event["phase"] for event in telemetry.events]

    assert [stage for stage in stages if (markers / f"{stage}.done").exists()] == list(ran)
    assert not (markers / f"{stopped_before}.entered").exists()
    assert "Traceback" not in stderr and "BrokenPipeError" not in stderr
    assert phases == ["started", "finished"]
    assert finished["operation"] == "deploy"
    assert finished["result"] == "cancelled"
    assert finished["error_code"] == "client_disconnected"
    assert finished["details"] == {"stopped_before_stage": stopped_before, "disconnected_during_stage": stages[0]}
