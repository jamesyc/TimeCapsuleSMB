from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.services.callbacks import OperationCallbacks


class OperationCallbacksTests(unittest.TestCase):
    def test_forwards_all_operation_events_to_entrypoint_hooks(self) -> None:
        stages: list[str] = []
        logs: list[str] = []
        debug_fields: list[dict[str, object]] = []
        update_fields: list[dict[str, object]] = []
        measurements: list[tuple[str, dict[str, object]]] = []
        callbacks = OperationCallbacks(
            set_stage=stages.append,
            log=logs.append,
            add_debug_fields=lambda **fields: debug_fields.append(fields),
            update_fields=lambda **fields: update_fields.append(fields),
            record_execution_measurement=lambda kind, **fields: measurements.append((kind, fields)),
        )

        callbacks.stage("scan")
        callbacks.message("scanning")
        callbacks.debug(source="repair-xattrs", attempt=1)
        callbacks.update(scanned_paths=4)
        callbacks.measurement("runtime_verification", timeout_sec=200)

        self.assertEqual(stages, ["scan"])
        self.assertEqual(logs, ["scanning"])
        self.assertEqual(debug_fields, [{"source": "repair-xattrs", "attempt": 1}])
        self.assertEqual(update_fields, [{"scanned_paths": 4}])
        self.assertEqual(measurements, [("runtime_verification", {"timeout_sec": 200})])

    def test_missing_hooks_are_noops(self) -> None:
        callbacks = OperationCallbacks()

        callbacks.stage("scan")
        callbacks.message("scanning")
        callbacks.debug(source="repair-xattrs")
        callbacks.update(scanned_paths=4)
        callbacks.measurement("runtime_verification", timeout_sec=200)

    def test_callbacks_can_be_used_by_runtime_flows(self) -> None:
        logs: list[str] = []

        OperationCallbacks(log=logs.append).message("reboot requested")

        self.assertEqual(logs, ["reboot requested"])


if __name__ == "__main__":
    unittest.main()
