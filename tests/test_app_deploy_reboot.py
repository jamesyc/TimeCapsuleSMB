from __future__ import annotations

import unittest

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.integrations.acp import ACPConnectionError
from timecapsulesmb.services.reboot import RebootFlowError, reboot_device
from tests.reboot_support import FakeAcpDevice


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


class DeployRebootStrategyTests(unittest.TestCase):
    def make_context(self) -> tuple[CollectingSink, AppOperationContext]:
        collector = CollectingSink()
        return collector, AppOperationContext("deploy", collector.sink)

    def test_reboot_is_one_network_acp_request(self) -> None:
        collector, context = self.make_context()
        device = FakeAcpDevice()

        with device.patched():
            reboot_device("root@10.0.0.2", "pw", wait=False, callbacks=context.to_operation_callbacks())

        self.assertEqual(device.calls, ["request"])
        self.assertEqual(context.diagnostics.debug_fields["reboot_request_strategy"], "network_acp")
        self.assertEqual(context.diagnostics.debug_fields["acp_reboot_succeeded"], True)
        self.assertEqual([event["stage"] for event in collector.events_of_type("stage")], ["reboot"])
        self.assertEqual([event["message"] for event in collector.events_of_type("log")], ["ACP reboot requested."])

    def test_failed_request_is_observed_once_and_never_retried(self) -> None:
        collector, context = self.make_context()
        device = FakeAcpDevice(request_error=ACPConnectionError("ACP receive failed: timed out"))

        with device.patched():
            reboot_device("root@10.0.0.2", "pw", wait=True, callbacks=context.to_operation_callbacks())

        self.assertEqual(device.calls.count("request"), 1)
        self.assertEqual(context.diagnostics.debug_fields["acp_reboot_succeeded"], False)
        self.assertIn("timed out", context.diagnostics.debug_fields["acp_reboot_error"])
        messages = [event["message"] for event in collector.events_of_type("log")]
        self.assertIn("ACP reboot request failed; checking whether the device is restarting anyway...", messages)
        self.assertEqual(messages[-1], "Device is back online.")
        self.assertEqual(
            [event["stage"] for event in collector.events_of_type("stage")],
            ["reboot", "wait_for_reboot_down", "wait_for_reboot_up"],
        )

    def test_no_wait_request_failure_is_an_operation_error(self) -> None:
        _collector, context = self.make_context()
        device = FakeAcpDevice(request_error=ACPConnectionError("refused"))

        with device.patched():
            with self.assertRaisesRegex(RebootFlowError, "ACP reboot request failed: refused") as raised:
                reboot_device("root@10.0.0.2", "pw", wait=False, callbacks=context.to_operation_callbacks())

        self.assertEqual(raised.exception.code, "remote_error")
        self.assertEqual(context.diagnostics.debug_fields["acp_reboot_succeeded"], False)


if __name__ == "__main__":
    unittest.main()
