from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, TextIO

from timecapsulesmb.app.events import AppEvent, EventSink
from timecapsulesmb.app.service import run_api_request


def _sink_for_stream(stream: TextIO) -> EventSink:
    def emit(event: AppEvent) -> None:
        stream.write(event.to_json_line())
        stream.flush()

    return EventSink(emit)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one structured TimeCapsuleSMB app backend request.")
    parser.add_argument(
        "--pretty-error",
        action="store_true",
        help="Also write request parsing errors to stderr for local debugging.",
    )
    args = parser.parse_args(argv)
    sink = _sink_for_stream(sys.stdout)

    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        message = f"invalid JSON request: {exc.msg}"
        sink.error("api", message, debug={"pos": exc.pos})
        if args.pretty_error:
            print(message, file=sys.stderr)
        return 1
    if not isinstance(request, dict):
        sink.error("api", "request must be a JSON object")
        return 1
    return run_api_request(request, sink)


if __name__ == "__main__":
    raise SystemExit(main())

