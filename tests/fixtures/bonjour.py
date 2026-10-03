"""Wire observations shared by provider, helper and Swift contract tests."""
from __future__ import annotations

import json
import sys
import time

from timecapsulesmb.discovery import bonjour, native_dns_sd, zeroconf_backend
from timecapsulesmb.discovery.models import SERVICE_TYPES


def records():
    return [dict(name="Office", hostname="office.local", service_type=stype, port=port,
                 ipv4=["192.0.2.10"], ipv6=["fd00::10"], interface_index=14,
                 properties={"waMA": "00:11:22:33:44:55,syAP=116,syVs=7.9.1"} if stype.startswith("_airport") else {})
            for stype, port in zip(SERVICE_TYPES, (5009, 445, 9, 548, 0))]


def info(record):
    from zeroconf import ServiceInfo
    return ServiceInfo(record["service_type"], f'{record["name"]}.{record["service_type"]}',
                       server=record["hostname"] + ".", port=record["port"],
                       interface_index=record.get("interface_index"),
                       properties={k.encode(): v.encode() for k, v in record.get("properties", {}).items()},
                       parsed_addresses=[*record.get("ipv4", []), *record.get("ipv6", [])])


def native_txt_output(properties):
    """Apple dns-sd's ShowTXTRecord wire display, including both escape rounds."""
    def field(text):
        return "".join("\\" * 4 if char == "\\" else "\\" * 2 + f"x{ord(char):02X}" if ord(char) < 32
                       else "\\" + char if char in " &;`'\"|*?~<>^()[]{}$" else char for char in text)
    return " ".join(field(key + "=" + value) for key, value in properties.items())


def install_zeroconf(monkeypatch, observations, *, fail_family=None, family_observations=None, delays=None,
                     address_delays=None):
    from zeroconf import IPVersion, ServiceStateChange
    closed = []
    started = time.monotonic()
    class FakeZeroconf:
        def __init__(self, **kwargs):
            self.family = "ipv6" if kwargs.get("ip_version") == IPVersion.V6Only else "ipv4"
            assert kwargs.get("ip_version") != IPVersion.All
            self.observations = (family_observations or {}).get(self.family, observations)
            self.ready_at = time.monotonic() + (delays or {}).get(self.family, 0)
            if self.family == fail_family:
                raise OSError("family unavailable")
        def get_service_info(self, stype, fullname, timeout_ms, **kwargs):
            remaining = self.ready_at - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, timeout_ms / 1000))
                if time.monotonic() < self.ready_at:
                    return None
            for r in self.observations:
                if r["service_type"] == stype and f'{r["name"]}.{stype}' == fullname:
                    return info({**r, **{f: [] for f, delay in (address_delays or {}).items()
                                         if time.monotonic() < started + delay}})
            return None
        def add_listener(self, *args): pass
        def remove_listener(self, *args): pass
        def close(self): closed.append(self.family)
    class FakeBrowser:
        def __init__(self, zc, stype, *, handlers, **kwargs):
            for r in zc.observations:
                if r["service_type"] == stype:
                    handlers[0](zeroconf=zc, service_type=stype, name=f'{r["name"]}.{stype}', state_change=ServiceStateChange.Added)
        def cancel(self): pass
    def resolver(family):
        class Resolver:
            def __init__(self, server):
                self.server = server
                self.interface_index = None
                self.addresses = []
            def request(self, zc, timeout, **kwargs):
                remaining = started + (address_delays or {}).get(family, 0) - time.monotonic()
                if remaining > 0:
                    time.sleep(min(remaining, timeout / 1000))
                    if time.monotonic() < started + (address_delays or {})[family]:
                        return False
                self.addresses = next((r.get(family, []) for r in zc.observations
                                       if r["hostname"].rstrip(".") == self.server.rstrip(".")), [])
                if not self.addresses:
                    time.sleep(timeout / 1000)
                return bool(self.addresses)
            def parsed_scoped_addresses(self, version):
                return self.addresses
        return Resolver
    monkeypatch.setattr("zeroconf.Zeroconf", FakeZeroconf)
    monkeypatch.setattr("zeroconf.ServiceBrowser", FakeBrowser)
    monkeypatch.setattr("zeroconf.AddressResolverIPv4", resolver("ipv4"))
    monkeypatch.setattr("zeroconf.AddressResolverIPv6", resolver("ipv6"))
    monkeypatch.setattr(bonjour, "command_exists", lambda _name: False)
    return closed


