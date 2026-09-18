"""Doctor's USB printer check (guide G6 as a release gate): Apple's printd
must keep advertising a plugged-in printer under our runtime."""
from types import SimpleNamespace

from timecapsulesmb.checks.doctor_steps import _add_usb_printer_results
from timecapsulesmb.checks.models import CheckResult
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
