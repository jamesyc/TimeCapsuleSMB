"""The same ACP process engine serves scalar, multiline, password and MaSt reads."""
import os
from pathlib import Path
import subprocess

import pytest

from tests.native.build import ROOT, compile_native, instrumentation_flags


@pytest.fixture(scope="module")
def capture_tools(tmp_path_factory):
    work = tmp_path_factory.mktemp("acp-capture")
    acp = work / "acp"
    subprocess.run(["cc", str(ROOT / "tests/native/integration/acp_fixture.c"), "-o", str(acp)], check=True)
    flags = [f'-DTC_ACP_PATH="{acp}"', "-DTC_ACP_TIMEOUT_SECONDS=5"]
    driver = work / "capture"
    subprocess.run(["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", *instrumentation_flags(), *flags,
                    "-I", str(ROOT / "build/native/common"), str(ROOT / "tests/native/unit/test_acp_capture.c"),
                    str(ROOT / "build/native/common/acp.c"), "-o", str(driver)], check=True)
    return driver, compile_native("service", work / "service", flags=flags), compile_native("discovery", work / "mdns", flags=flags)


def raw_env(tmp_path, data, *, key="*", exit_code=0):
    path = tmp_path / "answer"
    path.write_bytes(data)
    return {**os.environ, "TC_TEST_ACP_MODE": "file", "TC_TEST_ACP_FILE": str(path),
            "TC_TEST_ACP_KEY": key, "TC_TEST_ACP_EXIT": str(exit_code)}


@pytest.mark.parametrize("async_driver", [0, 1])
@pytest.mark.parametrize("multiline", [0, 1])
@pytest.mark.parametrize("trim", [0, 1])
@pytest.mark.parametrize("data", [b"  first\r\n  second\t\n\n", b"\nnext", b" \t\r\n", b"", " Café\n中 ".encode()])
def test_capture_options(capture_tools, tmp_path, async_driver, multiline, trim, data):
    result = subprocess.run([str(capture_tools[0]), str(multiline), str(trim), "256", str(async_driver), "10000"],
                            env=raw_env(tmp_path, data), capture_output=True, check=True, timeout=15)
    header, output = result.stdout.split(b"\n", 1)
    expected = data if multiline else data.split(b"\n", 1)[0]
    if trim: expected = expected.strip(b" \t\r\n\v\f")
    assert output == expected, (header, result.stderr)
    assert header == f"0 {len(expected)} 0 1".encode()


@pytest.mark.parametrize("data,cap,multiline,expected", [
    (b"abc", 4, 1, 0), (b"abcd", 4, 1, -2), (b"  x  ", 4, 1, -2),
    (b"a\0b", 20, 1, -2), (b"a\n\0b", 20, 0, 0), (b"a\n\0b", 20, 1, -2),
    (b"", 0, 1, -2), (b"a\n" + b"x" * 65536, 4, 0, 0),
])
def test_capture_bounds_and_discarded_tail(capture_tools, tmp_path, data, cap, multiline, expected):
    result = subprocess.run([str(capture_tools[0]), str(multiline), "1", str(cap), "1", "4000"],
                            env=raw_env(tmp_path, data), capture_output=True, check=True, timeout=6)
    header, output = result.stdout.split(b"\n", 1)
    assert int(header.split()[0]) == expected
    if expected: assert output == b"" and header.split()[1] == b"0"


def test_capture_keeps_child_exit_and_aborts_unstarted_requests(capture_tools, tmp_path):
    for budget, status, exit_status in [(2000, -1, 6), (0, -2, -1)]:
        result = subprocess.run([str(capture_tools[0]), "1", "0", "256", "0", str(budget)],
                                env=raw_env(tmp_path, b"untrusted\n", exit_code=6), capture_output=True, timeout=5)
        header, output = result.stdout.split(b"\n", 1)
        assert list(map(int, header.split()[:3])) == [status, 0, exit_status]
        assert output == b""


@pytest.mark.parametrize("data", [b"password\n", b"  password \t\n", b"first\nsecond\n\n", b"password\r\n",
                                 "密碼😀\n".encode(), b"x" * 4095 + b"\n"])
def test_device_hash_matches_original_transport(capture_tools, tmp_path, data):
    service = str(capture_tools[1])
    actual = subprocess.run([service, "--print-device-nt-hash"], env=raw_env(tmp_path, data, key="syPW"), capture_output=True, timeout=5)
    expected = subprocess.run([service, "--print-nt-hash-from-stdin"], input=data.rstrip(b"\n") + b"\n", capture_output=True, timeout=5)
    assert actual.returncode == expected.returncode == 0
    assert actual.stdout == expected.stdout


@pytest.mark.parametrize("data", [b"", b"\n", b"\0", b"x" * 4096 + b"\n", b"x" * 8193, b"\xff"])
def test_device_hash_rejects_invalid_or_oversized_input(capture_tools, tmp_path, data):
    result = subprocess.run([str(capture_tools[1]), "--print-device-nt-hash"], env=raw_env(tmp_path, data, key="syPW"), capture_output=True, timeout=5)
    assert result.returncode != 0 and result.stdout == b""


@pytest.mark.parametrize("data,success", [(b"MaSt = (\n  item\n);", True), (b"x" * 65536, True),
                                         (b"x" * 65537, False), (b"\n\n", False)])
def test_mast_command_preserves_all_text(capture_tools, tmp_path, data, success):
    result = subprocess.run([str(capture_tools[2]), "--print-mast"], env=raw_env(tmp_path, data), capture_output=True, timeout=5)
    assert (result.returncode == 0) == success
    assert result.stdout == (data if success else b"")


@pytest.mark.parametrize("value,model", [(b"0x77\n", "TimeCapsule8,119"), (b"106\n", "TimeCapsule6,106"),
                                       (b"unknown\n", "TimeCapsule8,119")])
def test_samba_identity_uses_observed_model(capture_tools, tmp_path, value, model):
    result = subprocess.run([str(capture_tools[1]), "--print-samba-identity"], env=raw_env(tmp_path, value, key="syAP"), capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    header, netbios, server, actual_model = result.stdout.splitlines()
    assert header == "samba-identity 1" and netbios
    assert server == "Test Capsule" and actual_model == model
