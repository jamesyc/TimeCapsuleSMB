"""The doctor command."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from timecapsulesmb.cli import doctor
from timecapsulesmb.device.storage import (
    MAST_PROBE_COMMAND,
    MaStProbeDiagnostics,
    mast_probe_debug_summary,
)

from tests.cli_support import CliTestCase, FakeCommandContext


class CliDoctorTests(CliTestCase):
    def test_doctor_returns_failure_when_checks_fatal(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("FAIL", "broken")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("doctor found one or more fatal problems", output.getvalue())
        self.assertIn("Doctor failures:", self._telemetry_client.emit.call_args_list[-1].kwargs["error"] if self._telemetry_client.emit.call_args_list else "")

    def test_doctor_failure_telemetry_includes_bonjour_candidate_context(self) -> None:
        output = io.StringIO()
        results = [
            doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched configured instance 'Home'"),
            doctor.CheckResult("INFO", "discovered _smb._tcp candidates: 'Kitchen' @ kitchen.local [10.0.1.99]"),
        ]
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=(results, True)):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Doctor context:", telemetry_error)
        self.assertIn("discovered _smb._tcp candidates: 'Kitchen' @ kitchen.local [10.0.1.99]", telemetry_error)

    def test_doctor_failure_telemetry_includes_debug_fields_from_checks(self) -> None:
        output = io.StringIO()
        results = [doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched configured instance 'Home'")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "ip_version": "V4Only"}
            kwargs["debug_fields"]["remote_rc_local_log_tail"] = "rc line 1\nrc line 2"
            kwargs["debug_fields"]["remote_discovery_log_tail"] = "mdns line"
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("bonjour_zeroconf={instance_count:0,ip_version:V4Only}", telemetry_error)
        self.assertIn("remote_rc_local_log_tail=rc line 1\nrc line 2", telemetry_error)
        self.assertIn("remote_discovery_log_tail=mdns line", telemetry_error)

    def test_doctor_telemetry_reports_nbns_subnet_outcome_on_a_passing_run(self) -> None:
        # Debug fields ship only inside a fatal run's error, so an off-subnet
        # NBNS SKIP would be invisible unless the outcome rides on the event.
        nbns_subnet = {
            "client_source": "192.168.24.102",
            "device_subnets": ["192.168.28.0/24"],
            "outcome": "off_subnet",
            "detail": None,
        }
        results = [doctor.CheckResult("SKIP", "NBNS query for 'backsy' got no answer; this computer (192.168.24.102) "
                                              "is outside the device's subnet 192.168.28.0/24")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["nbns_subnet"] = nbns_subnet
            return results, False

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(io.StringIO()):
                    rc = doctor.main([])

        self.assertEqual(rc, 0)
        finished = self._telemetry_client.emit.call_args_list[-1].kwargs
        self.assertEqual(finished["nbns_subnet"], nbns_subnet)
        self.assertIsNone(finished.get("error"))

    def test_doctor_telemetry_omits_nbns_subnet_when_the_device_was_not_probed(self) -> None:
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([], False)):
                with redirect_stdout(io.StringIO()):
                    rc = doctor.main([])

        self.assertEqual(rc, 0)
        self.assertNotIn("nbns_subnet", self._telemetry_client.emit.call_args_list[-1].kwargs)

    def test_doctor_failure_telemetry_includes_bounded_mast_probe_debug_fields(self) -> None:
        output = io.StringIO()
        raw_stdout = "a" * 10000
        results = [doctor.CheckResult("FAIL", "one or more managed share volumes are not mounted")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"].update(
                mast_probe_debug_summary(
                    MaStProbeDiagnostics(
                        command=MAST_PROBE_COMMAND,
                        returncode=0,
                        volumes=(),
                        stdout=raw_stdout,
                        stderr="",
                    )
                )
            )
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn(f"mast_probe_command={MAST_PROBE_COMMAND}", telemetry_error)
        self.assertIn("mast_probe_volume_count=0", telemetry_error)
        self.assertIn("mast_probe_stdout_chars=10000", telemetry_error)
        self.assertIn("<truncated", telemetry_error)
        self.assertNotIn(raw_stdout, telemetry_error)

    def test_doctor_failure_telemetry_reports_empty_zeroconf_without_dns_sd_diagnosis(self) -> None:
        output = io.StringIO()
        results = [doctor.CheckResult("FAIL", "no discovered _smb._tcp instance matched expected device instance 'Home'")]

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["debug_fields"]["bonjour_expected"] = {
                "instance_name": "Home",
                "host_label": "home",
                "target_ip": "10.0.0.2",
            }
            kwargs["debug_fields"]["bonjour_zeroconf"] = {"instance_count": 0, "service_event_count": 0, "ptr_record_count": 0}
            kwargs["debug_fields"]["bonjour_native_dns_sd"] = {
                "status": "ok",
                "timeout_sec": 6.0,
                "elapsed_sec": 6.125,
                "browses": [
                    {
                        "service_type": "_smb._tcp",
                        "events": [
                            {"service_type": "_smb._tcp", "action": "Add", "name": "Home"},
                        ],
                    }
                ],
            }
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Discovery context:", telemetry_error)
        self.assertIn(
            "INFO expected Bonjour identity: instance_name='Home' host_label='home' target_ip='10.0.0.2'",
            telemetry_error,
        )
        self.assertIn(
            "INFO Python zeroconf discovered 0 Bonjour instances during doctor; "
            "Bonjour registration path needs investigation",
            telemetry_error,
        )
        self.assertIn(
            "INFO Python zeroconf diagnostics: instance_count=0 service_event_count=0 ptr_record_count=0",
            telemetry_error,
        )
        self.assertIn("INFO native dns-sd diagnostics: status=ok timeout_sec=6.0 elapsed_sec=6.125", telemetry_error)
        self.assertIn("INFO native dns-sd observed _smb._tcp instances: 'Home'", telemetry_error)
        self.assertIn("INFO native dns-sd observed expected _smb._tcp instance: yes", telemetry_error)
        self.assertNotIn("INFO native dns-sd discovered expected _smb._tcp instance", telemetry_error)
        self.assertNotIn("likely doctor false negative", telemetry_error)

    def test_doctor_error_reports_native_dns_sd_as_telemetry_only(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_expected": {"instance_name": "Home"},
                "bonjour_zeroconf": {"instance_count": 0},
                "bonjour_native_dns_sd": {
                    "status": "ok",
                    "browses": [
                        {
                            "service_type": "_smb._tcp",
                            "events": [
                                {"service_type": "_smb._tcp", "action": "Add", "name": "Kitchen"},
                            ],
                        }
                    ],
                },
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("Discovery context:", error)
        self.assertIn("INFO expected Bonjour identity: instance_name='Home'", error)
        self.assertIn("INFO Python zeroconf discovered 0 Bonjour instances during doctor", error)
        self.assertIn("INFO native dns-sd diagnostics: status=ok", error)
        self.assertIn("INFO native dns-sd observed _smb._tcp instances: 'Kitchen'", error)
        self.assertIn("INFO native dns-sd observed expected _smb._tcp instance: no", error)
        self.assertNotIn("INFO native dns-sd discovered expected _smb._tcp instance", error)
        self.assertNotIn("likely doctor false negative", error)

    def test_doctor_error_preserves_expected_instance_from_failure_message_for_telemetry(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_zeroconf": {"instance_count": 0},
                "bonjour_native_dns_sd": {
                    "status": "ok",
                    "browses": [
                        {
                            "service_type": "_smb._tcp",
                            "events": [
                                {"service_type": "_smb._tcp", "action": "Add", "name": "Home"},
                            ],
                        }
                    ],
                },
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("INFO expected Bonjour identity: instance_name='Home'", error)
        self.assertIn("INFO native dns-sd observed expected _smb._tcp instance: yes", error)
        self.assertNotIn("likely doctor false negative", error)

    def test_doctor_error_reports_native_dns_sd_diagnostic_errors_as_telemetry(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_zeroconf": {"instance_count": 0},
                "bonjour_native_dns_sd_error": "RuntimeError: dns-sd broke",
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("INFO native dns-sd diagnostic error: RuntimeError: dns-sd broke", error)
        self.assertNotIn("likely doctor false negative", error)

    def test_doctor_error_reports_unicast_smb_when_bonjour_is_empty(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_zeroconf": {"instance_count": 0},
                "authenticated_smb_listing_attempts": [
                    {
                        "server": "home.local",
                        "outcome": "error",
                        "expected_share_found": False,
                    },
                    {
                        "server": "Home.IoT",
                        "outcome": "pass",
                        "expected_share_found": True,
                    },
                ],
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("Discovery context:", error)
        self.assertIn("INFO SMB works over unicast, but Bonjour discovered no matching _smb._tcp records", error)

    def test_doctor_error_ignores_legacy_mdns_transport_logs(self) -> None:
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]
        error = doctor.build_doctor_error(
            results,
            {
                "bonjour_zeroconf": {"instance_count": 0},
                "authenticated_smb_listing_attempts": [
                    {"outcome": "pass", "expected_share_found": True},
                ],
                "remote_discovery_log_tail": (
                    "mdns auto-ip active: link[0] iface=bridge0 mdns_ipv4=1 mdns_ipv6=1\n"
                    "mdns transport active: reason=startup status=degraded ipv4=off ipv6=bridge0\n"
                    "mdns counters: reason=ipv6_packet ipv4_rx=0 ipv6_rx=3\n"
                ),
            },
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("INFO SMB works over unicast, but Bonjour discovered no matching _smb._tcp records", error)
        self.assertNotIn("INFO mdns transport state:", error)
        self.assertNotIn("INFO mdns counters:", error)
        self.assertNotIn("INFO mdns IPv4 transport active:", error)

    def test_doctor_failure_telemetry_includes_derived_mdns_boot_context(self) -> None:
        output = io.StringIO()
        results = [
            doctor.CheckResult(
                "FAIL",
                "no discovered _smb._tcp instance matched expected device instance 'Home'",
            )
        ]

        def fake_run_doctor_checks(*_args, **kwargs):
            # smbd was not ready at first, so the early lines are in the RAM log.
            kwargs["debug_fields"]["remote_diskless_discovery_log_tail"] = "\n".join(
                [
                    "2026-09-16 07:35:21 registrant: plan validated mode=bridge desired=2 [if=9 _smb._tcp] [if=9 _adisk._tcp,_airport]",
                    "2026-09-16 07:35:22 registrant: name conflict if=9 _smb._tcp \"Home\"; retrying with backoff",
                ]
            )
            kwargs["debug_fields"]["remote_discovery_log_tail"] = "\n".join(
                [
                    "2026-09-16 07:35:30 registrant: mDNSResponder unreachable; registrations degraded until it answers (never started by us; reboot recovers)",
                    "2026-09-16 07:35:31 registrant: mDNSResponder accepted the connection but did not answer; exiting for relaunch",
                    # A retired v3.0 responder line must not be summarized.
                    "2026-09-16 07:35:32 mDNS takeover established after SIGTERM + 0ms using exclusive bind",
                ]
            )
            return results, True

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("mDNS boot context:", telemetry_error)
        boot_context = telemetry_error.split("mDNS boot context:", 1)[1].split("Debug context:", 1)[0]
        self.assertNotIn("mDNS takeover", boot_context)
        self.assertIn("INFO mdns registrant validated mode=bridge desired=2 [if=9 _smb._tcp] [if=9 _adisk._tcp,_airport]", telemetry_error)
        self.assertIn("WARN mdns registrant: name conflict if=9 _smb._tcp \"Home\"; retrying with backoff", telemetry_error)
        self.assertIn("WARN Apple mDNSResponder is unreachable; nothing respawns it, reboot the device", telemetry_error)
        self.assertIn("WARN mdns registrant exited because Apple mDNSResponder stopped answering; the manager relaunches it, a wedged daemon needs a reboot", telemetry_error)

    def test_doctor_failure_telemetry_reports_failures_without_a_preinspection_error(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        fake_result = doctor.CheckResult("FAIL", "SSH command works failed")
        with tempfile.NamedTemporaryFile() as env_file:
            env_path = Path(env_file.name)
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config(values, path=env_path)):
                with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], True)):
                    with redirect_stdout(output):
                        rc = doctor.main([])
        self.assertEqual(rc, 1)
        telemetry_error = self._telemetry_client.emit.call_args_list[-1].kwargs["error"]
        self.assertIn("Doctor failures:", telemetry_error)
        self.assertNotIn("preflight_error=doctor pre-inspection failed", telemetry_error)

    def test_doctor_passes_its_connection_and_probe_state_to_checks(self) -> None:
        output = io.StringIO()
        values = self.make_valid_env()
        command_context = FakeCommandContext()
        probe_state = self.make_probe_state(self.make_probe_result_netbsd6())
        command_context.probe_state = probe_state

        with tempfile.NamedTemporaryFile() as env_file:
            env_path = Path(env_file.name)
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config(values, path=env_path)):
                with mock.patch("timecapsulesmb.cli.doctor.CommandContext", return_value=command_context):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([], False)) as checks_mock:
                        with redirect_stdout(output):
                            rc = doctor.main([])

        self.assertEqual(rc, 0)
        checks_kwargs = checks_mock.call_args.kwargs
        self.assertIs(checks_kwargs["connection"], command_context.connection)
        self.assertIs(checks_kwargs["precomputed_probe_state"], probe_state)

    def test_doctor_streams_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("PASS", "streamed")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("\033[32mPASS\033[0m streamed", output.getvalue())

    def test_doctor_streams_fail_results_in_red_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("FAIL", "broken")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], True)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 1)
        self.assertIn("\033[31mFAIL\033[0m broken", output.getvalue())

    def test_doctor_streams_info_results_in_human_mode(self) -> None:
        output = io.StringIO()
        streamed_result = doctor.CheckResult("INFO", "advertised Bonjour instance: Home-Samba")

        def fake_run_doctor_checks(*_args, **kwargs):
            kwargs["on_result"](streamed_result)
            return ([streamed_result], False)

        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", side_effect=fake_run_doctor_checks):
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertIn("INFO advertised Bonjour instance: Home-Samba", output.getvalue())

    def test_doctor_json_outputs_structured_results(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                with redirect_stdout(output):
                    rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["fatal"], False)
        self.assertEqual(payload["results"][0]["status"], "PASS")

    def test_doctor_does_not_pass_legacy_bonjour_timeout_to_checks(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)) as checks_mock:
                with redirect_stdout(output):
                    rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertNotIn("bonjour_timeout", checks_mock.call_args.kwargs)
        self.assertIs(checks_mock.call_args.kwargs["startup_grace"], True)

    def test_doctor_no_startup_grace_flag_disables_grace_transform(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
            with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)) as checks_mock:
                with redirect_stdout(output):
                    rc = doctor.main(["--no-startup-grace"])
        self.assertEqual(rc, 0)
        self.assertIs(checks_mock.call_args.kwargs["startup_grace"], False)

    def test_doctor_ensures_install_id_before_telemetry(self) -> None:
        output = io.StringIO()
        fake_result = doctor.CheckResult("PASS", "ok")
        with mock.patch("timecapsulesmb.cli.doctor.ensure_install_id") as ensure_mock:
            with mock.patch("timecapsulesmb.cli.doctor.load_env_config", return_value=self.make_app_config({})):
                with mock.patch("timecapsulesmb.cli.doctor.CommandContext", return_value=FakeCommandContext()):
                    with mock.patch("timecapsulesmb.cli.doctor.run_doctor_checks", return_value=([fake_result], False)):
                        with redirect_stdout(output):
                            rc = doctor.main(["--json"])
        self.assertEqual(rc, 0)
        ensure_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
