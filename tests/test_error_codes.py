from __future__ import annotations

import json
import unittest

from timecapsulesmb.core.error_codes import CodedError, ErrorCode, coded_error_in, exception_chain
from timecapsulesmb.transport.errors import (
    SshClientError,
    SshClientCrashedError,
    SshError,
    SshLocalNetworkFilteredError,
    transport_error_code,
)
from timecapsulesmb.device.errors import DeviceError, DiskPayloadMissingError
from timecapsulesmb.services.app import AppOperationError


class ErrorCodeTests(unittest.TestCase):
    def test_a_code_reads_as_its_value_wherever_it_is_written(self) -> None:
        # The app, telemetry and the localization keys built from it all
        # expect the bare value, never "ErrorCode.AUTH_FAILED".
        code = ErrorCode.AUTH_FAILED
        self.assertEqual(str(code), "auth_failed")
        self.assertEqual(f"{code}", "auth_failed")
        self.assertEqual(f"deploy.{code}", "deploy.auth_failed")
        self.assertEqual(json.dumps({"code": code}), '{"code": "auth_failed"}')
        self.assertEqual(code, "auth_failed")


class CodedErrorTests(unittest.TestCase):
    def test_a_code_passed_on_as_a_string_becomes_its_member(self) -> None:
        error = AppOperationError("failed", code="auth_failed")
        self.assertIs(error.code, ErrorCode.AUTH_FAILED)

    def test_a_misspelled_or_empty_code_fails_where_it_is_raised(self) -> None:
        for code in ("auth_faild", ""):
            with self.subTest(code=code), self.assertRaises(ValueError):
                AppOperationError("failed", code=code)

    def test_an_error_that_names_no_cause_has_no_code(self) -> None:
        # Its reporter picks the generic code that fits (remote_error for a
        # device step, operation_failed otherwise).
        self.assertIsNone(DeviceError("failed").code)
        self.assertIsNone(CodedError("failed").code)

    def test_a_subclass_code_holds_for_every_instance(self) -> None:
        self.assertIs(DiskPayloadMissingError("gone").code, ErrorCode.PAYLOAD_MISSING_FROM_DISK)

    def test_an_app_error_without_a_code_is_operation_failed(self) -> None:
        self.assertIs(AppOperationError("failed").code, ErrorCode.OPERATION_FAILED)

    def test_its_text_is_the_message_then_the_facts_on_their_own_line(self) -> None:
        error = DeviceError("Not enough free space on /mnt/Flash.", code="insufficient_flash_space",
                            detail="Available 10 bytes, need 20 bytes.")
        self.assertEqual(error.message, "Not enough free space on /mnt/Flash.")
        self.assertEqual(error.detail, "Available 10 bytes, need 20 bytes.")
        self.assertEqual(str(error), "Not enough free space on /mnt/Flash.\nAvailable 10 bytes, need 20 bytes.")

    def test_without_facts_its_text_is_the_message(self) -> None:
        for detail in (None, ""):
            with self.subTest(detail=detail):
                self.assertEqual(str(DeviceError("The disk did not respond.", detail=detail)), "The disk did not respond.")


class CodedErrorInTests(unittest.TestCase):
    def raised(self, outer: BaseException, inner: BaseException, *, explicit: bool) -> BaseException:
        try:
            try:
                raise inner
            except BaseException as exc:
                if explicit:
                    raise outer from exc
                raise outer
        except BaseException as caught:
            return caught

    def test_it_finds_the_coded_error_a_command_turned_into_its_exit(self) -> None:
        inner = DeviceError("gone", code="device_unreachable")
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                self.assertIs(coded_error_in(self.raised(SystemExit("gone"), inner, explicit=explicit)), inner)

    def test_the_outermost_code_wins(self) -> None:
        outer = AppOperationError("refused", code="auth_failed")
        self.assertIs(coded_error_in(self.raised(outer, DeviceError("x", code="remote_error"), explicit=True)), outer)

    def test_an_error_that_names_no_cause_is_passed_over(self) -> None:
        inner = DeviceError("gone", code="device_unreachable")
        outer = self.raised(DeviceError("no code"), inner, explicit=True)
        self.assertIs(coded_error_in(outer), inner)
        self.assertIsNone(coded_error_in(DeviceError("no code")))
        self.assertIsNone(coded_error_in(RuntimeError("boom")))


    def test_raise_from_none_ends_the_chain(self) -> None:
        # The code says the earlier error is unrelated: its steps do not apply.
        try:
            try:
                raise DeviceError("gone", code="device_unreachable")
            except DeviceError:
                raise SystemExit("Deployment cancelled.") from None
        except SystemExit as exc:
            caught = exc
        self.assertEqual(list(exception_chain(caught)), [caught])
        self.assertIsNone(coded_error_in(caught))


class TransportErrorCodeTests(unittest.TestCase):
    def test_a_connection_this_computer_dropped_is_not_the_device_s_fault(self) -> None:
        self.assertIs(transport_error_code(SshClientError("ssh crashed")), ErrorCode.SSH_CLIENT_FAILED)
        self.assertIs(transport_error_code(SshLocalNetworkFilteredError("filtered")), ErrorCode.LOCAL_NETWORK_FILTERED)
        self.assertIs(transport_error_code(SshError("reset by peer")), ErrorCode.REMOTE_ERROR)

    def test_a_crashed_ssh_client_is_reported_like_a_filtered_connection(self) -> None:
        # Its one seen cause is a Network Extension, whose guidance names the same apps.
        self.assertIs(transport_error_code(SshClientCrashedError(11)), ErrorCode.LOCAL_NETWORK_FILTERED)


if __name__ == "__main__":
    unittest.main()
