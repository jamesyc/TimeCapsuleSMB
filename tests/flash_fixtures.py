"""Synthetic flash banks and Apple firmware templates for the flash tests."""
from __future__ import annotations

import functools
import re
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from timecapsulesmb.basebinary import (
    DEFAULT_BASEBINARY_KEYS,
    BasebinaryHeader,
    BasebinaryKey,
    compose_basebinary,
)
from timecapsulesmb.flash import STOCK_LOGIN_NETBSD4_DUMMY, find_footer, inspect_flash_banks
from timecapsulesmb.flash_payloads import AcpFlashPayload
from timecapsulesmb.flash_workflow import plan_restore_apple, require_primary_patch_ready
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


# Full-size banks for the secondary bank rewrite (tests/test_flash_secondary.py).

FULL_BANK_SIZE = 0x700000
FULL_BANK_OS_RELEASE = "4.0_STABLE"


def make_full_bank(
    release: bytes = b"NetBSD 4.0_STABLE #0: current",
    *,
    login: bytes = STOCK_LOGIN_NETBSD4_DUMMY,
    size: int = FULL_BANK_SIZE,
) -> bytes:
    """A bank as Apple lays it out: loader stub, gzip member, 0xFF, footer at bank end - 32."""
    decompressed = b"kernel " + release + b"\n" + (b"A" * 128) + login + (b"\x00" * 64)
    prefix = b"BOOT" + (b"\x00" * 16) + make_gzip_member(decompressed) + b"\x00\x00"
    bank = bytearray(b"\xff" * size)
    bank[: len(prefix)] = prefix
    bank[size - 32 : size - 24] = struct.pack(">II", zlib.adler32(prefix) & 0xFFFFFFFF, len(prefix))
    return bytes(bank)


def acpd_checksum(bank: bytes) -> int:
    """What ACPd's cks1/cks2 return: the adler32 it computes up to the footer slot's end offset.

    ACPd returns what it computed even when that does not match the footer, and
    0 when the footer slot at bank end - 32 holds no plausible end offset.
    """
    _checksum, end_offset = struct.unpack_from(">II", bank, len(bank) - 32)
    if end_offset == 0 or end_offset > len(bank) - 32:
        return 0
    return zlib.adler32(bank[:end_offset]) & 0xFFFFFFFF


def without_footer(bank: bytes) -> bytes:
    """As every field report reads: "expected exactly one valid footer, found 0"."""
    return bank[:-32] + b"\x00" * 8 + bank[-24:]


def with_flipped_byte(bank: bytes, offset: int = 5) -> bytes:
    """The bank with one byte inside its checksummed range changed: the footer no longer matches."""
    return bank[:offset] + bytes([bank[offset] ^ 0x01]) + bank[offset + 1 :]


