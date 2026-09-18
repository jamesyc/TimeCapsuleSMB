"""The manager accepts only the current service helper's bind protocol."""
import shlex
import subprocess

import pytest

from timecapsulesmb.deploy.boot_assets import load_boot_asset_text


@pytest.mark.parametrize("status,accepted", [
    ("status=validated", True),
    ("status=incomplete reason=mode", True),
    ("status=cold-start", False),
    ("", False),
    ("status=incomplete reason=", False),
    ("status=unexpected", False),
])
def test_bind_probe_status_contract(tmp_path, status, accepted):
    common = tmp_path / "common.sh"
    common.write_text(load_boot_asset_text("common.sh"))
    service = tmp_path / "service"
    output = "127.0.0.1/8 ::1/128 192.0.2.1/24\n" + status + "\npolicy 1 0\n9 0 bridge0\n"
    service.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(output) + "\n")
    service.chmod(0o755)
    result = subprocess.run(["/bin/sh", "-c", f"""
set -eu
. {shlex.quote(str(common))}
TC_SERVICE_BIN={shlex.quote(str(service))}
if tc_probe_smb_bind_interfaces; then
    printf '%s|%s|%s\\n' "$TC_SMB_BIND_PROBE_TOKENS" "$TC_SMB_BIND_STATUS" "$TC_SMB_BIND_REASON"
else
    echo rejected
fi
"""], capture_output=True, text=True, check=True)
    if accepted:
        expected = "validated|" if status == "status=validated" else "incomplete|mode"
        assert result.stdout.strip() == "127.0.0.1/8 ::1/128 192.0.2.1/24|" + expected
    else:
        assert result.stdout.strip() == "rejected"
