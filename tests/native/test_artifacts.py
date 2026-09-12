import struct

import pytest

from tests.native.build import ROOT, binary_name


@pytest.mark.parametrize("target", ["mdns", "nbns", "service", "telemetry"])
@pytest.mark.parametrize("suffix,byte_order", [("", 1), ("-netbsd4le", 1), ("-netbsd4be", 2)])
def test_device_binary_is_single_static_arm_elf(target, suffix, byte_order):
    data = (ROOT / "bin" / (target + suffix) / binary_name(target)).read_bytes()
    assert data[:4] == b"\x7fELF"
    assert data[4] == 1  # ELF32
    assert data[5] == byte_order
    endian = "<" if byte_order == 1 else ">"
    executable, machine = struct.unpack_from(endian + "HH", data, 16)
    assert executable == 2 and machine == 40  # ET_EXEC, EM_ARM
    offset = struct.unpack_from(endian + "I", data, 28)[0]
    stride, count = struct.unpack_from(endian + "HH", data, 42)
    assert stride >= 32 and count > 0
    headers = [struct.unpack_from(endian + "I", data, offset + i * stride)[0] for i in range(count)]
    assert 1 in headers  # PT_LOAD
    assert 2 not in headers and 3 not in headers  # No PT_DYNAMIC or PT_INTERP
    # NetBSD requires its ABI note even for a fully static executable.
    assert b"NetBSD\0" in data
