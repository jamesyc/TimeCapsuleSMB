from __future__ import annotations

from dataclasses import dataclass, replace

from timecapsulesmb.transport.errors import LOCAL_NETWORK_FILTERED_MESSAGE, SSH_TIMEOUT_SLOW_DEVICE_MESSAGE
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
    "unknown_param": RecoveryInfo(
        "Unknown parameter",
        "The helper does not accept a parameter this request sent, so the app and helper are out of step.",
        (
            "Update or reinstall TimeCapsuleSMB so the app and helper use the same API contract.",
            "If Helper path is set in Settings, clear it.",
        ),
        # Resending the same request fails the same way.
        retryable=False,
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
    "device_identity_mismatch": RecoveryInfo(
        "Device identity could not be confirmed",
        "The device identity could not be confirmed. Refresh discovery and reconnect the saved device.",
        ("Refresh discovery and reconnect the saved device.",),
        retryable=True,
        suggested_operation="configure",
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
    # The helper's own message names the address and this Mac's networks.
    "device_off_network": RecoveryInfo(
        "Device not on this Mac's network",
        "The device's address isn't on this Mac's network. Check the address, or connect this Mac "
        "to the device's network by Wi-Fi or one of its LAN ports, then try again.",
        (),
        retryable=True,
    ),
    # No step: the filtering app may be a VPN the user cannot turn off.
    "local_network_filtered": RecoveryInfo(
        "Connection blocked on this Mac",
        LOCAL_NETWORK_FILTERED_MESSAGE,
        (),
        retryable=True,
    ),
    "auth_failed": RecoveryInfo(
        "Authentication failed",
        "The device rejected the supplied password or SSH credentials.",
        ("Re-enter the AirPort admin password.", "Verify that SSH is enabled on the device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    # Shared by causes that are not an unsupported model, such as flash or Activate
    # on NetBSD 6, so it must not tell the user to forget a working device.
    "unsupported_device": RecoveryInfo(
        "Unsupported device",
        "This operation is not supported on the detected AirPort model or OS.",
        (
            "Check the detected model and OS.",
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
        ),
        retryable=False,
    ),
    "ssh_compatibility_failed": RecoveryInfo(
        "SSH compatibility failed",
        "The local SSH client could not negotiate algorithms with the AirPort SSH server.",
        ("Update TimeCapsuleSMB and retry.", "Check debug details for the SSH algorithm error."),
        retryable=True,
        suggested_operation="configure",
    ),
    # The SSH probe's other outcomes (services.runtime.probe_failure_error).
    # None of them says anything about the model, so none may suggest it is
    # unsupported or that the device should be forgotten.
    "ssh_disabled": RecoveryInfo(
        "SSH is turned off",
        "The device answers AirPort ACP, but its SSH port is closed. SSH turns off after a reset, "
        "or when it is disabled in SSH Access.",
        ("Open SSH Access and choose Enable SSH, then try again.",),
        retryable=True,
        action_ids=("open_ssh_access",),
    ),
    "device_unreachable": RecoveryInfo(
        "Device not reachable",
        "Neither SSH nor AirPort ACP answered at the device's saved address.",
        (
            "Make sure the device is turned on and connected to the same network or Wi-Fi as this Mac.",
            "If the device is restarting, wait a few minutes, then try again.",
        ),
        retryable=True,
    ),
    "ssh_transport_failed": RecoveryInfo(
        "SSH connection dropped",
        "The device accepted the SSH connection, then closed it before login. "
        "This can happen while the device is starting up or busy.",
        ("Wait a minute, then try again.", "If this keeps happening, restart the device."),
        retryable=True,
    ),
    "device_probe_failed": RecoveryInfo(
        "Device check failed",
        "TimeCapsuleSMB logged in over SSH but could not read the device's system information.",
        ("Try again.", "Run Checkup for details."),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # Configure and set-ssh: ACP took the request but SSH did not open within
    # services.reboot.REBOOT_UP_TIMEOUT_SECONDS.
    "ssh_enable_timeout": RecoveryInfo(
        "SSH has not opened yet",
        "Turning on SSH restarts the device. Some devices take longer to restart than TimeCapsuleSMB waits.",
        ("Wait a few minutes, then try again.", "If SSH still does not open, restart the device, then try again."),
        retryable=True,
    ),
    # services.reboot.reboot_device: every reboot that does not finish.
    "reboot_not_started": RecoveryInfo(
        "Reboot did not start",
        "The reboot request was sent, but the device did not restart.",
        ("Power-cycle the device.", "Try again once the device is reachable."),
        retryable=True,
        suggested_operation="doctor",
    ),
    "reboot_not_finished": RecoveryInfo(
        "Reboot did not finish",
        (
            "The device went offline or restarted, but did not come back in time. It may still be "
            "starting up, or it may have a new IP address."
        ),
        (
            "Wait a few more minutes.",
            "Make sure you are connected to the same network or Wi-Fi as the device.",
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # fsck: the reboot finished, but the installed runtime did not start again
    # (services.activation.start_netbsd4_runtime_after_reboot).
    "runtime_not_restarted": RecoveryInfo(
        "File sharing did not restart",
        "The device restarted, but the installed TimeCapsuleSMB services did not start.",
        ("Run Activate.", "Run Checkup for details."),
        retryable=True,
        suggested_operation="activate",
        action_ids=("start_smb", "run_checkup"),
    ),
    "ssh_still_enabled": RecoveryInfo(
        "SSH is still enabled",
        "The device restarted, but SSH was still enabled afterwards.",
        ("Disable SSH again in SSH Access.", "If SSH stays enabled, restart the device, then try again."),
        retryable=True,
        action_ids=("open_ssh_access",),
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
    ("deploy", "reboot_not_finished"): RecoveryInfo(
        "Reboot did not finish",
        (
            "The payload was uploaded and the reboot request succeeded, but the device did not accept SSH "
            "again in time. It may still be booting, or it may have come back with a "
            "different IP address."
        ),
        (
            "Wait a few more minutes.",
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
    ),
    ("configure", "auth_failed"): RecoveryInfo(
        "AirPort password rejected",
        "ACP or SSH authentication failed while configuring the device.",
        ("Re-enter the AirPort admin password.", "Confirm the selected device is the intended Apple device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    ("configure", "unsupported_device"): RecoveryInfo(
        "Unsupported device",
        "This AirPort model cannot run TimeCapsuleSMB.",
        (
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
            "Add your Time Capsule or AirPort Extreme instead.",
        ),
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
        ("Open Diagnostics.", "Fix missing artifacts or invalid fields before retrying."),
        retryable=True,
        suggested_operation="validate-install",
        action_ids=("open_diagnostics",),
    ),
    ("deploy", "unsupported_device"): RecoveryInfo(
        "No supported deploy payload",
        "This AirPort model cannot run TimeCapsuleSMB.",
        (
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
            "Forget this device, then add your Time Capsule or AirPort Extreme.",
        ),
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
    ("deploy", "deploy_no_usb_disk_detected"): RecoveryInfo(
        "No USB disk detected",
        "An AirPort Extreme has no internal disk, and the device reported no USB disk through MaSt.",
        (
            "Connect a USB disk formatted for Mac (HFS+).",
            "If a disk is connected, check its power and cable, then retry.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_no_hfs_partition"): RecoveryInfo(
        "No valid HFS partition",
        "A disk was found, but it does not expose a valid HFS partition that TimeCapsuleSMB can deploy to.",
        (
            "Retry deploy.",
            "Erase the disk with AirPort Utility using Erase Disk.",
            "Retry deploy after the Time Capsule formats the disk.",
            "Some devices cannot detect some partitions larger than 2 TB.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_disk_not_writable"): RecoveryInfo(
        "No writable payload volume",
        "MaSt found HFS volumes, but none accepted the managed payload directory.",
        ("Wake or remount the disk.", "Check available free space.", "Retry deploy."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_disk_not_mounted"): RecoveryInfo(
        "HFS disk not mounted",
        "MaSt found HFS volumes, but none was mounted, and the device did not mount one when asked.",
        ("Wait a minute, then retry deploy.", "Restart the device if the disk still does not mount."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", "deploy_disk_not_confirmed"): RecoveryInfo(
        "Disk not kept mounted",
        "An HFS volume is mounted, but the device would not keep it mounted for TimeCapsuleSMB, "
        "so it could be unmounted during deploy.",
        ("Wait a minute, then retry deploy.", "Restart the device if this keeps happening."),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("flash", "secondary_bank_invalid"): RecoveryInfo(
        "Backup firmware bank is damaged",
        "The secondary (backup) firmware bank is not a valid copy, so patching the primary bank is refused: "
        "an interrupted write would leave nothing to start from.",
        (
            "Choose Plan Restore to rewrite the backup bank with Apple firmware.",
            "Then choose Back Up and Inspect Again, and patch.",
        ),
        retryable=False,
    ),
    ("flash", "secondary_bank_read_mismatch"): RecoveryInfo(
        "Backup firmware bank read inconsistently",
        "This backup's read of the secondary (backup) firmware bank does not agree with the device's own check "
        "of it. The bank may still be a good copy, so it is not rewritten and patching is refused.",
        (
            "Choose Back Up and Inspect.",
            "If the reads keep disagreeing, leave the firmware as it is: this device's flash may be failing.",
        ),
        retryable=False,
    ),
    ("activate", "runtime_not_installed"): RecoveryInfo(
        "TimeCapsuleSMB not installed",
        "The device has no TimeCapsuleSMB installation to start.",
        ("Run Install / Update Samba.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    ("activate", "runtime_outdated"): RecoveryInfo(
        "Installation is out of date",
        "The installed TimeCapsuleSMB is too old for this app to start or check.",
        ("Run Install / Update Samba.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    ("activate", "client_outdated"): RecoveryInfo(
        "App is out of date",
        "The installed TimeCapsuleSMB is from a newer major version, which this app cannot start or check.",
        ("Update TimeCapsuleSMB, then start it again.",),
        retryable=False,
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
    ),
    ("configure", "remote_error", "acp_enable_ssh"): RecoveryInfo(
        "ACP SSH enablement failed",
        "The helper could not enable SSH through AirPort ACP.",
        ("Verify the AirPort admin password.", "Power-cycle the device if AirPort Utility also cannot manage it."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
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
}


# The app shows each entry from its catalogs as backend.recovery.<key>.title,
# .message and .action.N, keyed by where the entry sits in these tables:
# "<code>", "<operation>.<code>" or "<operation>.<code>.<stage>". The English
# catalog repeats this text (tests/test_error_catalog.py checks both).
_DEFAULTS = {code: replace(info, localization_key=code) for code, info in _DEFAULTS.items()}
_OPERATION_CODE_RECOVERY = {
    (operation, code): replace(info, localization_key=f"{operation}.{code}")
    for (operation, code), info in _OPERATION_CODE_RECOVERY.items()
}
_OPERATION_CODE_RECOVERY[("discover", "discovery_timeout_too_short")] = replace(
    _DEFAULTS["validation_failed"], localization_key="discover.discovery_timeout_too_short",
)
_OPERATION_CODE_RECOVERY[("discover", "local_network_permission_denied")] = replace(
    _DEFAULTS["local_network_permission_denied"],
    actions=_DEFAULTS["local_network_permission_denied"].actions[:2] + ("Enable access, then retry discovery.",),
    suggested_operation="discover", localization_key="discover.local_network_permission_denied",
)
_STAGE_RECOVERY = {
    (operation, code, stage): replace(info, localization_key=f"{operation}.{code}.{stage}")
    for (operation, code, stage), info in _STAGE_RECOVERY.items()
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
