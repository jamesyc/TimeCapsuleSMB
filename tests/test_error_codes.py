from __future__ import annotations

import json
import unittest

from timecapsulesmb.core.error_codes import CodedError, ErrorCode
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


if __name__ == "__main__":
    unittest.main()
