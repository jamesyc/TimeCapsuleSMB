from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib
import struct
import zlib


MAGIC_PREFIX = b"APPLE-FIRMWARE\x00"
HEADER_FORMAT = struct.Struct(">15sB2I4BI")
CHECKSUM_FORMAT = struct.Struct(">I")
ENCRYPTED_FLAG = 0x02
AES_BLOCK_SIZE = 16
AES_CHUNK_SIZE = 0x8000
PYCRYPTODOME_BOOTSTRAP_MESSAGE = (
    "Python package pycryptodome is required for basebinary firmware encryption. "
    "Run `./tcapsule bootstrap` to install it, then rerun `.venv/bin/tcapsule flash`."
)


class BasebinaryError(RuntimeError):
    """Raised when an Apple basebinary container cannot be parsed or composed."""


@dataclass(frozen=True)
class BasebinaryKey:
    key_id: str
    stored_key: bytes
    provenance: str = ""

    @classmethod
    def from_hex(cls, key_id: str, value: str, *, provenance: str = "") -> "BasebinaryKey":
        try:
            stored_key = bytes.fromhex(value)
        except ValueError as exc:
            raise BasebinaryError(f"invalid basebinary key hex for {key_id}") from exc
        if len(stored_key) != AES_BLOCK_SIZE:
            raise BasebinaryError(f"basebinary key {key_id} must be 16 bytes, got {len(stored_key)}")
        return cls(key_id=key_id, stored_key=stored_key, provenance=provenance)

    @property
    def derived_key(self) -> bytes:
        return derive_basebinary_key(self.stored_key)


@dataclass(frozen=True)
class BasebinaryHeader:
    iv_suffix: int
    model: int
    version: int
    byte_0x18: int
    byte_0x19: int
    byte_0x1a: int
    flags: int
    unk_0x1c: int

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & ENCRYPTED_FLAG)

    @property
    def iv(self) -> bytes:
        return MAGIC_PREFIX + bytes((self.iv_suffix,))

    def to_bytes(self) -> bytes:
        return HEADER_FORMAT.pack(
            MAGIC_PREFIX,
            self.iv_suffix,
            self.model,
            self.version,
            self.byte_0x18,
            self.byte_0x19,
            self.byte_0x1a,
            self.flags,
            self.unk_0x1c,
        )


@dataclass(frozen=True)
class BasebinaryContainer:
    header: BasebinaryHeader
    payload: bytes
    checksum: int
    key: BasebinaryKey | None

    @property
    def encrypted(self) -> bool:
        return self.header.encrypted

    @property
    def key_id(self) -> str | None:
        return None if self.key is None else self.key.key_id


@dataclass(frozen=True)
class NestedBasebinary:
    outer: BasebinaryContainer
    inner: BasebinaryContainer


DEFAULT_BASEBINARY_KEYS: tuple[BasebinaryKey, ...] = (
    BasebinaryKey.from_hex(
        "observed-acpd-78100",
        "513c1ca5bf035127335f7c2596aa20aa",
        provenance="extracted from a NetBSD4 ACPd binary and validated by checksum against Apple product 106 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-airport5-105-78100",
        "87f52f57c573e87499b6d69c8e4bcb8b",
        provenance="extracted from an AirPort5,105 ACPd binary and validated by checksum against Apple product 105 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-timecapsule6-109-78100",
        "cfb15a151a3998b983a5a48aaa859e80",
        provenance="extracted from a TimeCapsule6,109 ACPd binary and validated by checksum against Apple product 109 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-k30a-78100",
        "c025fefa2320b0e985dfac106694db4a",
        provenance="extracted from a TimeCapsule6,113 ACPd binary and validated by checksum against Apple product 113 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-k10a-78100",
        "a66e263e7b751242ac7fa0c90951ed08",
        provenance="extracted from an AirPort5,114 A1354 ACPd binary and validated by checksum against Apple product 114 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-k30b-78100",
        "9d1259ee89f28a2ccfa64697adbb4193",
        provenance="extracted from a TimeCapsule6,116 ACPd binary and validated by checksum against Apple product 116 firmware",
    ),
    BasebinaryKey.from_hex(
        "observed-j28-79100",
        "b19937ddcb78b3f151e4e0b48198e6a7",
        provenance="extracted from a TimeCapsule8,119 ACPd binary and validated by checksum against Apple product 119 firmware",
    ),
    BasebinaryKey.from_hex(
        "airpyrt-107",
        "5249c351028bf1fd2bd1849e28b23f24",
        provenance="published by airpyrt-tools",
    ),
    BasebinaryKey.from_hex(
        "airpyrt-108",
        "bb7deb0970d8ee2e00fa46cb1c3c098e",
        provenance="published by airpyrt-tools",
    ),
    BasebinaryKey.from_hex(
        "airpyrt-115",
        "1075e806f4770cd4763bd285a64e9174",
        provenance="published by airpyrt-tools",
    ),
    BasebinaryKey.from_hex(
        "airpyrt-120",
        "688cdd3b1b6bdda207b6cec2735292d2",
        provenance="published by airpyrt-tools",
    ),
)


def derive_basebinary_key(stored_key: bytes) -> bytes:
    if len(stored_key) != AES_BLOCK_SIZE:
        raise BasebinaryError(f"basebinary key must be 16 bytes, got {len(stored_key)}")
    return bytes(value ^ (index + 0x19) for index, value in enumerate(stored_key))


