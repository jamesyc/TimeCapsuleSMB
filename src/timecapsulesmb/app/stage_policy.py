from __future__ import annotations

from dataclasses import dataclass


LOCAL_READ = "local_read"
LOCAL_WRITE = "local_write"
REMOTE_READ = "remote_read"
REMOTE_WRITE = "remote_write"
DESTRUCTIVE = "destructive"
REBOOT = "reboot"

RISK_VALUES = frozenset({
    LOCAL_READ,
    LOCAL_WRITE,
    REMOTE_READ,
    REMOTE_WRITE,
    DESTRUCTIVE,
    REBOOT,
})


@dataclass(frozen=True)
class StagePolicy:
    risk: str
    cancellable: bool
    description: str

    def to_jsonable(self) -> dict[str, object]:
        return {
            "risk": self.risk,
            "cancellable": self.cancellable,
            "description": self.description,
        }


_POLICIES: dict[tuple[str, str], StagePolicy] = {
    ("capabilities", "resolve_paths"): StagePolicy(LOCAL_READ, True, "Resolve helper configuration and distribution paths."),
    ("capabilities", "summarize_capabilities"): StagePolicy(LOCAL_READ, True, "Summarize helper API capabilities."),
    ("discover", "bonjour_discovery"): StagePolicy(LOCAL_READ, True, "Browse for AirPort Bonjour services."),
    ("reachability", "load_config"): StagePolicy(LOCAL_READ, True, "Read selected device reachability configuration."),
    ("reachability", "build_candidates"): StagePolicy(LOCAL_READ, True, "Build selected device host candidates."),
    ("reachability", "check_dns"): StagePolicy(LOCAL_READ, True, "Resolve selected device host candidates."),
    ("reachability", "check_ping"): StagePolicy(REMOTE_READ, True, "Ping selected device host candidates."),
    ("reachability", "check_ssh_port"): StagePolicy(REMOTE_READ, True, "Check selected device SSH port reachability."),
    ("reachability", "check_ssh_auth"): StagePolicy(REMOTE_READ, True, "Check selected device SSH authentication."),
    ("reachability", "check_smb_port"): StagePolicy(REMOTE_READ, True, "Check selected device SMB port reachability."),
    ("set-telemetry", "resolve_paths"): StagePolicy(LOCAL_READ, True, "Resolve local app state paths."),
    ("set-telemetry", "write_bootstrap"): StagePolicy(LOCAL_WRITE, False, "Update local telemetry preference."),
    ("validate-install", "resolve_paths"): StagePolicy(LOCAL_READ, True, "Resolve app installation paths."),
    ("validate-install", "validate_install"): StagePolicy(LOCAL_READ, True, "Validate local helper and artifact prerequisites."),
    ("version-check", "resolve_paths"): StagePolicy(LOCAL_READ, True, "Resolve version check cache path."),
    ("version-check", "check_version"): StagePolicy(LOCAL_READ, True, "Fetch or read version metadata."),
    ("configure", "load_existing_config"): StagePolicy(LOCAL_READ, True, "Read the existing .env configuration."),
    ("configure", "ssh_probe"): StagePolicy(REMOTE_READ, True, "Probe SSH reachability and device compatibility."),
    ("configure", "confirm_enable_ssh"): StagePolicy(REBOOT, True, "Confirm SSH enablement and reboot through AirPort ACP."),
    ("configure", "acp_port_probe"): StagePolicy(REMOTE_READ, True, "Check AirPort ACP reachability before enabling SSH."),
    ("configure", "acp_enable_ssh"): StagePolicy(REMOTE_WRITE, False, "Request SSH enablement through AirPort ACP."),
    ("configure", "wait_for_ssh_after_acp"): StagePolicy(REMOTE_READ, True, "Wait for SSH to open after ACP enablement."),
    ("configure", "ssh_probe_after_acp"): StagePolicy(REMOTE_READ, True, "Probe SSH again after ACP enablement."),
    ("configure", "write_env"): StagePolicy(LOCAL_WRITE, False, "Write the app .env configuration."),
    ("deploy", "load_config"): StagePolicy(LOCAL_READ, True, "Read deployment configuration."),
    ("deploy", "resolve_managed_target"): StagePolicy(REMOTE_READ, True, "Resolve and probe the device target."),
    ("deploy", "validate_artifacts"): StagePolicy(LOCAL_READ, True, "Validate bundled payload artifacts."),
    ("deploy", "check_compatibility"): StagePolicy(REMOTE_READ, True, "Check detected device compatibility."),
    ("deploy", "read_mast"): StagePolicy(REMOTE_READ, True, "Read mounted HFS volume metadata from MaSt."),
    ("deploy", "select_payload_home"): StagePolicy(REMOTE_READ, True, "Select a writable HFS payload location."),
    ("deploy", "build_deployment_plan"): StagePolicy(LOCAL_READ, True, "Build the deployment action plan."),
    ("deploy", "pre_upload_actions"): StagePolicy(REMOTE_WRITE, False, "Prepare remote directories and stop conflicting processes."),
    ("deploy", "prepare_deployment_files"): StagePolicy(LOCAL_WRITE, True, "Generate temporary deployment config files."),
    ("deploy", "upload_payload"): StagePolicy(REMOTE_WRITE, False, "Upload managed Samba payload files."),
    ("deploy", "upload_smbd"): StagePolicy(REMOTE_WRITE, False, "Upload smbd."),
    ("deploy", "upload_mdns_advertiser"): StagePolicy(REMOTE_WRITE, False, "Upload mdns-advertiser."),
    ("deploy", "upload_nbns_advertiser"): StagePolicy(REMOTE_WRITE, False, "Upload nbns-advertiser."),
    ("deploy", "upload_boot_files"): StagePolicy(REMOTE_WRITE, False, "Upload boot files."),
    ("deploy", "upload_runtime_config"): StagePolicy(REMOTE_WRITE, False, "Upload runtime config."),
    ("deploy", "upload_samba_accounts"): StagePolicy(REMOTE_WRITE, False, "Upload Samba account files."),
    ("deploy", "post_upload_actions"): StagePolicy(REMOTE_WRITE, False, "Install flash hooks and payload permissions."),
    ("deploy", "verify_payload_upload"): StagePolicy(REMOTE_READ, True, "Verify uploaded payload files."),
    ("deploy", "flush_payload_upload"): StagePolicy(REMOTE_WRITE, False, "Flush remote filesystem writes."),
    ("deploy", "verify_payload_upload_after_sync"): StagePolicy(REMOTE_READ, True, "Verify uploaded payload files after sync."),
    ("deploy", "probe_runtime"): StagePolicy(REMOTE_READ, True, "Checking whether the device will start TimeCapsuleSMB automatically."),
    ("deploy", "activate_runtime"): StagePolicy(REMOTE_WRITE, False, "Start the deployed runtime without reboot."),
    ("deploy", "post_reboot_boot_settle"): StagePolicy(REMOTE_READ, True, "Wait briefly after SSH returns before probing boot-time services."),
    ("deploy", "post_activation_settle"): StagePolicy(REMOTE_READ, True, "Wait briefly after activation before probing runtime readiness."),
    ("deploy", "post_reboot_activation"): StagePolicy(REMOTE_WRITE, False, "Start the deployed runtime after reboot."),
    ("deploy", "verify_runtime_activation"): StagePolicy(REMOTE_READ, True, "Wait for the activated runtime to become ready."),
    ("deploy", "reboot"): StagePolicy(REBOOT, False, "Request a device reboot."),
    ("deploy", "wait_for_reboot_down"): StagePolicy(REBOOT, True, "Wait for SSH to go down after reboot request."),
    ("deploy", "wait_for_reboot_up"): StagePolicy(REBOOT, True, "Wait for SSH to return after reboot."),
    ("deploy", "verify_runtime_reboot"): StagePolicy(REMOTE_READ, True, "Wait for the managed runtime after reboot."),
    ("doctor", "load_config"): StagePolicy(LOCAL_READ, True, "Read diagnostic configuration."),
    ("doctor", "resolve_connection"): StagePolicy(REMOTE_READ, True, "Resolve the configured SSH connection."),
    ("doctor", "run_checks"): StagePolicy(REMOTE_READ, True, "Run local and remote diagnostic checks."),
    ("activate", "load_config"): StagePolicy(LOCAL_READ, True, "Read activation configuration."),
    ("activate", "resolve_managed_target"): StagePolicy(REMOTE_READ, True, "Resolve and probe the NetBSD4 target."),
    ("activate", "build_activation_plan"): StagePolicy(LOCAL_READ, True, "Build the NetBSD4 activation action plan."),
    ("activate", "probe_runtime"): StagePolicy(REMOTE_READ, True, "Checking whether TimeCapsuleSMB is already running before activating it."),
    ("activate", "run_activation"): StagePolicy(REMOTE_WRITE, False, "Run NetBSD4 activation commands."),
    ("activate", "post_activation_settle"): StagePolicy(REMOTE_READ, True, "Wait briefly after activation before probing runtime readiness."),
    ("activate", "verify_runtime_activation"): StagePolicy(REMOTE_READ, True, "Wait for the activated runtime to become ready."),
    ("uninstall", "load_config"): StagePolicy(LOCAL_READ, True, "Read uninstall configuration."),
    ("uninstall", "resolve_connection"): StagePolicy(REMOTE_READ, True, "Resolve the configured SSH connection."),
    ("uninstall", "read_mast"): StagePolicy(REMOTE_READ, True, "Read mounted HFS volume metadata from MaSt."),
    ("uninstall", "mount_mast_volumes"): StagePolicy(REMOTE_WRITE, False, "Mount HFS volumes before uninstall."),
    ("uninstall", "build_uninstall_plan"): StagePolicy(LOCAL_READ, True, "Build the uninstall action plan."),
    ("uninstall", "uninstall_payload"): StagePolicy(DESTRUCTIVE, False, "Remove managed payload files and flash hooks."),
    ("uninstall", "reboot"): StagePolicy(REBOOT, False, "Request a device reboot."),
    ("uninstall", "wait_for_reboot_down"): StagePolicy(REBOOT, True, "Wait for SSH to go down after reboot request."),
    ("uninstall", "wait_for_reboot_up"): StagePolicy(REBOOT, True, "Wait for SSH to return after reboot."),
    ("uninstall", "verify_post_uninstall"): StagePolicy(REMOTE_READ, True, "Verify managed files are absent after reboot."),
    ("fsck", "load_config"): StagePolicy(LOCAL_READ, True, "Read fsck configuration."),
    ("fsck", "resolve_connection"): StagePolicy(REMOTE_READ, True, "Resolve the configured SSH connection."),
    ("fsck", "read_mast"): StagePolicy(REMOTE_READ, True, "Read mounted HFS volume metadata from MaSt."),
    ("fsck", "mount_hfs_volumes"): StagePolicy(REMOTE_WRITE, False, "Mount HFS volumes before fsck."),
    ("fsck", "list_fsck_volumes"): StagePolicy(REMOTE_READ, True, "List mounted HFS volumes available for fsck."),
    ("fsck", "select_fsck_volume"): StagePolicy(REMOTE_READ, True, "Select the HFS volume to repair."),
    ("fsck", "run_fsck"): StagePolicy(DESTRUCTIVE, False, "Unmount the selected disk and run fsck_hfs."),
    ("fsck", "wait_for_reboot_down"): StagePolicy(REBOOT, True, "Wait for SSH to go down after fsck reboot."),
    ("fsck", "wait_for_reboot_up"): StagePolicy(REBOOT, True, "Wait for SSH to return after fsck reboot."),
    ("repair-xattrs", "platform_check"): StagePolicy(LOCAL_READ, True, "Verify repair-xattrs is running on macOS."),
    ("repair-xattrs", "validate_params"): StagePolicy(LOCAL_READ, True, "Validate repair-xattrs request parameters."),
    ("repair-xattrs", "resolve_scan_root"): StagePolicy(LOCAL_READ, True, "Resolve the mounted SMB share scan root."),
    ("repair-xattrs", "scan_findings"): StagePolicy(LOCAL_READ, True, "Scan local mounted SMB files for xattr problems."),
    ("repair-xattrs", "report_findings"): StagePolicy(LOCAL_READ, True, "Render xattr findings and repair candidates."),
    ("repair-xattrs", "confirm_repair"): StagePolicy(LOCAL_READ, True, "Confirm local metadata repairs."),
    ("repair-xattrs", "repair_findings"): StagePolicy(DESTRUCTIVE, False, "Repair local file metadata on the mounted SMB share."),
    ("flash", "load_config"): StagePolicy(LOCAL_READ, True, "Read flash configuration."),
    ("flash", "resolve_connection"): StagePolicy(REMOTE_READ, True, "Resolve the configured SSH connection."),
    ("flash", "check_compatibility"): StagePolicy(REMOTE_READ, True, "Check NetBSD4 flash compatibility."),
    ("flash", "read_flash"): StagePolicy(REMOTE_READ, True, "Read both firmware banks from the device."),
    ("flash", "save_raw_backup"): StagePolicy(LOCAL_WRITE, False, "Save raw firmware bank backups locally."),
    ("flash", "inspect_backup"): StagePolicy(LOCAL_READ, True, "Read and inspect the saved flash backup."),
    ("flash", "analyze_flash"): StagePolicy(LOCAL_READ, True, "Analyze firmware bank safety metadata."),
    ("flash", "plan_flash"): StagePolicy(LOCAL_WRITE, True, "Build and save the firmware flash plan."),
    ("flash", "save_backup"): StagePolicy(LOCAL_WRITE, False, "Write flash backup manifest."),
    ("flash", "confirm_write"): StagePolicy(DESTRUCTIVE, True, "Confirm firmware flash write."),
    ("flash", "pre_write_validation"): StagePolicy(REMOTE_READ, True, "Verify the live target bank still matches the saved backup."),
    ("flash", "write_primary_bank"): StagePolicy(DESTRUCTIVE, False, "Write the primary firmware bank."),
    ("flash", "write_active_bank"): StagePolicy(DESTRUCTIVE, False, "Write the active firmware bank."),
    ("flash", "post_write_validation"): StagePolicy(REMOTE_READ, True, "Read back and validate the written firmware bank."),
}


def stage_policy(operation: str, stage: str) -> StagePolicy | None:
    return _POLICIES.get((operation, stage))
