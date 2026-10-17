"""Wire observations shared by provider, helper and Swift contract tests."""
from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
from types import SimpleNamespace

from timecapsulesmb.discovery import bonjour, native_dns_sd, zeroconf_backend
from timecapsulesmb.discovery.models import SERVICE_TYPES


# Apple dns-sd prints "%2d:%02d:%02d.%03d  " before each callback. Fixtures use
# a single-digit hour, whose leading space a parser must also accept.
APPLE_STAMP = " 9:20:00.123  "


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


def browse_row(name, *, action="Add", domain="local.", service="_smb._tcp.", index=14, flags=2):
    # The C formatter's minimum widths apply to UTF-8 bytes, not Python code points.
    return (f"{APPLE_STAMP}{action} {flags:8X} {index:3d} ".encode()
            + domain.encode().ljust(20) + b" " + service.encode().ljust(20) + b" "
            + name.encode() + b"\n").decode()


def native_fullname(name, service_type):
    """DNSServiceConstructFullName escapes ASCII controls, spaces, dots and backslashes."""
    label = "".join(f"\\{ord(char):03d}" if ord(char) <= 32 else "\\" + char
                    if char in ".\\" else char for char in name)
    return f"{label}.{service_type}"


def install_zeroconf(monkeypatch, observations, *, fail_family=None, family_observations=None, delays=None,
                     address_delays=None, address_ready=None):
    from zeroconf import IPVersion, ServiceStateChange
    closed = []
    def missing_families():
        return {f for f, delay in (address_delays or {}).items() if time.monotonic() < started + delay} | {
            f for f, ready in (address_ready or {}).items() if not ready.is_set()}
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
                    return info({**r, **{f: [] for f in missing_families()}})
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
                ready = (address_ready or {}).get(family)
                if ready is not None and not ready.wait(timeout / 1000):
                    return False
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