def install_native(monkeypatch, tmp_path, observations, *, address_delay=0.02, delayed_family="ipv6", ignore_terminate=False):
    script = tmp_path / "fake-dns-sd.py"
    script.write_text('''import sys,time,signal,json,threading
records = json.loads(sys.argv[1]); delay=float(sys.argv[2]); ignore=sys.argv[3]=='1'; epoch=float(sys.argv[4]); delayed=sys.argv[5]; args=sys.argv[6:]
changes_ready = threading.Event()
signal.signal(signal.SIGUSR1, lambda *_args: changes_ready.set())
if ignore: signal.signal(signal.SIGTERM, signal.SIG_IGN)
index=int(args[args.index('-i')+1]) if '-i' in args else None
if '-B' in args:
    stype=args[args.index('-B')+1]+'.local.'
    for r in records:
        if r['service_type']==stype and r.get('initial',True):
            print('10:20:00 Add 2 %d local. %s %s' % (r.get('interface_index',14), stype.removesuffix('local.'), r['name']),flush=True)
    for r in records:
        if r['service_type']==stype:
            for wait,action in r.get('browse_events',[]):
                if wait is None: changes_ready.wait()
                else: time.sleep(wait)
                print('10:20:01 %s 2 %d local. %s %s' % (action,r.get('interface_index',14), stype.removesuffix('local.'),r['name']),flush=True)
    time.sleep(30)
elif '-L' in args:
    name,stype=args[args.index('-L')+1:args.index('-L')+3]
    for r in records:
        if r['name']==name and r['service_type']==stype+'.local.' and (index is None or index==r.get('interface_index',14)):
            print('10:20:00 %s.%s can be reached at %s.:%d (interface %d)' % (name,r['service_type'],r['hostname'],r['port'],r.get('interface_index',14)),flush=True)
            print(r['native_txt'],flush=True)
            break
elif '-G' in args:
    host=args[args.index('-G')+2]
    r=next((r for r in records if r['hostname']==host and (index is None or index==r.get('interface_index',14))),None)
    if r:
        for after,ipv4,ipv6 in r.get('address_updates',[]):
            if time.clock_gettime(time.CLOCK_MONOTONIC) >= epoch+after:
                r = dict(r,ipv4=ipv4,ipv6=ipv6)
        protocol=args[args.index('-G')+1]
        for family in (('ipv4','ipv6') if delayed=='ipv6' else ('ipv6','ipv4')):
            if protocol != 'v4v6' and protocol != ('v4' if family=='ipv4' else 'v6'): continue
            if family==delayed: time.sleep(max(0,epoch+delay-time.clock_gettime(time.CLOCK_MONOTONIC)))
            for address in r.get(family,[]):
                print('10:20:00 Add 2 %d %s. %s 120' % (r.get('interface_index',14),host,address),flush=True)
    time.sleep(30)
''')
    children = []
    epochs = {}
    launch = native_dns_sd._ProcessOwner.launch
    wire_observations = [dict(r, native_txt=native_txt_output(r.get("properties", {}))) for r in observations]
    def fake_launch(owner, args):
        # macOS Python 3.9's monotonic() starts separately in each process.
        now = time.clock_gettime(time.CLOCK_MONOTONIC)
        epoch = epochs.setdefault(args[-1], now) if "-G" in args else now
        proc = launch(owner, [sys.executable, "-u", str(script), json.dumps(wire_observations), str(address_delay), str(int(ignore_terminate)), str(epoch), delayed_family, *args[1:]])
        children.append(proc)
        return proc
    monkeypatch.setattr(native_dns_sd._ProcessOwner, "launch", fake_launch)
    monkeypatch.setattr(bonjour, "command_exists", lambda _name: True)
    return children


def cap_browse_window(monkeypatch, provider, seconds=2.0):
    """Public boundary uses valid input; real adapter timing is exercised with a small test window."""
    backend = native_dns_sd if provider == "dns-sd" else zeroconf_backend
    original = backend.discover_snapshot_merged_detailed
    requested = []
    def browse(service=None, timeout=6, **kwargs):
        requested.append((service, timeout))
        return original(service, seconds, **kwargs)
    monkeypatch.setattr(backend, "discover_snapshot_merged_detailed", browse)
    return requested
