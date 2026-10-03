from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
import selectors
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from collections.abc import Sequence
import time
from dataclasses import dataclass, field, replace

from timecapsulesmb.core.net import scoped_ip_literal, is_link_local_ipv6

from timecapsulesmb.discovery.models import (
    BonjourDiscoverySnapshot,
    BonjourQueryDiagnostics,
    BonjourIPFamily,
    BonjourResolvedService,
    BonjourServiceInstance,
    DEFAULT_BROWSE_TIMEOUT_SEC,
    BonjourPermissionDenied,
    _matching_service_types, _merge_snapshots, normalize_properties,
)
from timecapsulesmb.discovery.interfaces import interface_index_for_target

# dns-sd's printtimestamp() writes "%2d:%02d:%02d.%03d  ": hours before 10
# start with a space, and two spaces separate the stamp from the callback.
_DNS_SD_TIMESTAMP = r" ?\d{1,2}:\d\d:\d\d(?:\.\d+)?"


@dataclass
class NativeDnsSdServiceEvent:
    service_type: str
    action: str
    interface_index: int | None
    flags: str
    domain: str
    name: str


@dataclass
class NativeDnsSdBrowseResult:
    service_type: str
    events: list[NativeDnsSdServiceEvent] = field(default_factory=list)
    parse_error_count: int = 0
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdAddressResult:
    hostname: str
    family: str
    addresses: list[str] = field(default_factory=list)
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdResolveResult:
    service_type: str
    name: str
    fullname: str = ""
    hostname: str = ""
    port: int = 0
    interface_index: int | None = None
    addresses: list[NativeDnsSdAddressResult] = field(default_factory=list)
    stderr: str = ""
    exit_code: int | None = None
    terminated_after_timeout: bool = False
    error: str = ""


@dataclass
class NativeDnsSdDiscoveryDiagnostics:
    timeout_sec: float
    elapsed_sec: float
    status: str
    service_types: list[str]
    ip_version: str
    instance_count: int
    resolved_count: int
    browses: list[NativeDnsSdBrowseResult]
    resolves: list[NativeDnsSdResolveResult] = field(default_factory=list)
    error: str = ""
    pending_count: int = 0


def _normalize_dns_sd_service_type(service_type: str) -> str:
    value = service_type.strip()
    for suffix in (".local.", ".local"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip(".")


def _dns_sd_service_type_domain(service_type: str, domain: str = "local.") -> str:
    normalized = _normalize_dns_sd_service_type(service_type)
    normalized_domain = (domain or "local.").strip().strip(".") or "local"
    return f"{normalized}.{normalized_domain}."


def _parse_dns_sd_browse_output(service_type: str, stdout: str) -> tuple[list[NativeDnsSdServiceEvent], int]:
    events: list[NativeDnsSdServiceEvent] = []
    parse_error_count = 0
    # dns-sd prints LF records; Unicode line separators belong to service labels.
    for line in stdout.split("\n"):
        line = line.removesuffix("\r")
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("Browsing for ")
            or stripped.startswith("DATE:")
            or stripped.startswith("Timestamp")
            or re.fullmatch(rf"{_DNS_SD_TIMESTAMP}[ \t]+\.\.\.STARTING\.\.\.", stripped)
        ):
            continue
        prefix = re.match(rf"^{_DNS_SD_TIMESTAMP}[ \t]+(Add|Rmv)[ \t]+([0-9A-Fa-f]+)[ \t]+(-?\d+)[ \t]+", line)
        if prefix is None:
            parse_error_count += 1
            continue
        action, flags, iface = prefix.groups()
        remainder = line[prefix.end():].encode("utf-8")
        fields = []
        # Apple's %-20s fields pad UTF-8 bytes. Consume only that padding and
        # the single separator, so even a leading space/tab remains in the label.
        for _ in range(2):
            field = re.match(rb"[^ \t]+", remainder)
            if field is None:
                break
            value = field[0]
            framed = value.ljust(20) + b" "
            if not remainder.startswith(framed):
                break
            fields.append(value.decode("utf-8"))
            remainder = remainder[len(framed):]
        if len(fields) != 2 or not remainder:
            parse_error_count += 1
            continue
        domain, observed_service_type = fields
        events.append(
            NativeDnsSdServiceEvent(
                service_type=observed_service_type.rstrip(".") or service_type,
                action=action,
                interface_index=int(iface),
                flags=flags,
                domain=domain,
                # Browse replies print the raw instance label, not an escaped DNS fullname.
                name=remainder.decode("utf-8"),
            )
        )
    return events, parse_error_count


