from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import socket
import struct
import zlib


ACP_PORT = 5009
ACP_VERSION = 0x00030001
# Older TimeCapsule6,106 ACPd replies have been observed using 0x00030000.
SUPPORTED_ACP_RESPONSE_VERSIONS = frozenset((
    0x00000001,
    0x00030000,
    ACP_VERSION,
))
ACP_MAGIC = b"acpp"
ACP_STATIC_KEY = bytes.fromhex("5b6faf5d9d5b0e1351f2da1de7e8d673")

COMMAND_GETPROP = 0x14
COMMAND_SETPROP = 0x15
COMMAND_FLASH_PRIMARY = 0x03
COMMAND_FLASH_SECONDARY = 0x05

# Minimal Python 3 ACP packet framing. Keep this file stdlib-only so configure
# can enable SSH without bootstrapping extra dependencies.
HEADER = struct.Struct("!4s8i12x32s48x")
PROPERTY_HEADER = struct.Struct("!4s2I")

DBUG_SSH_VALUE = 0x3000

LogCallback = Callable[[str], None]


class ACPError(RuntimeError):
    """Base error for Apple ACP protocol operations."""


class ACPConnectionError(ACPError):
    """Raised when the device cannot be reached or closes the ACP connection."""


class ACPAuthError(ACPError):
    """Raised when the device rejects an authenticated ACP command."""


class ACPProtocolError(ACPError):
    """Raised when an ACP response is malformed."""


class ACPPropertyError(ACPError):
    """Raised when an ACP property operation fails."""


@dataclass(frozen=True)
class ACPMessageHeader:
    version: int
    flags: int
    command: int
    error_code: int
    body_size: int
    body_checksum: int


@dataclass(frozen=True)
class ACPFlashResult:
    command: int
    reply_body: bytes


@dataclass(frozen=True)
class ACPIdentity:
    syap: int | None = None


def _resolve_log(log: LogCallback | None, verbose: bool) -> LogCallback | None:
    if log is not None:
        return log
    if verbose:
        return print
    return None


def _emit(log: LogCallback | None, message: str) -> None:
    if log is not None:
        log(message)


def _signed_i32(value: int) -> int:
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def _adler32_i32(data: bytes) -> int:
    return _signed_i32(zlib.adler32(data))


def _format_error_code(error_code: int) -> str:
    if error_code < 0:
        return f"-0x{-error_code:x}"
    return f"0x{error_code:x}"


def _generate_acp_keystream(length: int) -> bytes:
    return bytes(
        ((idx + 0x55) & 0xFF) ^ ACP_STATIC_KEY[idx % len(ACP_STATIC_KEY)]
        for idx in range(length)
    )


def _generate_acp_header_key(password: str) -> bytes:
    password_bytes = password.encode("utf-8")[:32].ljust(32, b"\x00")
    key = _generate_acp_keystream(32)
    return bytes(key[idx] ^ password_bytes[idx] for idx in range(32))


def _compose_header(
    *,
    command: int,
    password: str = "",
    flags: int = 0,
    error_code: int = 0,
    payload: bytes | None = None,
    body_size: int | None = None,
) -> bytes:
    if payload is None:
        resolved_body_size = -1 if body_size is None else body_size
        body_checksum = 1
    else:
        resolved_body_size = len(payload) if body_size is None else body_size
        body_checksum = _adler32_i32(payload)

    key = _generate_acp_header_key(password)
    tmp_header = HEADER.pack(
        ACP_MAGIC,
        ACP_VERSION,
        0,
        body_checksum,
        resolved_body_size,
        flags,
        0,
        command,
        error_code,
        key,
    )
    return HEADER.pack(
        ACP_MAGIC,
        ACP_VERSION,
        _adler32_i32(tmp_header),
        body_checksum,
        resolved_body_size,
        flags,
        0,
        command,
        error_code,
        key,
    )


def _compose_message(command: int, password: str, payload: bytes, *, flags: int = 0) -> bytes:
    return _compose_header(command=command, password=password, flags=flags, payload=payload) + payload


def _parse_header(data: bytes) -> ACPMessageHeader:
    if len(data) != HEADER.size:
        raise ACPProtocolError(f"ACP header has {len(data)} bytes, expected {HEADER.size}")

    magic, version, header_checksum, body_checksum, body_size, flags, _unused, command, error_code, key = HEADER.unpack(data)
    if magic != ACP_MAGIC:
        raise ACPProtocolError("ACP response had invalid magic")
    if version not in SUPPORTED_ACP_RESPONSE_VERSIONS:
        raise ACPProtocolError(f"ACP response had unsupported version {version:#x}")

    tmp_header = HEADER.pack(magic, version, 0, body_checksum, body_size, flags, _unused, command, error_code, key)
    expected_checksum = _adler32_i32(tmp_header)
    if header_checksum != expected_checksum:
        raise ACPProtocolError(
            f"ACP response header checksum mismatch: got {header_checksum:#x}, expected {expected_checksum:#x}"
        )

    return ACPMessageHeader(
        version=version,
        flags=flags,
        command=command,
        error_code=error_code,
        body_size=body_size,
        body_checksum=body_checksum,
    )


