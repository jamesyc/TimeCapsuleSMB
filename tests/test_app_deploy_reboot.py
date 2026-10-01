from __future__ import annotations

import unittest
from unittest import mock

from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.services import reboot as reboot_service
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.transport.errors import SshCommandTimeout, SshError
from timecapsulesmb.transport.ssh import SshConnection


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.sink = EventSink(lambda event: self.events.append(event.to_jsonable()))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.events if event["type"] == event_type]


class DeployRebootStrategyTests(unittest.TestCase):
    def make_context(self) -> tuple[CollectingSink, AppOperationContext, SshConnection]:
        collector = CollectingSink()
        context = AppOperationContext("deploy", collector.sink)
        connection = SshConnection("root@10.0.0.2", "pw", "-o test")
        return collector, context, connection

    def test_reboot_is_one_native_acp_request_over_ssh(self) -> None:
        collector, context, connection = self.make_context()

        request = mock.Mock()
        # The network ACP client is not a reboot route any more.
        with mock.patch("timecapsulesmb.integrations.acp.set_property_int", side_effect=AssertionError("network ACP used")):
            reboot_service.request_reboot(
                connection,
                callbacks=context.to_operation_callbacks(),
                request_reboot_func=request,
            )

        request.assert_called_once_with(connection)
        self.assertEqual(context.diagnostics.debug_fields["reboot_request_strategy"], "native_acp")
        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_succeeded"], True)
        self.assertEqual(collector.events_of_type("stage")[0]["stage"], "reboot")
        self.assertEqual([event["message"] for event in collector.events_of_type("log")], ["ACP reboot requested."])

    def test_failed_acp_request_has_no_fallback_route(self) -> None:
        collector, context, connection = self.make_context()

        request = mock.Mock(side_effect=SshError("ssh command failed with rc=1"))
        reboot_service.request_reboot(
            connection,
            callbacks=context.to_operation_callbacks(),
            request_reboot_func=request,
        )

        request.assert_called_once_with(connection)
        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(
            [event["message"] for event in collector.events_of_type("log")],
            ["ACP reboot request failed; checking whether the device is rebooting anyway..."],
        )

    def test_ssh_timeout_is_logged_when_request_error_is_not_required(self) -> None:
        collector, context, connection = self.make_context()

        reboot_service.request_reboot(
            connection,
            callbacks=context.to_operation_callbacks(),
            request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("timeout")),
            raise_on_request_error=False,
        )

        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_succeeded"], False)
        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_timed_out"], True)
        self.assertEqual(
            collector.events_of_type("log")[0]["message"],
            "ACP reboot request timed out; checking whether the device is rebooting...",
        )

    def test_ssh_timeout_can_be_promoted_to_operation_error(self) -> None:
        _collector, context, connection = self.make_context()

        with self.assertRaisesRegex(RebootFlowError, "SSH reboot request timed out"):
            reboot_service.request_reboot(
                connection,
                    callbacks=context.to_operation_callbacks(),
                request_reboot_func=mock.Mock(side_effect=SshCommandTimeout("timeout")),
                raise_on_request_error=True,
            )

        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_timed_out"], True)

    def test_ssh_error_can_be_promoted_to_operation_error(self) -> None:
        _collector, context, connection = self.make_context()

        with self.assertRaisesRegex(RebootFlowError, "SSH reboot request failed"):
            reboot_service.request_reboot(
                connection,
                    callbacks=context.to_operation_callbacks(),
                request_reboot_func=mock.Mock(side_effect=SshError("rc=255")),
                raise_on_request_error=True,
            )

        self.assertEqual(context.diagnostics.debug_fields["ssh_reboot_succeeded"], False)
        self.assertIn("rc=255", context.diagnostics.debug_fields["ssh_reboot_error"])


if __name__ == "__main__":
    unittest.main()