MAX_COMMAND_OUTPUT = 1024 * 1024


class _ProcessOwner:
    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.lock = threading.Lock()
        self.stopping = False
        self.children: set[subprocess.Popen[bytes]] = set()

    def launch(self, args: list[str]) -> subprocess.Popen[bytes]:
        if self.cancel.is_set():
            raise KeyboardInterrupt
        if self.stopping:
            raise subprocess.TimeoutExpired(args, 0)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.lock:
            self.children.add(proc)
        if self.cancel.is_set() or self.stopping:
            self.stop()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            with self.lock:
                self.children.discard(proc)
            if self.cancel.is_set():
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired(args, 0)
        return proc

    def stop(self) -> None:
        self.stopping = True
        with self.lock:
            children = list(self.children)
        # Signal every child before waiting; cleanup is one budget, not N budgets.
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
        end = time.monotonic() + 0.25
        while any(p.poll() is None for p in children) and time.monotonic() < end:
            time.sleep(0.01)
        for proc in children:
            if proc.poll() is None:
                proc.kill()
        for proc in children:
            proc.wait()


class _ProcessIO:
    def __init__(self, owner: _ProcessOwner, args: list[str]):
        self.owner = owner
        self.selector = selectors.DefaultSelector()
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            self.proc = owner.launch(args)
            for label, stream in (("stdout", self.proc.stdout), ("stderr", self.proc.stderr)):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, label)
        except BaseException:
            if hasattr(self, "proc"):
                self.close()
            else:
                self.selector.close()
            raise

    def read(self, timeout: float = 0.0) -> None:
        for key, _event in self.selector.select(timeout):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                continue
            target = self.output[key.data]
            if len(target) + len(chunk) > MAX_COMMAND_OUTPUT:
                raise RuntimeError("Bonjour command output exceeded its limit")
            target.extend(chunk)

    def text(self, label: str) -> str:
        return self.output[label].decode("utf-8", "ignore" if label == "stdout" else "replace")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.selector.close()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        with self.owner.lock:
            self.owner.children.discard(self.proc)


def _command_error(stdout: str, stderr: str, exit_code: int | None, stopped: bool,
                   *, operation: str | None = None) -> str:
    timestamp = _DNS_SD_TIMESTAMP
    callback = rf"(?:{timestamp}\s+)?Error code\s+(-\d+)\s*"
    startup = r"DNSService[A-Za-z]+(?:\(\))?\s+(?:failed|returned)\s+(-\d+)(?:\s+\(Service Not Running\))?\s*"
    resolve = rf"{timestamp}\s+.+\._[^.\s]+\._(?:tcp|udp)\.\S+\.\s+error code\s+(-\d+)(?:\s+Flags:\s+[0-9A-F]+)?\s*"
    addresses = rf"{timestamp}\s+(?:Add|Rmv)\s+[0-9A-F]+\s+(?:[A-Za-z?+-]\s+)?\d+\s+\S+\s+\S+\s+\d+\s+Error code\s+(-\d+)\s*"
    codes: list[int] = []
    for output, is_stderr in ((stdout, False), (stderr, True)):
        # A Unicode separator inside a browse label cannot turn the label's
        # remaining text into a standalone denial/error callback.
        lines = output.split("\n")
        for index, raw_line in enumerate(lines):
            # A live stream may end in half a callback; classification waits for its newline.
            if exit_code is None and index == len(lines) - 1 and not raw_line.endswith("\r"):
                continue
            line = raw_line.strip() if is_stderr else raw_line.rstrip("\r\n")
            match = re.fullmatch(callback, line, re.I)
            if match is None:
                # Match actual callback positions. A browse name, successful target,
                # lookup header or TXT string is data, even if it contains an error code.
                pattern = startup if is_stderr else resolve if operation == "-L" else addresses if operation == "-G" else None
                if pattern is not None:
                    match = re.fullmatch(pattern, line, re.I)
            if match is not None:
                codes.append(int(match.group(1)))
    if -65570 in codes:
        return "Bonjour query error -65570"
    error = next((code for code in codes if code != -65554), None)
    if error is not None:
        return f"Bonjour query error {error}"
    if codes:  # No-such-record callbacks are ordinary absence, not query failure.
        return ""
    if exit_code not in (None, 0) and not stopped:
        return stderr.strip()[:512] or f"Bonjour command exited with status {exit_code}"
    return ""


