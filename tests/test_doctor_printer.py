"""Doctor's USB printer check (guide G6 as a release gate): Apple's printd
must keep advertising a plugged-in printer under our runtime."""
import ipaddress
from types import SimpleNamespace

from timecapsulesmb.checks.doctor_steps import BONJOUR_OFF_LINK_CODE, _add_usb_printer_results
from timecapsulesmb.checks.models import CheckResult
from timecapsulesmb.checks.network import classify_network_link
from timecapsulesmb.device.probe import UsbPrinterProbeResult, parse_prni_printers
from timecapsulesmb.discovery.bonjour import BonjourResolvedService, BonjourServiceInstance

NETBSD6_PRNI = """{
    printers=[
        {
            appSocketPort=9100
            generatedNumber=0
            make="Brother"
            model="HL-L2370DN series"
            name="Brother HL-L2370DN series"
            pluggedIn=true
            productID=160
            serialNumber="E78098M7N216821"
            vendorID=1273
        }
    ]
}
"""
NETBSD4_PRNI = """{
    printers=[
        {
            appSocketPort=9100
            make=Canon
            model=MP490 series
            name=Canon MP490 series
            pluggedIn=true
            productID=5948
            serialNumber=C0958C
            vendorID=1193
        }
    ]
}
"""


def test_parse_prni_quoted_and_bare_string_forms():
    quoted = parse_prni_printers(NETBSD6_PRNI)
    assert quoted == UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series", make="Brother", model="HL-L2370DN series")
    bare = parse_prni_printers(NETBSD4_PRNI)
    assert bare.present and bare.name == "Canon MP490 series" and bare.make == "Canon"


def test_parse_prni_unplugged_empty_and_garbage_mean_no_printer():
    assert parse_prni_printers(NETBSD6_PRNI.replace("pluggedIn=true", "pluggedIn=false")).present is False
    assert parse_prni_printers("{\n    printers=[]\n}\n").present is False
    assert parse_prni_printers("").present is False
    assert parse_prni_printers("printers=[ { name=Ghost pluggedIn=true } ]").present is False  # not the acp layout


def collect(printer, snapshot, error=None, host_label="jamess-airport-time-capsule"):
    results = []
    _add_usb_printer_results(printer, snapshot, error, host_label=host_label, add_result=results.append)
    return results


def snapshot(instances=(), resolved=()):
    return SimpleNamespace(instances=list(instances), resolved=list(resolved))


def test_no_printer_is_a_skip_not_a_pass():
    (result,) = collect(UsbPrinterProbeResult(present=False, name=None), None)
    assert result.status == "SKIP" and "no USB printer is plugged in" in result.message
    (result,) = collect(UsbPrinterProbeResult(present=False, name=None, error="acp -A prni timed out"), None)
    assert result.status == "SKIP" and "timed out" in result.message


def test_plugged_in_printer_advertised_by_printd_passes_on_name_or_device_host():
    printer = UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series")
    by_name = snapshot(resolved=[BonjourResolvedService("Brother HL-L2370DN series", "other-host.local", "_pdl-datastream._tcp.local.", port=9100)])
    (result,) = collect(printer, by_name)
    assert result.status == "PASS" and "_pdl-datastream" in result.message
    by_host = snapshot(resolved=[BonjourResolvedService("Printer @ capsule", "Jamess-AirPort-Time-Capsule.local", "_riousbprint._tcp.local.", port=9100)])
    (result,) = collect(printer, by_host)
    assert result.status == "PASS" and "_riousbprint" in result.message
    unresolved = snapshot(instances=[BonjourServiceInstance("_printer._tcp.local.", "Brother HL-L2370DN series", "x")])
    (result,) = collect(printer, unresolved)
    assert result.status == "PASS" and "unresolved" in result.message


def test_plugged_in_printer_with_no_record_fails_and_lists_what_was_seen():
    printer = UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series")
    (result,) = collect(printer, snapshot(resolved=[BonjourResolvedService("Office LaserJet", "laserjet.local", "_ipp._tcp.local.", port=631)]))
    assert result.status == "FAIL"
    assert "Office LaserJet (_ipp, laserjet.local)" in result.message
    assert "compare with stock firmware" in result.message
    (result,) = collect(printer, snapshot())
    assert result.status == "FAIL" and "no printer records seen" in result.message
    (result,) = collect(printer, None, error=CheckResult("FAIL", "Bonjour printer check failed: boom"))
    assert result.status == "FAIL" and "boom" in result.message


class FakeNetwork:
    """Stands in for DoctorNetworkProbe with a fixed link verdict."""

    def __init__(self, device: str, local: str) -> None:
        self.verdict = classify_network_link(
            [ipaddress.ip_network(device)], [ipaddress.ip_network(local)], source="device_ifconfig",
        )
        self.link_calls = 0
        self.skipped: list[str] = []

    def link(self):
        self.link_calls += 1
        return self.verdict

    def record_skip(self, check: str) -> None:
        self.skipped.append(check)


def collect_with_network(printer, found, network, error=None):
    results = []
    _add_usb_printer_results(
        printer, found, error, host_label="jamess-airport-time-capsule", add_result=results.append, network=network,
    )
    return results


def test_unadvertised_printer_is_skipped_when_this_computer_is_off_the_device_network():
    printer = UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series")
    network = FakeNetwork("192.168.1.0/24", "10.20.0.0/24")
    seen = snapshot(resolved=[BonjourResolvedService("Office LaserJet", "laserjet.local", "_ipp._tcp.local.", port=631)])

    (result,) = collect_with_network(printer, seen, network)

    assert result.status == "SKIP"
    assert result.details["code"] == BONJOUR_OFF_LINK_CODE
    assert result.message.startswith("USB printer 'Brother HL-L2370DN series' not checked; this computer (10.20.0.0/24)")
    assert "device's network (192.168.1.0/24)" in result.message
    assert network.skipped == ["usb_printer"]


def test_unadvertised_printer_still_fails_on_the_device_network_or_when_unknown():
    printer = UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series")
    for network in (FakeNetwork("192.168.1.0/24", "192.168.1.0/24"), FakeNetwork("192.168.1.0/24", "2001:db8::/64")):
        (result,) = collect_with_network(printer, snapshot(), network)
        assert result.status == "FAIL" and "no printer records seen" in result.message
        assert network.skipped == []


def test_advertised_printer_and_browse_errors_do_not_look_at_the_network():
    printer = UsbPrinterProbeResult(present=True, name="Brother HL-L2370DN series")
    network = FakeNetwork("192.168.1.0/24", "10.20.0.0/24")
    advertised = snapshot(resolved=[BonjourResolvedService("Brother HL-L2370DN series", "x.local", "_ipp._tcp.local.", port=631)])

    (passed,) = collect_with_network(printer, advertised, network)
    (failed,) = collect_with_network(printer, None, network, error=CheckResult("FAIL", "Bonjour printer check failed: boom"))

    assert passed.status == "PASS"
    assert failed.status == "FAIL" and "boom" in failed.message
    assert network.link_calls == 0
