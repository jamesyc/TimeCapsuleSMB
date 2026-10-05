"""Run the actual app helper with a stand-in deploy, for the lost-app test.

argv: MARKERS STAGE... The deploy runs the named stages in order. Each stage
records "<stage>.entered", waits for "<stage>.release" in MARKERS, writes a
log event and records "<stage>.done". The test closes the helper's stdout
during the first stage.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from timecapsulesmb.app import helper, service
from timecapsulesmb.services.app import OperationResult


def main() -> int:
    markers = Path(sys.argv[1])
    stages = sys.argv[2:]

    def stage(context, name: str) -> None:
        context.stage(name)
        (markers / f"{name}.entered").touch()
        deadline = time.monotonic() + 30
        while not (markers / f"{name}.release").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        # The write a real stage makes when its work ends.
        context.log(f"{name} finished")
        (markers / f"{name}.done").touch()

    def deploy(_params, context):
        for name in stages:
            stage(context, name)
        return OperationResult(True, {"summary": "Deployment completed."})

    service.OPERATIONS["deploy"] = deploy
    return helper.main([])


if __name__ == "__main__":
    raise SystemExit(main())