class FakeFlashDevice:
    """Raw flash banks behind SSH and ACP, as the kernel and ACPd behave.

    The write command runs as the shell would: `flashctl <dev> erase` fills that
    device with 0xFF, then `dd of=<dev>` writes stdin to it, cut off at the end
    of the bank. Any other command, or one with `unlock`, is recorded so a test
    can fail on it. cks1/cks2 are computed from the banks as ACPd computes them.
    """

    COMMAND = re.compile(r"^/sbin/flashctl (\S+) erase </dev/null && /bin/dd of=(\S+) ibs=4096 obs=65536$")

    def __init__(self, *, primary: bytes, secondary: bytes) -> None:
        self.banks = {"/dev/rflash0.raw": primary, "/dev/rflash1.raw": secondary}
        self.commands: list[tuple[str, int]] = []
        self.erased: list[str] = []
        self.unlocked = False
        self.reads = 0
        self.write_protected = False
        self.corrupt_write = False
        self.flaky_reads = 0
        self.change_primary_on_write = False
        self.write_error: Exception | None = None
        self.cks_error: Exception | None = None

    @property
    def primary(self) -> bytes:
        return self.banks["/dev/rflash0.raw"]

    @property
    def secondary(self) -> bytes:
        return self.banks["/dev/rflash1.raw"]

    def run_write(self, connection, command: str, *, input_bytes: bytes, timeout: int):
        self.commands.append((command, timeout))
        if "unlock" in command:
            self.unlocked = True
        if self.write_error is not None:
            raise self.write_error
        match = self.COMMAND.match(command)
        if match is None:
            raise AssertionError(f"unexpected flash command: {command}")
        erase_device, dd_device = match.groups()
        if self.change_primary_on_write:
            self.banks["/dev/rflash0.raw"] = make_full_bank(b"NetBSD 4.0_STABLE #0: changed")
        if self.write_protected:
            return None
        self.erased.append(erase_device)
        self.banks[erase_device] = b"\xff" * len(self.banks[erase_device])
        data = bytearray(input_bytes[: len(self.banks[dd_device])])
        if self.corrupt_write:
            data[4096] ^= 0x01
        self.banks[dd_device] = bytes(data) + self.banks[dd_device][len(data) :]
        return None

    def dump(self, connection, device: str, log=None) -> bytes:
        self.reads += 1
        bank = self.banks[device]
        if self.flaky_reads:
            self.flaky_reads -= 1
            return bank[:10] + bytes([bank[10] ^ 0x01]) + bank[11:]
        return bank

    def get_property(self, host: str, password: str, name: str) -> int:
        if self.cks_error is not None:
            raise self.cks_error
        return acpd_checksum(self.primary if name == "cks1" else self.secondary)


# Full banks are built on first use, so importing this module stays cheap.
@functools.cache
def full_primary() -> bytes:
    return make_full_bank()


@functools.cache
def full_old_secondary() -> bytes:
    return make_full_bank(b"NetBSD 4.0_BETA2 #0: old")


@functools.cache
def full_broken_secondary() -> bytes:
    return without_footer(full_old_secondary())


@functools.cache
def newest_firmware_prefix() -> bytes:
    """The newest Apple firmware for the model: at least 3 MiB, as ACPd requires."""
    return make_full_bank(b"NetBSD 4.0_STABLE #0: newest")[:100] + bytes(range(256)) * (13 * 1024)


def newest_firmware_payload(prefix: bytes | None = None) -> AcpFlashPayload:
    if prefix is None:
        prefix = newest_firmware_prefix()
    return AcpFlashPayload(
        data=b"basebinary",
        expected_prefix=prefix,
        expected_login_classification="stock",
        template_source="catalog",
        template_path=Path("/tmp/7.8.1.basebinary"),
        template_product_id="116",
        template_version="7.8.1",
        template_sha256="template-sha",
        payload_sha256="payload-sha",
        key_id="k30a",
        inner_model=116,
        inner_version=0x07818000,
        inner_payload_size=len(prefix),
    )


def inspect_full_banks(
    primary: bytes | None = None,
    secondary: bytes | None = None,
    *,
    cks1: int | None = None,
    cks2: int | None = None,
):
    """Inspect two banks (by default a valid primary and a secondary with no footer)
    with the cks1/cks2 ACPd would report for them, unless given."""
    primary = full_primary() if primary is None else primary
    secondary = full_broken_secondary() if secondary is None else secondary
    return inspect_flash_banks(
        primary_data=primary,
        secondary_data=secondary,
        cks1=acpd_checksum(primary) if cks1 is None else cks1,
        cks2=acpd_checksum(secondary) if cks2 is None else cks2,
        os_release=FULL_BANK_OS_RELEASE,
    )


def plan_full_restore(inspection, *, syap: str = "116", firmware_version: str | None = None):
    """plan_restore_apple with the newest-firmware download and the restore payload stubbed."""
    with mock.patch("timecapsulesmb.flash_workflow.build_download_payload_for_syap",
                    return_value=newest_firmware_payload()) as download:
        with mock.patch("timecapsulesmb.flash_workflow.build_restore_payload_for_bank",
                        return_value=newest_firmware_payload()) as restore:
            plan = plan_restore_apple(inspection, syap=syap, firmware_template=None, firmware_version=firmware_version)
    return plan, download, restore
