"""Synthetic flash banks and Apple firmware templates for the flash tests."""
from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

from timecapsulesmb.basebinary import (
    DEFAULT_BASEBINARY_KEYS,
    BasebinaryHeader,
    BasebinaryKey,
    compose_basebinary,
)
from timecapsulesmb.flash import STOCK_LOGIN_NETBSD4_DUMMY, find_footer, inspect_flash_banks
from timecapsulesmb.flash_workflow import require_primary_patch_ready
from timecapsulesmb.services.flash import FlashInputs


def make_gzip_member(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=1, wbits=16 + zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


class FastFakeZopfliGzip:
    """Stands in for zopfli: a fast ordinary gzip member of the same data."""

    @staticmethod
    def compress(data: bytes, **_kwargs) -> bytes:
        return make_gzip_member(data)


@contextmanager
def zopfli_available() -> Iterator[None]:
    with mock.patch("timecapsulesmb.flash.require_python_module", return_value=None):
        with mock.patch("timecapsulesmb.flash._load_zopfli_gzip", return_value=FastFakeZopfliGzip):
            yield


def make_bank(
    *,
    login: bytes = STOCK_LOGIN_NETBSD4_DUMMY,
    release: bytes = b"NetBSD 4.0 #0: test",
    extra_gzip_magic: bytes = b"",
) -> bytes:
    """A kernel bank: a gzip member holding the release and LOGIN, then Apple's footer."""
    decompressed = b"kernel " + release + b"\n" + (b"A" * 128) + login + (b"\x00" * 64)
    gz = make_gzip_member(decompressed)
    prefix = b"BOOT" + extra_gzip_magic + (b"\x00" * 16)
    body = prefix + gz + b"\x00\x00"
    end_offset = len(body)
    checksum = zlib.adler32(body) & 0xFFFFFFFF
    return body + (b"\xff" * 16) + struct.pack(">II", checksum, end_offset) + (b"\xff" * 24)


def bank_checksum(bank: bytes) -> int:
    return find_footer(bank).checksum


def bank_end_offset(bank: bytes) -> int:
    return find_footer(bank).end_offset


def flash_inputs(
    primary: bytes,
    secondary: bytes,
    *,
    cks1: int | None = None,
    cks2: int | None = None,
    syap: str = "113",
    live_login: bytes = STOCK_LOGIN_NETBSD4_DUMMY,
) -> FlashInputs:
    """What reading both banks, their ACP checksums and the live LOGIN returns."""
    return FlashInputs(
        primary=primary,
        secondary=secondary,
        cks1=bank_checksum(primary) if cks1 is None else cks1,
        cks2=bank_checksum(secondary) if cks2 is None else cks2,
        syap=syap,
        live_login=live_login,
    )


def firmware_template(
    bank: bytes,
    *,
    product_id: int = 113,
    version: int = 0x07818000,
    key: BasebinaryKey | None = None,
) -> bytes:
    """An Apple basebinary whose inner payload is this bank up to its footer."""
    selected_key = key or next(key for key in DEFAULT_BASEBINARY_KEYS if key.key_id == "observed-k30a-78100")
    inner_header = BasebinaryHeader(
        iv_suffix=0x2E,
        model=product_id,
        version=version,
        byte_0x18=0,
        byte_0x19=0,
        byte_0x1a=0,
        flags=0x02,
        unk_0x1c=0,
    )
    outer_header = BasebinaryHeader(
        iv_suffix=0x2E,
        model=product_id,
        version=version,
        byte_0x18=0,
        byte_0x19=0,
        byte_0x1a=0,
        flags=0,
        unk_0x1c=0,
    )
    inner = compose_basebinary(inner_header, bank[: bank_end_offset(bank)], key=selected_key)
    return compose_basebinary(outer_header, inner)


def patched_bank(bank: bytes, secondary: bytes | None = None) -> bytes:
    """The primary bank the patch builder produces from this stock bank."""
    fallback_secondary = secondary or make_bank(release=b"NetBSD 4.0_BETA2 #0: old")
    with zopfli_available():
        inspection = inspect_flash_banks(
            primary_data=bank,
            secondary_data=fallback_secondary,
            cks1=bank_checksum(bank),
            cks2=bank_checksum(fallback_secondary),
            os_release="4.0_STABLE",
            build_primary_patch_candidate=True,
        )
    active = require_primary_patch_ready(inspection)
    assert active.patch is not None
    return active.patch.target_bank