def _compose_property_element(name: str | None, value: int | bytes | None, *, flags: int = 0) -> bytes:
    name_bytes = b"\x00\x00\x00\x00" if name is None else name.encode("ascii")
    if len(name_bytes) != 4:
        raise ACPPropertyError(f"ACP property names must be exactly 4 bytes: {name!r}")

    if value is None:
        value_bytes = b"\x00\x00\x00\x00"
    elif isinstance(value, int):
        if value < 0 or value > 0xFFFFFFFF:
            raise ACPPropertyError(f"ACP integer property out of range: {value!r}")
        value_bytes = struct.pack(">I", value)
    else:
        value_bytes = value

    return PROPERTY_HEADER.pack(name_bytes, flags, len(value_bytes)) + value_bytes


def _parse_property_header(data: bytes) -> tuple[str | None, int, int]:
    if len(data) != PROPERTY_HEADER.size:
        raise ACPProtocolError(
            f"ACP property header has {len(data)} bytes, expected {PROPERTY_HEADER.size}"
        )
    raw_name, flags, size = PROPERTY_HEADER.unpack(data)
    name = None if raw_name == b"\x00\x00\x00\x00" else raw_name.decode("ascii", errors="replace")
    return name, flags, size


def _parse_property_result_from_body(body: bytes, offset: int) -> tuple[str | None, int, bytes, int]:
    end_header = offset + PROPERTY_HEADER.size
    if end_header > len(body):
        raise ACPProtocolError(
            f"ACP property header at offset {offset} extends past body size {len(body)}"
        )
    name, flags, size = _parse_property_header(body[offset:end_header])
    end_value = end_header + size
    if end_value > len(body):
        raise ACPProtocolError(
            f"ACP property {name or '<end>'} value extends past body size {len(body)}"
        )
    data = body[end_header:end_value]
    if flags & 1:
        if len(data) == 4:
            error_code = struct.unpack(">i", data)[0]
            raise ACPPropertyError(
                f"ACP property {name or '<end>'} failed with error_code {_format_error_code(error_code)}"
            )
        raise ACPPropertyError(f"ACP property {name or '<end>'} failed")
    return name, flags, data, end_value


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        try:
            chunk = sock.recv(remaining)
        except OSError as exc:
            raise ACPConnectionError(f"ACP receive failed: {exc}") from exc
        if not chunk:
            raise ACPConnectionError(f"ACP connection closed while reading {size} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_connection(host: str, *, timeout: float) -> socket.socket:
    try:
        sock = socket.create_connection((host, ACP_PORT), timeout=timeout)
        sock.settimeout(timeout)
        return sock
    except OSError as exc:
        raise ACPConnectionError(f"Could not connect to ACP on {host}:{ACP_PORT}: {exc}") from exc


def _send_message(host: str, password: str, command: int, payload: bytes, *, flags: int = 0, timeout: float) -> socket.socket:
    sock = _open_connection(host, timeout=timeout)
    try:
        sock.sendall(_compose_message(command, password, payload, flags=flags))
        return sock
    except OSError as exc:
        sock.close()
        raise ACPConnectionError(f"ACP send failed: {exc}") from exc


def _read_reply_header(sock: socket.socket, *, expected_command: int) -> ACPMessageHeader:
    header = _parse_header(_recv_exact(sock, HEADER.size))
    if header.command != expected_command:
        raise ACPProtocolError(
            f"ACP response command mismatch: got {header.command:#x}, expected {expected_command:#x}"
        )
    if header.error_code != 0:
        raise ACPAuthError(
            f"ACP command failed with error_code {_format_error_code(header.error_code)} "
            "(likely wrong AirPort admin password)"
        )
    return header


def _read_reply_body(sock: socket.socket, header: ACPMessageHeader) -> bytes:
    if header.body_size in (-1, 0):
        return b""
    if header.body_size < -1:
        raise ACPProtocolError(f"ACP response had invalid body_size {header.body_size}")
    body = _recv_exact(sock, header.body_size)
    checksum = _adler32_i32(body)
    if checksum != header.body_checksum:
        raise ACPProtocolError(
            f"ACP response body checksum mismatch: got {checksum:#x}, expected {header.body_checksum:#x}"
        )
    return body


def _read_property_result(sock: socket.socket) -> tuple[str | None, int, bytes]:
    name, flags, size = _parse_property_header(_recv_exact(sock, PROPERTY_HEADER.size))
    data = _recv_exact(sock, size)
    if flags & 1:
        if len(data) == 4:
            error_code = struct.unpack(">i", data)[0]
            raise ACPPropertyError(
                f"ACP property {name or '<end>'} failed with error_code {_format_error_code(error_code)}"
            )
        raise ACPPropertyError(f"ACP property {name or '<end>'} failed")
    return name, flags, data


def _iter_property_results_from_body(body: bytes) -> list[tuple[str | None, int, bytes]]:
    results: list[tuple[str | None, int, bytes]] = []
    offset = 0
    while offset < len(body):
        name, flags, data, offset = _parse_property_result_from_body(body, offset)
        results.append((name, flags, data))
    return results


def _read_property_results(sock: socket.socket, header: ACPMessageHeader) -> list[tuple[str | None, int, bytes]] | None:
    if header.body_size in (-1, 0):
        return None
    return _iter_property_results_from_body(_read_reply_body(sock, header))


def set_property_int(
    host: str,
    password: str,
    name: str,
    value: int,
    *,
    timeout: float = 25.0,
) -> None:
    payload = _compose_property_element(name, value)
    sock = _send_message(host, password, COMMAND_SETPROP, payload, timeout=timeout)
    try:
        header = _read_reply_header(sock, expected_command=COMMAND_SETPROP)
        results = _read_property_results(sock, header)
        if results is None:
            _read_property_result(sock)
        elif not results:
            raise ACPProtocolError("ACP set-property response body did not contain a property result")
    finally:
        sock.close()


def get_property_int(
    host: str,
    password: str,
    name: str,
    *,
    timeout: float = 25.0,
) -> int:
    payload = _compose_property_element(name, None)
    sock = _send_message(host, password, COMMAND_GETPROP, payload, flags=4, timeout=timeout)
    try:
        header = _read_reply_header(sock, expected_command=COMMAND_GETPROP)
        results = _read_property_results(sock, header)
        if results is None:
            while True:
                prop_name, _flags, data = _read_property_result(sock)
                if prop_name is None and data == b"\x00\x00\x00\x00":
                    raise ACPPropertyError(f"ACP property {name} was not returned")
                if prop_name == name:
                    if len(data) != 4:
                        raise ACPProtocolError(f"ACP property {name} returned {len(data)} bytes, expected 4")
                    return struct.unpack(">I", data)[0]
        for prop_name, _flags, data in results:
            if prop_name is None and data == b"\x00\x00\x00\x00":
                break
            if prop_name == name:
                if len(data) != 4:
                    raise ACPProtocolError(f"ACP property {name} returned {len(data)} bytes, expected 4")
                return struct.unpack(">I", data)[0]
        raise ACPPropertyError(f"ACP property {name} was not returned")
    finally:
        sock.close()


def read_identity(
    host: str,
    password: str,
    *,
    timeout: float = 25.0,
) -> ACPIdentity:
    return ACPIdentity(syap=get_property_int(host, password, "syAP", timeout=timeout))


def flash_firmware_bank(
    host: str,
    password: str,
    bank_name: str,
    payload: bytes,
    *,
    timeout: float = 120.0,
) -> ACPFlashResult:
    if bank_name == "primary":
        command = COMMAND_FLASH_PRIMARY
    elif bank_name == "secondary":
        command = COMMAND_FLASH_SECONDARY
    else:
        raise ACPProtocolError(f"unsupported flash bank name: {bank_name!r}")
    sock = _send_message(host, password, command, payload, timeout=timeout)
    try:
        header = _read_reply_header(sock, expected_command=command)
        return ACPFlashResult(command=command, reply_body=_read_reply_body(sock, header))
    finally:
        sock.close()


def set_dbug(
    host: str,
    password: str,
    value_hex: str | int,
    *,
    log: LogCallback | None = None,
    verbose: bool = False,
    timeout: float = 25.0,
) -> None:
    logger = _resolve_log(log, verbose)
    value = int(value_hex, 0) if isinstance(value_hex, str) else value_hex
    _emit(logger, f"Setting ACP dbug={value:#x} on {host}")
    set_property_int(host, password, "dbug", value, timeout=timeout)


def reboot(
    host: str,
    password: str,
    *,
    log: LogCallback | None = None,
    verbose: bool = False,
    timeout: float = 25.0,
) -> None:
    logger = _resolve_log(log, verbose)
    _emit(logger, f"Sending ACP reboot request to {host}")
    set_property_int(host, password, "acRB", 0, timeout=timeout)


def enable_ssh(
    host: str,
    password: str,
    *,
    reboot_device: bool = True,
    log: LogCallback | None = None,
    verbose: bool = False,
    timeout: float = 25.0,
) -> None:
    logger = _resolve_log(log, verbose)
    set_dbug(host, password, DBUG_SSH_VALUE, log=logger, timeout=timeout)
    if reboot_device:
        reboot(host, password, log=logger, timeout=timeout)