def _run_dns_sd_command(args: list[str], *, timeout_sec: float,
                        owner: _ProcessOwner | None = None) -> tuple[str, str, int | None, bool, str]:
    owner = owner or _ProcessOwner(threading.Event())
    try:
        io = _ProcessIO(owner, args)
    except subprocess.TimeoutExpired:
        return "", "", None, True, ""
    end = time.monotonic() + max(0.0, timeout_sec)
    stopped = False
    try:
        while io.selector.get_map():
            if owner.cancel.is_set():
                raise KeyboardInterrupt
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            io.read(min(0.05, remaining))
        io.read()
        if not io.selector.get_map() and io.proc.poll() is None:
            try:
                io.proc.wait(timeout=min(0.05, max(0.0, end - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        stopped = io.proc.poll() is None or (owner.stopping and io.proc.returncode != 0)
    finally:
        io.close()
    stdout, stderr = io.text("stdout"), io.text("stderr")
    operation = next((arg for arg in args if arg in {"-B", "-L", "-G"}), None)
    error = _command_error(stdout, stderr, io.proc.returncode, stopped, operation=operation)
    if error == "Bonjour query error -65570":
        raise BonjourPermissionDenied(error)
    return stdout, stderr, io.proc.returncode, stopped, error


def _parse_reached_line(line: str) -> tuple[str, str, int, int | None] | None:
    marker = " can be reached at "
    if marker not in line:
        return None
    left, right = line.rsplit(marker, 1)
    # dns-sd escapes ASCII spaces and controls in a fullname (\032), so every
    # ASCII space after the timestamp is framing. A fullname may still start
    # with an unescaped NBSP or contain other Unicode whitespace.
    fullname = re.sub(rf"^{_DNS_SD_TIMESTAMP}[ \t]+", "", left, count=1)

    host_port = right.split(" (", 1)[0].strip()
    if ":" not in host_port:
        return None
    host, port_text = host_port.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError:
        return None

    interface_index = None
    interface_match = re.search(r"\(interface\s+(\d+)\)", right)
    if interface_match:
        interface_index = int(interface_match.group(1))

    return _decode_dns_sd_text(fullname), _decode_dns_sd_text(host.strip().rstrip(".")), port, interface_index


def _decode_dns_sd_text(value: str) -> str:
    # DNS escapes encode UTF-8 bytes, not independent Unicode code points.
    out = bytearray()
    for chunk in re.split(r"(\\(?:[0-9]{3}|x[0-9A-Fa-f]{2}|.))", value):
        if re.fullmatch(r"\\[0-9]{3}", chunk) and int(chunk[1:]) <= 255:
            out.append(int(chunk[1:]))
        elif re.fullmatch(r"\\x[0-9A-Fa-f]{2}", chunk):
            out.append(int(chunk[2:], 16))
        elif len(chunk) == 2 and chunk.startswith("\\"):
            out.extend(chunk[1].encode("utf-8"))
        else:
            out.extend(chunk.encode("utf-8"))
    return out.decode("utf-8", "ignore")


def _decode_dns_sd_txt_field(value: str) -> str:
    # Apple's ShowTXTRecord escapes twice: shell quoting, then dns-sd -R's
    # byte/backslash syntax. Decimal DNS fullname escapes do not apply to TXT.
    out = bytearray()
    for chunk in re.split(r"(\\(?:x[0-9A-Fa-f]{2}|.))", value):
        if re.fullmatch(r"\\x[0-9A-Fa-f]{2}", chunk):
            out.append(int(chunk[2:], 16))
        elif len(chunk) == 2 and chunk.startswith("\\"):
            out.extend(chunk[1].encode("utf-8"))
        else:
            out.extend(chunk.encode("utf-8"))
    return out.decode("utf-8", "ignore")


def _parse_dns_sd_txt_output(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    reached_service = False
    for line in stdout.split("\n"):
        # A trailing escaped space is part of the TXT value, not line padding.
        stripped = line.lstrip()
        if _parse_reached_line(stripped) is not None:
            reached_service = True
            continue
        if not reached_service or not stripped.strip():
            continue
        # dns-sd prints each TXT string as key=value after the SRV target line
        # and backslash-escapes whitespace inside values.
        try:
            fields = shlex.split(stripped)
        except ValueError:
            continue
        for field in fields:
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            properties[_decode_dns_sd_txt_field(key)] = _decode_dns_sd_txt_field(value)
    return normalize_properties(properties)


def _parse_dns_sd_lookup_output(
    service_type: str,
    name: str,
    stdout: str,
) -> tuple[str, str, int, int | None, dict[str, str]]:
    properties = _parse_dns_sd_txt_output(stdout)
    for line in stdout.split("\n"):
        parsed = _parse_reached_line(line.removesuffix("\r"))
        if parsed is not None:
            return (*parsed, properties)
    return f"{name}.{_dns_sd_service_type_domain(service_type)}", "", 0, None, properties


def _append_ip(values: list[str], candidate: str) -> None:
    cleaned = candidate.strip().rstrip(",")
    if not cleaned:
        return
    try:
        address = ipaddress.ip_address(cleaned.split("%", 1)[0])
    except ValueError:
        return
    value = str(address)
    if "%" in cleaned and address.version == 6:
        value = cleaned
    if value not in values:
        values.append(value)


def _parse_dns_sd_address_observations(
    stdout: str, initial: Sequence[str] = (),
) -> dict[int, list[str]]:
    """Only include families with callbacks; silence is not a negative answer."""
    observed: dict[int, list[str]] = {}
    replaced: set[int] = set()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[1].lower() not in {"add", "rmv"}:
            continue
        # Some dns-sd versions print a validation-status column before the interface.
        offset = 0 if fields[3].isdigit() else 1
        if len(fields) < 7 + offset:
            continue
        index_text, address = fields[3 + offset], fields[5 + offset]
        if not index_text.isdigit():
            continue
        try:
            version = ipaddress.ip_address(address.split("%", 1)[0]).version
        except ValueError:
            continue  # <error> has no address family; it cannot invalidate either one.
        if fields[7 + offset:]:
            if fields[7 + offset:] in (["No", "Such", "Record"], ["Error", "code", "-65554"]):
                observed[version] = []
                replaced.add(version)
            continue
        index = int(index_text)
        if index and is_link_local_ipv6(address) and "%" not in address:
            address = scoped_ip_literal(address, scope_id=index) or address
        values: list[str] = []
        _append_ip(values, address)
        if not values:
            continue
        address = values[0]
        if fields[1].lower() == "add":
            if version not in replaced:
                observed[version] = []
                replaced.add(version)
            _append_ip(observed[version], address)
        else:
            current = observed.setdefault(version, [ip for ip in initial if (":" in ip) == (version == 6)])
            if address in current:
                current.remove(address)
    return observed


def _parse_dns_sd_address_output(stdout: str, initial: Sequence[str] = ()) -> list[str]:
    observed = _parse_dns_sd_address_observations(stdout, initial)
    return [ip for version in (4, 6) for ip in observed.get(
        version, [ip for ip in initial if (":" in ip) == (version == 6)]
    )]



def _interface_args(index: int | None) -> list[str]:
    return ["-i", str(index)] if index else []


def _resolve(instance: BonjourServiceInstance, previous: BonjourResolvedService | None,
             end: float, family: BonjourIPFamily | None, owner: _ProcessOwner,
             attempt_sec: float | None = None) -> tuple[BonjourResolvedService | None, NativeDnsSdResolveResult]:
    stype = _normalize_dns_sd_service_type(instance.service_type)
    domain = instance.service_type[len(stype):].strip(".") or "local"
    result = NativeDnsSdResolveResult(stype, instance.name, interface_index=instance.interface_index)
    # ponytail: retain evidence only for this bounded scan, not a persistent DNS
    # cache. Fresh family answers replace it; silence at shutdown does not erase it.
    record = replace(previous, ipv4=list(previous.ipv4), ipv6=list(previous.ipv6)) if previous else None
    if attempt_sec is not None:
        end = min(end, time.monotonic() + attempt_sec)
    def budget() -> float:
        return max(0.0, end - time.monotonic())
    if record is None:
        stdout, stderr, code, stopped, error = _run_dns_sd_command(
            ["dns-sd", "-m", *_interface_args(instance.interface_index), "-L", instance.name, stype, domain],
            timeout_sec=budget(), owner=owner,
        )
        result.stderr, result.exit_code, result.terminated_after_timeout, result.error = stderr, code, stopped, error
        if error:
            return None, result
        fullname, host, port, index, props = _parse_dns_sd_lookup_output(stype, instance.name, stdout)
        if stopped or not host or not 0 <= port <= 65535:
            result.error = "Bonjour service target has not resolved"
            return None, result
        record = BonjourResolvedService(instance.name, host, instance.service_type, port,
                                         properties=props, fullname=instance.fullname or fullname,
                                         interface_index=instance.interface_index or index)
    protocol = {"ipv4": "v4", "ipv6": "v6"}.get(family, "v4v6")
    if budget() > 0:
        stdout, stderr, code, stopped, error = _run_dns_sd_command(
            ["dns-sd", *_interface_args(record.interface_index), "-G", protocol, record.hostname],
            timeout_sec=budget(), owner=owner,
        )
        addresses = _parse_dns_sd_address_output(stdout, [*record.ipv4, *record.ipv6])
        result.addresses.append(NativeDnsSdAddressResult(
            record.hostname, protocol, _parse_dns_sd_address_output(stdout), stderr, code, stopped, error))
        result.error = error
        ipv4 = [a for a in addresses if ":" not in a]
        ipv6 = [a for a in addresses if ":" in a]
        record.ipv4, record.ipv6 = ipv4, ipv6
    result.hostname, result.port, result.fullname = record.hostname, record.port, record.fullname
    return record, result


def discover_snapshot_merged_detailed(service: str | None = None, timeout: float = DEFAULT_BROWSE_TIMEOUT_SEC,
                                      *, service_types: Sequence[str] | None = None,
                                      family: BonjourIPFamily | None = None, deadline: float | None = None,
                                      cancel: threading.Event | None = None, target_ip: str | None = None,
                                      interfaces: Sequence[str] | None = None) -> tuple[BonjourDiscoverySnapshot, BonjourQueryDiagnostics]:
    index = interface_index_for_target(target_ip, interfaces)
    cancel = cancel or threading.Event()
    owner = _ProcessOwner(cancel)
    types = list(service_types) if service_types is not None else _matching_service_types(service)
    start = time.monotonic()
    browse_end = start + max(0.0, timeout)
    end = browse_end + 3.0
    if deadline is not None:
        browse_end, end = min(browse_end, deadline), min(end, deadline)
    streams: list[tuple[str, _ProcessIO]] = []
    offsets: dict[int, int] = {}
    instances: dict[tuple[str, str, int | None], BonjourServiceInstance] = {}
    generations: dict[tuple[str, str, int | None], int] = {}
    records: dict[tuple[str, str, int | None], BonjourResolvedService] = {}
    pending: deque[tuple[str, str, int | None]] = deque()
    futures: dict[object, tuple[tuple[str, str, int | None], int]] = {}
    browses = [NativeDnsSdBrowseResult(_normalize_dns_sd_service_type(t)) for t in types]
    resolves: list[NativeDnsSdResolveResult] = []
    errors: list[str] = []
    executor = ThreadPoolExecutor(max_workers=4)
    def finish_work() -> None:
        for future in list(futures):
            if not future.done():
                continue
            key, generation = futures.pop(future)
            if future.cancelled():
                continue
            record, result = future.result()
            if len(resolves) < 100:
                resolves.append(result)
            if generation != generations.get(key) or key not in instances:
                continue
            if result.error and result.error != "Bonjour service target has not resolved" and result.error not in errors:
                errors.append(result.error)
            if record is not None:
                records[key] = record
            complete = record is not None and (
                bool(record.ipv4) if family == "ipv4" else bool(record.ipv6) if family == "ipv6" else bool(record.ipv4 and record.ipv6)
            )
            if not complete:
                pending.append(key)

    try:
        for stype in types:
            streams.append((stype, _ProcessIO(owner, ["dns-sd", *_interface_args(index), "-B", _normalize_dns_sd_service_type(stype), "local"])))
        while time.monotonic() < end:
            if cancel.is_set():
                raise KeyboardInterrupt
            now = time.monotonic()
            # Admission closes at the browse deadline; withdrawals of admitted
            # services must still invalidate their in-flight work during grace.
            if now < end:
                for i, (stype, io) in enumerate(streams):
                    io.read()
                    raw = io.output["stdout"]
                    begin = offsets.get(i, 0)
                    cut = raw.rfind(b"\n", begin) + 1
                    if cut > begin:
                        events, malformed = _parse_dns_sd_browse_output(stype, bytes(raw[begin:cut]).decode("utf-8", "replace"))
                        offsets[i] = cut
                        browses[i].parse_error_count += malformed
                        for event in events:
                            if len(browses[i].events) < 100:
                                browses[i].events.append(event)
                            canonical = _dns_sd_service_type_domain(event.service_type, event.domain)
                            key = (canonical, event.name, event.interface_index)
                            if event.action.lower() == "rmv":
                                instances.pop(key, None)
                                records.pop(key, None)
                                generations[key] = generations.get(key, 0) + 1
                            elif event.action.lower() == "add" and event.name:
                                if key not in instances and now < browse_end:
                                    generations[key] = generations.get(key, 0) + 1
                                    instances[key] = BonjourServiceInstance(canonical, event.name, f"{event.name}.{canonical}", event.interface_index)
                                    pending.append(key)
                    error = _command_error(io.text("stdout"), io.text("stderr"), io.proc.poll(), False, operation="-B")
                    if error == "Bonjour query error -65570":
                        raise BonjourPermissionDenied(error)
                    if error and error not in errors:
                        errors.append(error)
            finish_work()
            busy = {k for k, _generation in futures.values()}
            for _ in range(len(pending)):
                if len(futures) >= 4:
                    break
                key = pending.popleft()
                if key not in instances:
                    continue
                if key in busy:
                    pending.append(key)
                    continue
                # Short attempts rotate fairly; family completion shares one absolute deadline.
                future = executor.submit(_resolve, instances[key], records.get(key), end, family, owner, 0.5)
                futures[future] = (key, generations[key])
                busy.add(key)
            if now >= browse_end and not pending and not futures:
                break
            time.sleep(min(0.02, max(0.0, end - time.monotonic())))
    except BaseException:
        cancel.set()
        owner.stop()
        raise
    finally:
        # Request shutdown before joining workers, so SIGINT finishes within the GUI's grace.
        for future in futures:
            future.cancel()
        owner.stop()
        executor.shutdown(wait=True, cancel_futures=True)
        if not cancel.is_set():
            finish_work()
        for i, (_stype, io) in enumerate(streams):
            browses[i].stderr = io.text("stderr")[:512]
            browses[i].exit_code = io.proc.returncode
            browses[i].terminated_after_timeout = True
            io.close()
    snapshot = _merge_snapshots([BonjourDiscoverySnapshot(list(instances.values()), list(records.values()))])
    if errors and not snapshot.resolved:
        raise RuntimeError("; ".join(errors)[:512])
    diagnostics = NativeDnsSdDiscoveryDiagnostics(
        timeout, round(time.monotonic() - start, 3), "partial" if errors else "ok", types,
        "IPv4+IPv6" if family is None else family, len(snapshot.instances), len(snapshot.resolved), browses, resolves,
        "; ".join(errors)[:512],
    )
    diagnostics.pending_count = len({k for k in pending if k in instances} | {k for k, g in futures.values() if k in instances and g == generations.get(k)})
    return snapshot, BonjourQueryDiagnostics("dns-sd", types, timeout, diagnostics.elapsed_sec,
        len(snapshot.instances), len(snapshot.resolved), details=diagnostics,
        pending_count=diagnostics.pending_count, errors={"query": diagnostics.error} if diagnostics.error else {})


def resolve_service_instance_detailed(instance: BonjourServiceInstance, timeout_ms: int = 3000,
                                      *, family: BonjourIPFamily | None = None,
                                      cancel: threading.Event | None = None, target_ip: str | None = None,
                                      interfaces: Sequence[str] | None = None) -> tuple[BonjourResolvedService | None, BonjourQueryDiagnostics]:
    start = time.monotonic()
    owner = _ProcessOwner(cancel or threading.Event())
    if instance.interface_index is None:
        instance = replace(instance, interface_index=interface_index_for_target(target_ip, interfaces))
    try:
        record, result = _resolve(instance, None, time.monotonic() + max(0, timeout_ms) / 1000, family, owner)
    finally:
        owner.stop()
    errors = {"query": result.error} if result.error and result.error != "Bonjour service target has not resolved" else {}
    return record, BonjourQueryDiagnostics("dns-sd", [instance.service_type], timeout_ms / 1000,
        round(time.monotonic() - start, 3), 0, int(record is not None), details=result, errors=errors)
