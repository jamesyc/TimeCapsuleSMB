"""Manager-side retry of the diskd loopback relaunch (guide B.8 failure
contract, review finding 6). Uses the production function bodies with the
process probes stubbed."""
import shlex
import subprocess

from timecapsulesmb.deploy.boot_assets import load_boot_asset_text


def manager_library(tmp_path):
    text = load_boot_asset_text("manager.sh")
    text = text[text.index("tc_manager_debug_log() {"):text.index("\ntc_prepare_ram_root\n")]
    library = tmp_path / "manager-functions.sh"
    library.write_text(text)
    return library


def run(tmp_path, script):
    library = manager_library(tmp_path)
    result = subprocess.run(["/bin/sh", "-c", f"set -eu\n. {shlex.quote(str(library))}\n" + script],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


PREAMBLE = '''
tc_log() { printf 'log %s\\n' "$*"; }
tc_manager_debug_log() { printf 'debug %s\\n' "$*"; }
tc_now_seconds() { echo "$FAKE_NOW"; }
tc_apple_diskd_state() { cat "$STATE"; }
tc_relaunch_diskd_loopback() { echo relaunch; [ "$RELAUNCH_OK" = 1 ] && echo loopback >"$STATE"; [ "$RELAUNCH_OK" = 1 ]; }
'''


def test_loopback_diskd_needs_nothing(tmp_path):
    state = tmp_path / "state"
    state.write_text("loopback")
    out = run(tmp_path, PREAMBLE + f'''
STATE={shlex.quote(str(state))}; FAKE_NOW=1000; RELAUNCH_OK=1
tc_manager_reconcile_diskd
echo "retry_at=[${{TC_MANAGER_DISKD_RETRY_AT:-}}]"
''')
    assert out == ["retry_at=[]"]


def test_stray_or_absent_diskd_is_relaunched_every_pass_with_a_hold_after_failure(tmp_path):
    state = tmp_path / "state"
    state.write_text("acpd")
    out = run(tmp_path, PREAMBLE + f'''
STATE={shlex.quote(str(state))}; RELAUNCH_OK=0
FAKE_NOW=1000; tc_manager_reconcile_diskd
FAKE_NOW=1030; tc_manager_reconcile_diskd
FAKE_NOW=1299; tc_manager_reconcile_diskd
RELAUNCH_OK=1
FAKE_NOW=1300; tc_manager_reconcile_diskd
echo "retry_at=[${{TC_MANAGER_DISKD_RETRY_AT:-}}] state=$(cat "$STATE")"
FAKE_NOW=1330; tc_manager_reconcile_diskd
echo absent >"$STATE"
FAKE_NOW=1360; tc_manager_reconcile_diskd
echo "state=$(cat "$STATE")"
''')
    assert out == [
        "log manager diskd: state=acpd; Apple's SMB/AFP names may be on the LAN, retrying the loopback relaunch",
        "relaunch",
        "log manager diskd: relaunch failed; next attempt in 300s (doctor reports the degraded state)",
        "debug manager diskd: state=acpd; relaunch retry deferred",
        "debug manager diskd: state=acpd; relaunch retry deferred",
        "log manager diskd: state=acpd; Apple's SMB/AFP names may be on the LAN, retrying the loopback relaunch",
        "relaunch",
        "retry_at=[] state=loopback",
        # ours died later: relaunched at once, no hold from the earlier failure
        "log manager diskd: state=absent; Apple's SMB/AFP names may be on the LAN, retrying the loopback relaunch",
        "relaunch",
        "state=loopback",
    ]


def test_disk_step_runs_the_diskd_retry_before_reading_the_topology(tmp_path):
    """A dead diskd makes MaSt read as empty and the disk refresh would tear
    Samba down (seen on the NetBSD 4 device), so the relaunch comes first."""
    state = tmp_path / "state"
    state.write_text("absent")
    out = run(tmp_path, PREAMBLE + f'''
STATE={shlex.quote(str(state))}; RELAUNCH_OK=1; FAKE_NOW=1000
manager_iteration_id=7
DISCOVERY_PROC_NAME=discoveryd
runtime_process_present_by_ucomm() {{ return 1; }}
tc_manager_reconcile_disk_state() {{ echo "disk-reconcile state=$(cat "$STATE")"; }}
tc_manager_log_step_end() {{ :; }}
tc_manager_run_disk_step
''')
    assert out == [
        "debug manager pass 7 step=disk start",
        "log manager diskd: state=absent; Apple's SMB/AFP names may be on the LAN, retrying the loopback relaunch",
        "relaunch",
        "disk-reconcile state=loopback",
    ]


def test_warm_smbd_reload_failure_falls_back_to_restart(tmp_path):
    out = run(tmp_path, '''
tc_log() { printf 'log %s\n' "$*"; }
runtime_process_present_by_ucomm() { echo "present $1"; return 0; }
tc_smbd_bound_tcp_445() { echo bound; return 0; }
tc_reload_smbd_config() { echo reload; return 1; }
stop_runtime_process_by_ucomm() { echo "stop $*"; return 0; }
tc_manager_start_smbd_if_needed() { echo start; return 0; }
tc_manager_commit_smbd_runtime_apply() { echo commit; }
TC_MANAGER_SMBD_RESTART_REQUIRED=0
TC_MANAGER_SMBD_RELOAD_REQUIRED=1
tc_manager_apply_smbd_runtime_changes
echo "reload_required=$TC_MANAGER_SMBD_RELOAD_REQUIRED failure=[$TC_MANAGER_SMBD_APPLY_FAILURE]"
''')
    assert out == [
        "present smbd",
        "bound",
        "reload",
        "log manager smbd recovery: smbd config reload failed; restarting",
        "stop smbd smbd",
        "start",
        "commit",
        "reload_required=0 failure=[]",
    ]


def test_full_samba_step_retains_pending_restart_after_identity_failure(tmp_path):
    out = run(tmp_path, '''
tc_manager_debug_log() { :; }
tc_log() { :; }
tc_now_seconds() { echo 1; }
tc_manager_log_step_end() { :; }
stages=0
tc_manager_stage_samba_runtime_files_if_needed() {
    stages=$((stages + 1))
    if [ "$stages" = 1 ]; then TC_MANAGER_SMBD_RESTART_REQUIRED=1; fi
}
tc_init_runtime_identity() { [ "$stages" != 1 ]; }
tc_manager_reconcile_smb_bind_interfaces() { TC_SMB_BIND_INTERFACES=127.0.0.1/8; }
tc_manager_record_successful_bind_status() { :; }
tc_manager_render_smb_conf_if_needed() { TC_MANAGER_PENDING_CONFIG_SIGNATURE=new; }
tc_manager_reconcile_smbd() {
    echo "apply restart=$TC_MANAGER_SMBD_RESTART_REQUIRED"
    tc_manager_commit_smbd_runtime_apply
}
manager_iteration_id=1
manager_status=0
TC_MANAGER_SMBD_RESTART_REQUIRED=0
TC_MANAGER_SMBD_RELOAD_REQUIRED=0
TC_MANAGER_PENDING_CONFIG_SIGNATURE=
if tc_manager_run_samba_full_step; then exit 9; fi
echo "pending=$TC_MANAGER_SMBD_RESTART_REQUIRED"
tc_manager_run_samba_full_step
echo "committed=$TC_MANAGER_LAST_CONFIG_SIGNATURE pending=$TC_MANAGER_SMBD_RESTART_REQUIRED"
''')
    assert out == ['pending=1', 'apply restart=1', 'committed=new pending=0']


def test_unapplied_config_is_rendered_again_when_desired_state_reverts(tmp_path):
    conf = tmp_path / 'smb.conf'
    conf.write_text('unapplied-new-config')
    out = run(tmp_path, f'''
tc_log() {{ :; }}
tc_manager_debug_log() {{ :; }}
TC_SMBD_CONF={shlex.quote(str(conf))}
TC_MANAGER_LAST_CONFIG_SIGNATURE=old
TC_MANAGER_PENDING_CONFIG_SIGNATURE=new
tc_manager_samba_config_signature() {{ echo old; }}
tc_manager_generate_smb_conf() {{ echo render-old; }}
runtime_process_present_by_ucomm() {{ return 0; }}
tc_manager_render_smb_conf_if_needed
echo "pending=$TC_MANAGER_PENDING_CONFIG_SIGNATURE reload=$TC_MANAGER_SMBD_RELOAD_REQUIRED"
''')
    assert out == ['render-old', 'pending=old reload=1']


def test_bind_only_tick_retries_pending_apply_without_another_address_change(tmp_path):
    out = run(tmp_path, '''
tc_manager_debug_log() { :; }
tc_log() { :; }
tc_now_seconds() { echo 1; }
tc_manager_log_step_end() { :; }
tc_manager_current_payload_ready() { return 0; }
tc_manager_samba_runtime_ready_for_bind_tick() { return 0; }
tc_manager_reconcile_smb_bind_interfaces() { TC_MANAGER_SMB_BIND_CHANGED=0; }
tc_manager_render_smb_conf_if_needed() { echo render; }
tc_manager_record_successful_bind_status() { :; }
tc_manager_reconcile_smbd() {
    echo apply
    [ "$apply_ok" = 1 ] || return 1
    tc_manager_commit_smbd_runtime_apply
}
manager_iteration_id=1
manager_status=0
TC_MANAGER_SMBD_RESTART_REQUIRED=1
TC_MANAGER_SMBD_RELOAD_REQUIRED=0
TC_MANAGER_PENDING_CONFIG_SIGNATURE=new
apply_ok=0
if tc_manager_run_samba_bind_step; then exit 9; fi
apply_ok=1
tc_manager_run_samba_bind_step
tc_manager_run_samba_bind_step
echo "pending=$TC_MANAGER_SMBD_RESTART_REQUIRED"
''')
    assert out == ['render', 'apply', 'render', 'apply', 'pending=0']
