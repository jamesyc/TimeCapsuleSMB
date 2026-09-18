"""The advertiser receives the exact shares selected for Samba, without a file."""
import shlex
import subprocess

import pytest

from tests import test_storage_runtime as runtime_tests
from tests.test_manager_diskd import manager_library


@pytest.mark.parametrize("diskless", [False, True])
def test_manager_passes_canonical_shares_as_individual_arguments(tmp_path, diskless):
    flash, memory, _, volumes = runtime_tests.StorageRuntimeTests().write_runtime_harness(tmp_path)
    library = manager_library(tmp_path)
    captured = tmp_path / "arguments"
    discovery = flash / "discoveryd"
    discovery.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\" >" + shlex.quote(str(captured)) + "\n")
    discovery.chmod(0o755)
    uuid = "12345678-1234-1234-1234-123456789012"
    name = "James's café $(touch PWNED); Backup"
    topology = "\n".join(
        f"wd0\t0\t{key}\t{volumes}/{key}\t{name}\t{uuid}"
        for key in ("dk2", "dk5")
    )
    result = subprocess.run(["/bin/sh", "-c", f"""
set -eu
. {shlex.quote(str(flash / 'common.sh'))}
. {shlex.quote(str(flash / 'tcapsulesmb.conf'))}
. {shlex.quote(str(library))}
tc_init_runtime_env
tc_log() {{ :; }}
is_volume_root_mounted() {{ return 0; }}
tc_volume_is_writable() {{ return 0; }}
tc_prepare_share_path() {{ printf '%s' "$2"; }}
tc_ensure_runtime_identity() {{ SMB_NETBIOS_NAME=CAPSULE; SMB_SERVER_STRING=Capsule; }}
tc_prepare_smbd_core_dir() {{ :; }}
tc_select_cache_directory() {{ echo "$RAM_VAR"; }}
TC_SMB_BIND_INTERFACES='127.0.0.1/8 ::1/128'
mkdir -p "$RAM_ETC" "$RAM_VAR"
tc_manager_build_share_state_from_topology {shlex.quote(topology)}
tc_generate_smb_conf_from_share_rows {shlex.quote(str(volumes / 'dk2/.samba4'))} "$manager_share_rows"
tc_manager_launch_discovery test 0 0 {int(diskless)}
wait "$TC_DISCOVERY_PID"
"""], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    args = captured.read_bytes().rstrip(b"\0").decode().split("\0")
    if diskless:
        assert args == ["--diskless"]
    else:
        # Duplicate volume names get the same suffix in SMB and ADisk.
        names = [name, name + " (dk5)"]
        assert args == ["--netbios-name", "CAPSULE"] + [
            arg
            for share, key in zip(names, ("dk2", "dk5"))
            for arg in ("--adisk-share", share, key, uuid, "0x82")
        ]
        conf = (memory / "samba4/etc/smb.conf").read_text()
        assert all(f"[{share}]\n" in conf for share in names)
    assert not (tmp_path / "PWNED").exists()
    assert not (memory / "samba4/var/adisk.tsv").exists()


def test_advertiser_reconciles_actual_launch_inputs_and_retries_failures(tmp_path):
    flash, _, _, _ = runtime_tests.StorageRuntimeTests().write_runtime_harness(tmp_path)
    library = manager_library(tmp_path)
    result = subprocess.run(["/bin/sh", "-c", f"""
set -eu
. {shlex.quote(str(flash / 'common.sh'))}
. {shlex.quote(str(library))}
manager_payload_ready=1
manager_share_rows='Data'
runtime_process_present_by_ucomm() {{ [ "$1" = "$DISCOVERY_PROC_NAME" ]; }}
launch_ok=0
launches=0
tc_manager_launch_current_discovery() {{ launches=$((launches + 1)); [ "$launch_ok" = 1 ]; }}
initial=$TC_MANAGER_LAST_DISCOVERY_SIGNATURE
if tc_manager_reconcile_discovery; then exit 10; fi
[ "$initial" = "$TC_MANAGER_LAST_DISCOVERY_SIGNATURE" ]
launch_ok=1
tc_manager_reconcile_discovery
tc_manager_reconcile_discovery
[ "$launches" = 2 ]
# mDNS instance naming is native, while Samba's canonical NetBIOS name is argv.
MDNS_INSTANCE_NAME=Renamed
SMB_NETBIOS_NAME=Renamed
tc_manager_reconcile_discovery
[ "$launches" = 3 ]
for manager_share_rows in Renamed '' Data; do
    tc_manager_reconcile_discovery
done
TC_ADISK_DISK_ADVF=0x83
tc_manager_reconcile_discovery
manager_payload_ready=0
tc_manager_reconcile_discovery
MDNS_DEBUG_LOGGING=1
tc_manager_reconcile_discovery
[ "$launches" = 9 ]
runtime_process_present_by_ucomm() {{ return 1; }}
tc_manager_reconcile_discovery
[ "$launches" = 10 ]
"""], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