@lru_cache(maxsize=1)
def _load_aes_module() -> object:
    try:
        return importlib.import_module("Crypto.Cipher.AES")
    except Exception as exc:
        raise BasebinaryError(PYCRYPTODOME_BOOTSTRAP_MESSAGE) from exc


def _aes_cbc_cipher(key: bytes, iv: bytes) -> object:
    aes = _load_aes_module()
    return aes.new(key, aes.MODE_CBC, iv)


def _crypt_basebinary_payload(data: bytes, *, key: bytes, iv: bytes, decrypt: bool) -> bytes:
    output = bytearray()
    for offset in range(0, len(data), AES_CHUNK_SIZE):
        chunk = data[offset : offset + AES_CHUNK_SIZE]
        cipher = _aes_cbc_cipher(key, iv)
        encrypted_length = len(chunk) - (len(chunk) % AES_BLOCK_SIZE)
        if encrypted_length:
            block_data = chunk[:encrypted_length]
            if decrypt:
                output.extend(cipher.decrypt(block_data))
            else:
                output.extend(cipher.encrypt(block_data))
        output.extend(chunk[encrypted_length:])
    return bytes(output)


def decrypt_basebinary_payload(data: bytes, *, key: BasebinaryKey, iv: bytes) -> bytes:
    return _crypt_basebinary_payload(data, key=key.derived_key, iv=iv, decrypt=True)


def encrypt_basebinary_payload(data: bytes, *, key: BasebinaryKey, iv: bytes) -> bytes:
    return _crypt_basebinary_payload(data, key=key.derived_key, iv=iv, decrypt=False)


def _adler32(data: bytes) -> int:
    return zlib.adler32(data) & 0xFFFFFFFF


def parse_basebinary_header(data: bytes) -> BasebinaryHeader:
    if len(data) != HEADER_FORMAT.size:
        raise BasebinaryError(f"basebinary header must be {HEADER_FORMAT.size} bytes, got {len(data)}")
    magic, iv_suffix, model, version, byte_0x18, byte_0x19, byte_0x1a, flags, unk_0x1c = HEADER_FORMAT.unpack(data)
    if magic != MAGIC_PREFIX:
        raise BasebinaryError("bad basebinary header magic")
    return BasebinaryHeader(
        iv_suffix=iv_suffix,
        model=model,
        version=version,
        byte_0x18=byte_0x18,
        byte_0x19=byte_0x19,
        byte_0x1a=byte_0x1a,
        flags=flags,
        unk_0x1c=unk_0x1c,
    )


def parse_basebinary(data: bytes, *, keys: tuple[BasebinaryKey, ...] = DEFAULT_BASEBINARY_KEYS) -> BasebinaryContainer:
    minimum_size = HEADER_FORMAT.size + CHECKSUM_FORMAT.size
    if len(data) < minimum_size:
        raise BasebinaryError(f"not enough data to parse basebinary: got {len(data)}, need at least {minimum_size}")

    header_bytes = data[: HEADER_FORMAT.size]
    header = parse_basebinary_header(header_bytes)
    raw_payload = data[HEADER_FORMAT.size : -CHECKSUM_FORMAT.size]
    stored_checksum = CHECKSUM_FORMAT.unpack(data[-CHECKSUM_FORMAT.size :])[0]

    if not header.encrypted:
        checksum = _adler32(header_bytes + raw_payload)
        if checksum != stored_checksum:
            raise BasebinaryError(f"bad basebinary checksum: got 0x{checksum:08x}, expected 0x{stored_checksum:08x}")
        return BasebinaryContainer(header=header, payload=raw_payload, checksum=stored_checksum, key=None)

    if not keys:
        raise BasebinaryError(f"basebinary model {header.model} is encrypted, but no candidate keys were provided")

    for key in keys:
        payload = decrypt_basebinary_payload(raw_payload, key=key, iv=header.iv)
        checksum = _adler32(header_bytes + payload)
        if checksum == stored_checksum:
            return BasebinaryContainer(header=header, payload=payload, checksum=stored_checksum, key=key)

    raise BasebinaryError(f"no candidate basebinary key validated checksum for model {header.model}")


def compose_basebinary(header: BasebinaryHeader, payload: bytes, *, key: BasebinaryKey | None = None) -> bytes:
    header_bytes = header.to_bytes()
    checksum = _adler32(header_bytes + payload)
    if header.encrypted:
        if key is None:
            raise BasebinaryError("cannot compose encrypted basebinary without the validated key")
        stored_payload = encrypt_basebinary_payload(payload, key=key, iv=header.iv)
    else:
        stored_payload = payload
    return header_bytes + stored_payload + CHECKSUM_FORMAT.pack(checksum)


def is_basebinary(data: bytes) -> bool:
    return len(data) >= HEADER_FORMAT.size and data[: len(MAGIC_PREFIX)] == MAGIC_PREFIX


def parse_nested_basebinary(data: bytes, *, keys: tuple[BasebinaryKey, ...] = DEFAULT_BASEBINARY_KEYS) -> NestedBasebinary:
    outer = parse_basebinary(data, keys=keys)
    if not is_basebinary(outer.payload):
        raise BasebinaryError("outer basebinary payload is not another basebinary container")
    inner = parse_basebinary(outer.payload, keys=keys)
    return NestedBasebinary(outer=outer, inner=inner)


def compose_nested_basebinary(template: NestedBasebinary, inner_payload: bytes) -> bytes:
    inner_bytes = compose_basebinary(template.inner.header, inner_payload, key=template.inner.key)
    return compose_basebinary(template.outer.header, inner_bytes, key=template.outer.key)
