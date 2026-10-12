"""What an operation learned about its device, kept once for the app and the CLI."""
from __future__ import annotations

import unittest
from unittest import mock

from tests.cli_support import app_config
from timecapsulesmb.app.context import AppOperationContext
from timecapsulesmb.app.events import EventSink
from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.device.compat import DeviceCompatibility
from timecapsulesmb.device.probe import AirportAcpReading, ProbedDeviceState, ProbeResult, SshAccessStatus
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.context import OperationContext
from timecapsulesmb.services.runtime import DeviceAccessError, ManagedTargetState
from timecapsulesmb.transport.ssh import SshConnection

MAC = "02:00:00:00:00:0a"
READING = AirportAcpReading(password_matches=True, airport_mac=MAC)


def probed_state() -> ProbedDeviceState:
    probe = ProbeResult(ssh_status=SshAccessStatus.OPEN_AUTHENTICATED, error=None, os_name="NetBSD", os_release="6.0",
                        arch="earmv4", elf_endianness="little", airport_model="TimeCapsule8,119", airport_syap="119")
    compatibility = DeviceCompatibility(os_name="NetBSD", os_release="6.0", arch="earmv4", elf_endianness="little",
                                        payload_family="netbsd6_samba4", device_generation="gen5", supported=True,
                                        reason_code="supported_netbsd6")
    return ProbedDeviceState(probe_result=probe, compatibility=compatibility)


def contexts():
    """The shared context as each entrypoint holds it."""
    app = AppOperationContext("deploy", EventSink(mock.Mock()))
    cli = CommandContext(mock.Mock(), "deploy", "deploy_started", "deploy_finished")
    return {"shared": OperationContext("deploy"), "app": app.diagnostics, "cli": cli.operation_context}


class FollowSavedDeviceTests(unittest.TestCase):
    def follow(self, values, result):
        context = OperationContext("deploy", config=app_config(values))
        with mock.patch("timecapsulesmb.services.runtime.follow_saved_device", return_value=result) as follow:
            context.follow_saved_device(OperationCallbacks())
        return context, follow

    def test_the_config_and_its_read_move_together(self) -> None:
        moved = app_config({"TC_HOST": "root@10.0.0.9", "TC_PASSWORD": "pw"})
        context, follow = self.follow({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw", "TC_AIRPORT_MAC": MAC},
                                      (moved, ("root@10.0.0.9", READING)))
        self.assertEqual(follow.call_args.args[1], MAC)
        self.assertIs(context.config, moved)
        # The read belongs to the address it was taken at, and to no other.
        self.assertIs(context.reading_at(SshConnection("root@10.0.0.9", "pw", "")), READING)
        self.assertIsNone(context.reading_at(SshConnection("root@10.0.0.2", "pw", "")))
        self.assertIsNone(context.reading_at(None))

    def test_without_a_saved_mac_nothing_is_looked_for(self) -> None:
        context, follow = self.follow({"TC_HOST": "root@10.0.0.2", "TC_PASSWORD": "pw"}, None)
        follow.assert_not_called()
        self.assertIsNone(context.device_reading)

    def test_a_malformed_mac_or_another_airport_stops_the_operation(self) -> None:
        with self.assertRaises(ValueError):
            self.follow({"TC_HOST": "root@10.0.0.2", "TC_AIRPORT_MAC": "not-a-mac"}, None)
        context = OperationContext("deploy", config=app_config({"TC_HOST": "root@10.0.0.2", "TC_AIRPORT_MAC": MAC}))
        error = DeviceAccessError("A different AirPort answers at 10.0.0.2", code="device_identity_mismatch")
        with mock.patch("timecapsulesmb.services.runtime.follow_saved_device", side_effect=error), \
                self.assertRaises(DeviceAccessError):
            context.follow_saved_device(OperationCallbacks())


class DeviceFactsTests(unittest.TestCase):
    def test_a_new_address_retargets_the_connection_and_reaches_the_debug_fields(self) -> None:
        for name, context in contexts().items():
            with self.subTest(context=name):
                context.connection = SshConnection("root@10.0.0.2", "pw", "-o x")
                context.update_fields(current_host="root@10.0.0.9", airport_mac=MAC, airport_instance_name="Office",
                                      device_model=None)
                self.assertEqual(context.connection, SshConnection("root@10.0.0.9", "pw", "-o x"))
                self.assertEqual(context.finish_fields["current_host"], "root@10.0.0.9")
                # Every error event carries the debug fields, so the app learns them from failures too.
                self.assertEqual({key: context.debug_fields[key] for key in ("current_host", "airport_mac", "airport_instance_name")},
                                 {"current_host": "root@10.0.0.9", "airport_mac": MAC, "airport_instance_name": "Office"})
                self.assertNotIn("device_model", context.finish_fields)

    def test_the_device_names_reach_the_app_but_never_telemetry(self) -> None:
        # The MAC is a telemetry field; the names the user chose are not, nor
        # are they in the debug text a failure sends.
        context = OperationContext("deploy")
        context.update_fields(airport_mac=MAC, airport_instance_name="Gregory's Capsule", airport_host_label="gregorys-capsule",
                              airport_host_label_source="syNm")
        context.set_error("deploy failed")
        self.assertEqual(context.finish_fields, {"airport_mac": MAC})
        self.assertEqual({key: context.debug_fields[key] for key in
                          ("airport_instance_name", "airport_host_label", "airport_host_label_source")},
                         {"airport_instance_name": "Gregory's Capsule", "airport_host_label": "gregorys-capsule",
                          "airport_host_label_source": "syNm"})
        error = context.build_error()
        self.assertIn("deploy failed", error)
        self.assertNotIn("Gregory", error)
        self.assertNotIn("gregorys-capsule", error)

    def test_other_fields_leave_the_connection_and_debug_fields_alone(self) -> None:
        context = OperationContext("deploy")
        context.connection = SshConnection("root@10.0.0.2", "pw", "")
        context.update_fields(stage_count=3, current_host=None)
        self.assertEqual(context.connection.host, "root@10.0.0.2")
        self.assertEqual(context.debug_fields, {})

    def test_a_probed_device_records_its_model_os_and_family(self) -> None:
        for name, context in contexts().items():
            with self.subTest(context=name):
                state = probed_state()
                target = ManagedTargetState(connection=SshConnection("root@10.0.0.2", "pw", ""), probe_state=state)
                self.assertIs(context.apply_managed_target(target), target)
                self.assertIs(context.probe_state, state)
                self.assertEqual(context.connection, target.connection)
                self.assertEqual(
                    {key: context.finish_fields[key] for key in ("device_model", "device_syap", "device_family")},
                    {"device_model": "TimeCapsule8,119", "device_syap": "119", "device_family": "netbsd6_samba4"},
                )
                self.assertIn("NetBSD", context.finish_fields["device_os_version"])
                self.assertEqual(context.known_airport_display_name(), "Time Capsule 5th generation")

    def test_an_unprobed_device_has_no_display_name(self) -> None:
        self.assertIsNone(OperationContext("deploy").known_airport_display_name())