def install_native(monkeypatch, tmp_path, observations, *, address_delay=0.02, delayed_family="ipv6", ignore_terminate=False,
                   fragment_browse=False, child_startup_delay=0):
    script = tmp_path / "fake-dns-sd.py"
    script.write_text('''import sys,time,signal,json,threading,os
ready=int(sys.argv.pop(1).split('=',1)[1]); startup=float(sys.argv.pop(1)); query=int(sys.argv.pop(1))
records = json.loads(sys.argv[1]); delay=float(sys.argv[2]); ignore=sys.argv[3]=='1'; epoch=float(sys.argv[4]); delayed=sys.argv[5]; fragment=sys.argv[6]=='1'; args=sys.argv[7:]
changes_ready = threading.Event()
signal.signal(signal.SIGUSR1, lambda *_args: changes_ready.set())
if ignore: signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(startup)
# Running, with its handlers installed: the launch may now hand it to discovery.
os.write(ready,b'R'); os.close(ready)
index=int(args[args.index('-i')+1]) if '-i' in args else None
# Apple's preamble, captured from macOS 26.6.2. dns-sd flushes stdout only from a
# callback, so the preamble reaches the pipe with the first answer, never alone.
preamble=('Using interface %d\\n' % index if index else '')
if '-B' in args:
    preamble+='Browsing for %s.%s\\n' % tuple(args[args.index('-B')+1:args.index('-B')+3])
elif '-L' in args:
    preamble+='Lookup %s.%s.%s\\n' % tuple(args[args.index('-L')+1:args.index('-L')+4])
preamble+='DATE: ---Thu 08 Oct 2026---\\n 9:20:00.100  ...STARTING...\\n'
if '-B' in args:
    preamble+='Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\\n'
elif '-G' in args:
    preamble+='Timestamp     A/R  Flags         IF  Hostname                               Address                                      TTL\\n'
def out(data):
    global preamble
    sys.stdout.buffer.write(preamble.encode()+data); sys.stdout.buffer.flush(); preamble=''
def emit_browse(action,r):
    row=r['native_browse_rows'][action].encode()
    if fragment:
        label_start=len(row)-len(r['name'].encode())-1
        cuts=[0,label_start,label_start+1,len(row)-1,len(row)]
        for start,end in zip(cuts,cuts[1:]):
            out(row[start:end]); time.sleep(.02)
    else:
        out(row)
if '-B' in args:
    stype=args[args.index('-B')+1]+'.local.'
    for r in records:
        if r['service_type']==stype and r.get('initial',True):
            emit_browse('Add',r)
    for r in records:
        if r['service_type']==stype:
            for wait,action in r.get('browse_events',[]):
                if wait is None: changes_ready.wait()
                else: time.sleep(wait)
                emit_browse(action,r)
    time.sleep(30)
elif '-L' in args:
    name,stype=args[args.index('-L')+1:args.index('-L')+3]
    for r in records:
        if r['name']==name and r['service_type']==stype+'.local.' and (index is None or index==r.get('interface_index',14)):
            out((' 9:20:00.123  %s can be reached at %s.:%d (interface %d)\\n%s\\n' % (r['native_fullname'],r['hostname'],r['port'],r.get('interface_index',14),r['native_txt'])).encode())
            break
elif '-G' in args:
    host=args[args.index('-G')+2]
    r=next((r for r in records if r['hostname']==host and (index is None or index==r.get('interface_index',14))),None)
    if r:
        # address_updates rows: [nth, ipv4, ipv6], from the host's nth -G child on.
        for nth,ipv4,ipv6 in r.get('address_updates',[]):
            if query >= nth:
                r = dict(r,ipv4=ipv4,ipv6=ipv6)
        protocol=args[args.index('-G')+1]
        for family in (('ipv4','ipv6') if delayed=='ipv6' else ('ipv6','ipv4')):
            if protocol != 'v4v6' and protocol != ('v4' if family=='ipv4' else 'v6'): continue
            if family==delayed: time.sleep(max(0,epoch+delay-time.clock_gettime(time.CLOCK_MONOTONIC)))
            addresses=r.get(family,[])
            for i,address in enumerate(addresses):
                # Flag 1 (MoreComing) marks all but a batch's last answer; -m exits after the batch.
                flags=3 if i<len(addresses)-1 else 2
                out((' 9:20:00.123  Add %d %d %s. %s 120\\n' % (flags,r.get('interface_index',14),host,address)).encode())
            if addresses and '-m' in args: sys.exit()
    time.sleep(30)
''')
    children = []
    # dns-sd starts in milliseconds; this fake is a Python interpreter. Each
    # launch returns once its child runs, so a -L/-G read window, which starts
    # after launch, never pays for the interpreter; a later attempt retries
    # what a slow -L left of an attempt. Discovery spawns its browse children
    # after taking its start time and before its loop, so that wait is
    # credited back to the module's clock: the window starts once they run.
    credit = [0.0]
    monkeypatch.setattr(native_dns_sd, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() - credit[0], sleep=lambda seconds: time.sleep(seconds)))
    # The origin of scripted address delays, and how many -G children each
    # host has had. Resolves of one host can launch concurrently.
    scan = SimpleNamespace(epoch=None, queries={})
    scan_lock = threading.Lock()
    launch = native_dns_sd._ProcessOwner.launch
    wire_observations = [dict(r, native_txt=native_txt_output(r.get("properties", {})),
                             native_fullname=native_fullname(r['name'], r['service_type']),
                             native_browse_rows={action: browse_row(r['name'], action=action, index=r.get('interface_index', 14),
                                                                    service=r['service_type'].removesuffix('local.'))
                                                 for action in ("Add", "Rmv")}) for r in observations]
    def fake_launch(owner, args):
        # macOS Python 3.9's monotonic() starts separately in each process.
        before = time.clock_gettime(time.CLOCK_MONOTONIC)
        with scan_lock:
            epoch = scan.epoch if scan.epoch is not None else before
            query = 0
            if '-G' in args:
                key = (args[-1], args[args.index('-i')+1] if '-i' in args else None)
                query = scan.queries[key] = scan.queries.get(key, 0) + 1
        ready_read, ready_write = os.pipe()
        # popen_process uses posix_spawn with inheritable descriptors. Read a
        # byte, not EOF: simultaneous launches can inherit each other's pipe.
        os.set_inheritable(ready_write, True)
        proc = None
        try:
            proc = launch(owner, [sys.executable, "-u", str(script), f"--ready={ready_write}", str(child_startup_delay),
                                 str(query), json.dumps(wire_observations),
                                 str(address_delay), str(int(ignore_terminate)), str(epoch), delayed_family,
                                 str(int(fragment_browse)), *args[1:]])
            children.append(proc)
            while True:
                if owner.cancel.is_set():
                    raise KeyboardInterrupt
                if owner.stopping:
                    raise native_dns_sd.subprocess.TimeoutExpired(args, 0)
                # Observe exit before checking the pipe: a child can write its
                # byte and exit just after select times out. Polling afterward
                # would mistake that valid startup for an unready exit.
                exited = proc.poll() is not None
                if select.select([ready_read], [], [], .02)[0]:
                    assert os.read(ready_read, 1) == b'R', 'fake dns-sd exited before readiness'
                    break
                if exited:
                    if owner.stopping or owner.cancel.is_set():
                        continue
                    raise AssertionError('fake dns-sd exited before readiness')
                assert time.clock_gettime(time.CLOCK_MONOTONIC)-before < 10, 'fake dns-sd readiness timed out'
            ready = time.clock_gettime(time.CLOCK_MONOTONIC)
            with scan_lock:
                if '-B' in args:
                    credit[0] += ready - before
                    # Scripted delays count from when the browse children run,
                    # where the credited window starts.
                    scan.epoch = ready
                elif scan.epoch is None:
                    scan.epoch = before # a targeted resolve: from its first launch
            return proc
        except BaseException:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                for stream in (proc.stdout, proc.stderr):
                    stream.close()
                with owner.lock:
                    owner.children.discard(proc)
            raise
        finally:
            os.close(ready_read)
            os.close(ready_write)
    monkeypatch.setattr(native_dns_sd._ProcessOwner, "launch", fake_launch)
    parse = native_dns_sd._parse_dns_sd_browse_output
    def strict_parse(*args):
        # The fake prints Apple's real output, so a rejected line means the fixture and parser drifted.
        events, unparsed = parse(*args)
        assert unparsed == [], unparsed
        return events, unparsed
    monkeypatch.setattr(native_dns_sd, "_parse_dns_sd_browse_output", strict_parse)
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
