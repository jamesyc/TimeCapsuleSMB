from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.transport.errors import SSH_TIMEOUT_SLOW_DEVICE_MESSAGE
from timecapsulesmb.transport.errors import ssh_timeout_slow_device_message


@dataclass(frozen=True)
class RecoveryInfo:
    title: str
    message: str
    actions: tuple[str, ...]
    retryable: bool
    suggested_operation: str | None = None
    action_ids: tuple[str, ...] = ()
    docs_anchor: str | None = None
    localization_key: str | None = None
    localization_values: dict[str, str] | None = None

    def to_jsonable(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "message": self.message,
            "actions": list(self.actions),
            "action_ids": list(self.action_ids),
            "retryable": self.retryable,
            "suggested_operation": self.suggested_operation,
        }
        if self.docs_anchor:
            payload["docs_anchor"] = self.docs_anchor
        if self.localization_key:
            payload["localization_key"] = self.localization_key
        if self.localization_values:
            payload["localization_values"] = dict(self.localization_values)
        return payload


_DEFAULTS: dict[str, RecoveryInfo] = {
    "invalid_request": RecoveryInfo(
        "Invalid request",
        "The helper request was malformed or had invalid parameter types.",
        ("Check the request JSON shape.", "Send params as a JSON object."),
        retryable=True,
    ),
    "unknown_operation": RecoveryInfo(
        "Unknown operation",
        "The helper does not recognize the requested operation.",
        ("Use one of the helper operations exposed by this app version.",),
        retryable=False,
    ),
    "validation_failed": RecoveryInfo(
        "Request validation failed",
        "One or more operation parameters were missing or invalid.",
        ("Review the highlighted fields.", "Retry with valid values."),
        retryable=True,
    ),
    "config_error": RecoveryInfo(
        "Configuration error",
        "The current .env configuration could not be read or used.",
        ("Open the configuration step.", "Verify host, password, and SSH options."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    "local_network_permission_denied": RecoveryInfo(
        "Local Network access blocked",
        "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
        (
            "Open System Settings > Privacy & Security > Local Network.",
            "Enable TimeCapsuleSMB in Local Network.",
            "Quit and reopen TimeCapsuleSMB, then retry configure.",
        ),
        retryable=True,
        suggested_operation="configure",
        action_ids=("open_system_settings", "retry"),
    ),
    "auth_failed": RecoveryInfo(
        "Authentication failed",
        "The device rejected the supplied password or SSH credentials.",
        ("Re-enter the AirPort admin password.", "Verify that SSH is enabled on the device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
        localization_key="auth_failed",
    ),
    "unsupported_device": RecoveryInfo(
        "Unsupported device",
        "The detected AirPort model or OS does not have a deployable payload in this build.",
        ("Check the detected model and OS.", "Use the CLI only if you intentionally pass unsupported-device overrides."),
        retryable=False,
    ),
    "ssh_compatibility_failed": RecoveryInfo(
        "SSH compatibility failed",
        "The local SSH client could not negotiate algorithms with the AirPort SSH server.",
        ("Update TimeCapsuleSMB and retry.", "Check debug details for the SSH algorithm error."),
        retryable=True,
        suggested_operation="configure",
    ),
    "confirmation_required": RecoveryInfo(
        "Confirmation required",
        "This operation changes the device and needs explicit confirmation.",
        ("Review the plan.", "Confirm the operation in the app before retrying."),
        retryable=True,
    ),
    "cancelled": RecoveryInfo(
        "Operation cancelled",
        "The helper was interrupted before the operation completed.",
        ("Retry the operation when ready.",),
        retryable=True,
    ),
    "remote_error": RecoveryInfo(
        "Remote operation failed",
        "The helper could not complete the requested remote device operation.",
        ("Check the operation log.", "Run doctor after the device is reachable."),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    "operation_failed": RecoveryInfo(
        "Operation failed",
        "The helper hit an unexpected failure while running the operation.",
        ("Check debug details.", "Retry after fixing the reported cause."),
        retryable=True,
    ),
}


_OPERATION_CODE_RECOVERY: dict[tuple[str, str], RecoveryInfo] = {
    ("configure", "auth_failed"): RecoveryInfo(
        "AirPort password rejected",
        "ACP or SSH authentication failed while configuring the device.",
        ("Re-enter the AirPort admin password.", "Confirm the selected device is the intended Apple device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
        localization_key="configure.auth_failed",
    ),
    ("configure", "unsupported_device"): RecoveryInfo(
        "Unsupported device",
        "The SSH probe succeeded, but the detected hardware or OS cannot use a bundled payload.",
        ("Review the detected model and OS.", "Use a supported Apple AirPort Time Capsule or AirPort Extreme."),
        retryable=False,
    ),
    ("configure", "ssh_compatibility_failed"): RecoveryInfo(
        "SSH compatibility failed",
        "The AirPort SSH server only offered legacy algorithms that the local SSH client did not negotiate.",
        ("Update TimeCapsuleSMB and retry.", "Check debug details for the SSH algorithm offer."),
        retryable=True,
        suggested_operation="configure",
    ),
    ("deploy", "confirmation_required"): RecoveryInfo(
        "Deploy confirmation required",
        "Deploy needs confirmation before uploading payload files, rebooting, or activating NetBSD4.",
        ("Review the deploy plan.", "Confirm deploy and any required reboot or activation prompt."),
        retryable=True,
    ),
    ("deploy", "validation_failed"): RecoveryInfo(
        "Deployment validation failed",
        "The bundled payload artifacts or deployment inputs are invalid.",
        ("Open Readiness.", "Fix missing artifacts or invalid fields before retrying."),
        retryable=True,
        suggested_operation="validate-install",
        action_ids=("open_diagnostics",),
    ),
    ("deploy", "unsupported_device"): RecoveryInfo(
        "No supported deploy payload",
        "The detected device does not match a bundled payload family.",
        ("Check the device model and OS.", "Do not deploy from the GUI until a supported payload is available."),
        retryable=False,
    ),
    ("deploy", "deploy_no_disk_detected"): RecoveryInfo(
        "No internal disk detected",
        "The device did not report any internal disk through MaSt.",
        (
            "Check that the disk is connected and seated.",
            "Power-cycle the device and retry after the disk spins up.",
            "Some devices cannot fully detect some disks larger than 2TB.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_no_hfs_partition"): RecoveryInfo(
        "No valid HFS partition",
        "A disk was found, but it does not expose a valid HFS partition that TimeCapsuleSMB can deploy to.",
        (
            "Erase the disk with AirPort Utility using Erase Disk.",
            "Retry deploy after the Time Capsule formats the disk.",
            "Some devices cannot detect some partitions larger than 2TB.",
        ),
        retryable=False,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_disk_not_writable"): RecoveryInfo(
        "No writable payload volume",
        "MaSt found HFS volumes, but none accepted the managed payload directory.",
        ("Wake or remount the disk.", "Check available free space.", "Retry deploy."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("activate", "confirmation_required"): RecoveryInfo(
        "Activation confirmation required",
        "NetBSD4 activation starts the deployed runtime and must be confirmed.",
        ("Review the NetBSD4 activation guidance.", "Confirm activation before retrying."),
        retryable=True,
        action_ids=("start_smb",),
    ),
    ("uninstall", "confirmation_required"): RecoveryInfo(
        "Uninstall confirmation required",
        "Uninstall removes managed files and may reboot the device.",
        ("Review the uninstall plan.", "Confirm uninstall and reboot before retrying."),
        retryable=True,
        action_ids=("uninstall",),
    ),
    ("fsck", "confirmation_required"): RecoveryInfo(
        "Disk repair confirmation required",
        "Disk repair runs fsck, stops file sharing, unmounts the selected HFS disk, and may reboot the device.",
        ("Review the selected volume.", "Confirm disk repair before retrying."),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("fsck", "validation_failed"): RecoveryInfo(
        "Volume selection failed",
        "The helper could not choose a mounted HFS volume for fsck.",
        ("Select a specific HFS volume.", "Refresh mounted volumes and retry."),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("repair-xattrs", "confirmation_required"): RecoveryInfo(
        "Repair confirmation required",
        "repair-xattrs needs dry-run mode or explicit confirmation before changing local file metadata.",
        ("Run a dry run first.", "Confirm repair before retrying."),
        retryable=True,
        action_ids=("repair_metadata",),
    ),
    ("repair-xattrs", "validation_failed"): RecoveryInfo(
        "repair-xattrs cannot run",
        "repair-xattrs must run on macOS against a valid mounted SMB share path.",
        ("Choose a mounted share path.", "Run this from macOS."),
        retryable=True,
        action_ids=("repair_metadata",),
    ),
}


_STAGE_RECOVERY: dict[tuple[str, str, str], RecoveryInfo] = {
    ("configure", "remote_error", "acp_port_probe"): RecoveryInfo(
        "AirPort not reachable at this address",
        "TimeCapsuleSMB could not reach the AirPort ACP service before enabling SSH. "
        "Backups or AirPort Utility may still work even when ACP is blocked.",
        (
            "Disable VPN or security software that routes local network traffic, then try again.",
            "Check that the IP address is the Time Capsule or AirPort address.",
            "Confirm you are on the same network as the device.",
            "Use discovery or enter the current LAN IP address.",
        ),
        retryable=True,
        suggested_operation="configure",
        localization_key="configure.remote_error.acp_port_probe",
    ),
    ("configure", "remote_error", "acp_enable_ssh"): RecoveryInfo(
        "ACP SSH enablement failed",
        "The helper could not enable SSH through AirPort ACP.",
        ("Verify the AirPort admin password.", "Power-cycle the device if AirPort Utility also cannot manage it."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    ("configure", "remote_error", "wait_for_ssh_after_acp"): RecoveryInfo(
        "SSH did not open",
        "ACP accepted the request, but the SSH port did not become reachable in time.",
        ("Wait for the device to finish rebooting.", "Retry configure with a longer SSH wait timeout."),
        retryable=True,
        suggested_operation="configure",
    ),
    ("deploy", "remote_error", "read_mast"): RecoveryInfo(
        "No HFS volumes found",
        "The device did not report a deployable HFS disk through MaSt.",
        ("Wake the disk by opening it in Finder.", "Check the disk is installed and formatted HFS.", "Retry deploy."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "remote_error", "select_payload_home"): RecoveryInfo(
        "No writable payload volume",
        "MaSt found HFS volumes, but none accepted the managed payload directory.",
        ("Wake or remount the disk.", "Check available free space.", "Retry deploy."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "remote_error", "verify_payload_upload"): RecoveryInfo(
        "Payload verification failed",
        "The uploaded managed payload could not be verified on the HFS disk.",
        ("Wake the disk and retry.", "Check the operation log for the failing path."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "remote_error", "verify_payload_upload_after_sync"): RecoveryInfo(
        "Payload verification failed after sync",
        "The managed payload was not stable after flushing disk writes.",
        ("Retry deploy.", "Check the disk for write or corruption issues."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "remote_error", "wait_for_reboot_down"): RecoveryInfo(
        "Reboot did not start",
        "The reboot request was sent, but SSH did not go down.",
        ("Power-cycle the device.", "Retry deploy after it is reachable."),
        retryable=True,
        suggested_operation="doctor",
    ),
    ("deploy", "remote_error", "wait_for_reboot_up"): RecoveryInfo(
        "Reboot did not finish",
        (
            "The payload was uploaded and the reboot request succeeded, but the device did not accept SSH "
            "again before the 4 minute timeout. It may still be booting, or it may have come back with a "
            "different IP address."
        ),
        (
            "Wait a few more minutes.",
            "The device may have a new IP address. Run Discover and reselect it.",
            "Make sure you are connected to the same network or Wi-Fi as the device.",
            (
                "On NetBSD 4 devices, run tcapsule activate once SSH is reachable; deploy did not get far "
                "enough to activate Samba after reboot."
            ),
            (
                "If your device resets itself, see "
                "https://github.com/jamesyc/TimeCapsuleSMB/issues/177."
            ),
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
        localization_key="deploy.remote_error.wait_for_reboot_up",
    ),
    ("deploy", "remote_error", "verify_runtime_reboot"): RecoveryInfo(
        "Runtime not ready",
        "The device rebooted, but the managed Samba runtime did not become healthy.",
        ("Run doctor for details.", "Check boot logs from the CLI if doctor still fails."),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    ("deploy", "remote_error", "activate_runtime"): RecoveryInfo(
        "Runtime activation failed",
        "The deployed Samba runtime could not be started without rebooting.",
        ("Retry install/update.", "Run doctor for detailed runtime checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    ("deploy", "remote_error", "post_reboot_activation"): RecoveryInfo(
        "Post-reboot activation failed",
        "The device rebooted, but the deployed Samba runtime could not be started after SSH returned.",
        ("Retry install/update.", "Run doctor for detailed runtime checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    ("deploy", "remote_error", "verify_runtime_activation"): RecoveryInfo(
        "Activated runtime not ready",
        "The deployed Samba runtime was started but did not become healthy.",
        ("Retry install/update.", "Run doctor for detailed runtime checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    ("uninstall", "remote_error", "verify_post_uninstall"): RecoveryInfo(
        "Post-uninstall verification failed",
        "Managed TimeCapsuleSMB files were still present after reboot.",
        ("Retry uninstall.", "Run doctor if the device is reachable."),
        retryable=True,
        suggested_operation="uninstall",
        action_ids=("uninstall",),
    ),
    ("fsck", "validation_failed", "select_fsck_volume"): RecoveryInfo(
        "Volume selection failed",
        "The helper could not choose exactly one HFS volume for fsck.",
        ("Select the target volume explicitly.", "Refresh mounted volumes and retry."),
        retryable=True,
        suggested_operation="fsck",
        action_ids=("disk_repair",),
    ),
    ("repair-xattrs", "validation_failed", "platform_check"): RecoveryInfo(
        "repair-xattrs requires macOS",
        "repair-xattrs can only run on macOS because it uses xattr and chflags on a mounted SMB share.",
        ("Run the app on macOS.", "Use dry run or repair from a mounted share path."),
        retryable=False,
        suggested_operation="repair-xattrs",
        action_ids=("repair_metadata",),
    ),
    ("repair-xattrs", "validation_failed", "validate_params"): RecoveryInfo(
        "Invalid repair options",
        "One or more repair-xattrs options were invalid.",
        ("Review the repair options.", "Retry with valid values."),
        retryable=True,
        suggested_operation="repair-xattrs",
        action_ids=("repair_metadata",),
    ),
    ("repair-xattrs", "validation_failed", "resolve_scan_root"): RecoveryInfo(
        "Path cannot be scanned",
        "The selected path is not usable for repair-xattrs.",
        ("Choose a mounted SMB share path.", "Confirm the share is accessible in Finder."),
        retryable=True,
        suggested_operation="repair-xattrs",
        action_ids=("repair_metadata",),
    ),
    ("repair-xattrs", "validation_failed", "scan_findings"): RecoveryInfo(
        "Path cannot be scanned",
        "repair-xattrs could not read the selected mounted share path.",
        ("Choose a mounted SMB share path.", "Confirm the share is accessible in Finder."),
        retryable=True,
        suggested_operation="repair-xattrs",
        action_ids=("repair_metadata",),
    ),
}


_SSH_TIMEOUT_SLOW_DEVICE_RECOVERY = RecoveryInfo(
    "Device is responding very slowly",
    SSH_TIMEOUT_SLOW_DEVICE_MESSAGE,
    (
        "Reboot the device.",
        "Wait for SSH to come back.",
        "Retry the operation.",
    ),
    retryable=True,
    suggested_operation="doctor",
    action_ids=("run_checkup",),
    localization_key="remote_error.ssh_timeout_slow_device",
)


def recovery_for(
    operation: str,
    code: str,
    *,
    stage: str | None = None,
) -> dict[str, object]:
    if stage:
        policy = _STAGE_RECOVERY.get((operation, code, stage))
        if policy is not None:
            return policy.to_jsonable()
    policy = _OPERATION_CODE_RECOVERY.get((operation, code)) or _DEFAULTS.get(code) or _DEFAULTS["operation_failed"]
    return policy.to_jsonable()


def ssh_timeout_slow_device_recovery(*, device_name: str | None = None) -> dict[str, object]:
    recovery = _SSH_TIMEOUT_SLOW_DEVICE_RECOVERY.to_jsonable()
    recovery["message"] = ssh_timeout_slow_device_message(device_name)
    recovery["localization_values"] = {
        "device_name": (device_name or "").strip() or "device",
    }
    return recovery
