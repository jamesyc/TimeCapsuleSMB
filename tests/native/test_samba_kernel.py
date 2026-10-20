"""Exercise production kernel setup, replacing only NetBSD's sysctl syscall."""
import errno
import os
import subprocess

import pytest

from tests.native.build import ROOT, compile_modules


@pytest.fixture(scope="module", params=[(), ("-D__ARMEB__",)], ids=["le", "be"])
def kernel_tool(tmp_path_factory, request):
    binary = tmp_path_factory.mktemp("samba-kernel") / "kernel"
    compile_modules(binary, ["native/samba/kernel.c", "native/common/log.c"],
                    flags=("-I", str(ROOT / "build/native"),
                           "-include", str(ROOT / "tests/native/unit/sysctl_stub.h"), *request.param),
                    extra_sources=(ROOT / "tests/native/unit/test_samba_kernel.c",))
    return binary


@pytest.mark.parametrize("case", ["netbsd6", "netbsd4", "larger", "same", "restart"])
def test_kernel_policy_through_production_mib_reads_and_writes(kernel_tool, case):
    result = subprocess.run([str(kernel_tool), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not result.stderr


@pytest.mark.parametrize("operation,node,error", [
    *[(operation, node, error) for operation, error in [("read-fail", errno.EIO), ("write-fail", errno.EACCES)]
      for node in range(5)],
    ("query-size-fail", 3, errno.EIO), ("query-fill-fail", 3, errno.EIO),
    ("missing", 3, errno.ENOENT), ("missing", 4, errno.ENOENT),
])
def test_sysctl_failures_are_logged_and_other_settings_still_apply(kernel_tool, operation, node, error):
    names = ["kern.sbmax", "kern.maxfiles", "kern.maxproc", "net.inet.tcp.sendbuf_auto", "net.inet.tcp.sendbuf_max"]
    wanted = [2359296, 4096, 90, 1, 2097152]
    result = subprocess.run([str(kernel_tool), operation, str(node)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("could not set") == 1
    assert f"manager: could not set {names[node]} to {wanted[node]}: {os.strerror(error)}" in result.stderr
