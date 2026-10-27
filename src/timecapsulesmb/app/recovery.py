from __future__ import annotations

from dataclasses import dataclass, replace

from timecapsulesmb.core.error_codes import ErrorCode
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
    # Steps only the CLI has, after the app's own (cli_steps); never sent to
    # the app.
    cli_extra: tuple[str, ...] = ()

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


# A failing disk's way out: the data first, then a new disk.
_DISK_FAILING_STEPS = (
    "Copy off the files you need. If file sharing is off, take the disk out and connect it to a Mac "
    "with a SATA-to-USB adapter.",
    "Replace the disk.",
    "Erase the new disk in AirPort Utility, then run “Install / Update Samba”. Time Machine starts a new, "
    "full backup on it.",
)

# The app and its helper from different releases send requests the other does not know.
_MISMATCH_TITLE = "TimeCapsuleSMB components do not match"
_MISMATCH_MESSAGE = "Parts of this TimeCapsuleSMB installation are from different versions."
_MISMATCH_STEPS = (
    "Update or reinstall TimeCapsuleSMB.",
    "If “Helper path” is set in “Settings”, clear it.",
)

_DEFAULTS: dict[str, RecoveryInfo] = {
    ErrorCode.INVALID_REQUEST: RecoveryInfo(
        _MISMATCH_TITLE,
        _MISMATCH_MESSAGE,
        _MISMATCH_STEPS,
        retryable=True,
    ),
    ErrorCode.UNKNOWN_PARAM: RecoveryInfo(
        _MISMATCH_TITLE,
        _MISMATCH_MESSAGE,
        _MISMATCH_STEPS,
        # Resending the same request fails the same way.
        retryable=False,
    ),
    ErrorCode.UNKNOWN_OPERATION: RecoveryInfo(
        _MISMATCH_TITLE,
        _MISMATCH_MESSAGE,
        _MISMATCH_STEPS,
        retryable=False,
    ),
    ErrorCode.VALIDATION_FAILED: RecoveryInfo(
        "Request validation failed",
        "One or more operation parameters were missing or invalid.",
        ("Review the highlighted fields.", "Retry with valid values."),
        retryable=True,
    ),
    ErrorCode.DEVICE_IDENTITY_MISMATCH: RecoveryInfo(
        "Device identity could not be confirmed",
        "The device at this address did not identify itself as the saved device.",
        ("Refresh discovery and reconnect the saved device.",),
        retryable=True,
        suggested_operation="configure",
    ),
    ErrorCode.CONFIG_ERROR: RecoveryInfo(
        "Configuration error",
        "The current .env configuration could not be read or used.",
        ("Open the configuration step.", "Verify host, password, and SSH options."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED: RecoveryInfo(
        "Local Network access blocked",
        "macOS is blocking TimeCapsuleSMB from accessing devices on your local network.",
        (
            "Open “System Settings” > “Privacy & Security” > “Local Network”.",
            "Enable TimeCapsuleSMB in “Local Network”.",
            "Quit and reopen TimeCapsuleSMB, then try again.",
            # macOS can block a copy of the app that "Local Network" shows as
            # allowed, as when several copies of the app are on the Mac.
            "If TimeCapsuleSMB is already on, turn it off and on again. If that doesn't help, "
            "delete other copies of TimeCapsuleSMB and restart the Mac.",
        ),
        # No suggested operation: Add Device and SSH Access both show this
        # card, and each retries its own operation.
        retryable=True,
        action_ids=("open_system_settings", "retry"),
    ),
    # The helper's own message names the address and this Mac's networks.
    ErrorCode.DEVICE_OFF_NETWORK: RecoveryInfo(
        "Device not on this Mac's network",
        "The device's address isn't on this Mac's network. Check the address, or connect this Mac "
        "to the device's network by Wi-Fi or one of its LAN ports, then try again.",
        (),
        retryable=True,
        cli_extra=(
            "Check the address, or connect this computer to the device's network by Wi-Fi or one of its LAN ports, "
            "then try again.",
        ),
    ),
    # No step: the filtering app may be a VPN the user cannot turn off.
    ErrorCode.LOCAL_NETWORK_FILTERED: RecoveryInfo(
        "Connection blocked on this Mac",
        LOCAL_NETWORK_FILTERED_MESSAGE,
        (),
        retryable=True,
    ),
    ErrorCode.AUTH_FAILED: RecoveryInfo(
        "Authentication failed",
        "The device rejected the supplied password or SSH credentials.",
        ("Re-enter the AirPort admin password.", "Verify that SSH is enabled on the device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    # Shared by causes that are not an unsupported model, such as flash or Activate
    # on NetBSD 6, so it must not tell the user to forget a working device.
    ErrorCode.UNSUPPORTED_DEVICE: RecoveryInfo(
        "Unsupported device",
        "This operation is not supported on the detected AirPort model or OS.",
        (
            "Check the detected model and OS.",
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
        ),
        retryable=False,
    ),
    ErrorCode.SSH_COMPATIBILITY_FAILED: RecoveryInfo(
        "SSH connection could not be set up",
        "This Mac's SSH program and the device could not agree on how to secure the connection.",
        (
            "Update TimeCapsuleSMB, then try again.",
            "If it keeps failing, click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=True,
        suggested_operation="configure",
    ),
    # The SSH probe's other outcomes (services.runtime.probe_failure_error).
    # None of them says anything about the model, so none may suggest it is
    # unsupported or that the device should be forgotten.
    ErrorCode.SSH_DISABLED: RecoveryInfo(
        "SSH is turned off",
        "The device answers AirPort ACP, but its SSH port is closed. SSH turns off after a reset, "
        "or when it is disabled in “SSH Access”.",
        ("Open “SSH Access” and choose “Enable SSH”, then try again.",),
        retryable=True,
        action_ids=("open_ssh_access",),
    ),
    ErrorCode.DEVICE_UNREACHABLE: RecoveryInfo(
        "Device not reachable",
        "Neither SSH nor AirPort ACP answered at the device's saved address.",
        (
            "Make sure the device is turned on and connected to the same network or Wi-Fi as this Mac.",
            "If the device is restarting, wait a few minutes, then try again.",
        ),
        retryable=True,
        cli_extra=("Run `tcapsule doctor` to see which AirPorts answer on this network.",),
    ),
    ErrorCode.SSH_CLIENT_FAILED: RecoveryInfo(
        "SSH on this Mac failed",
        "The SSH program on this Mac could not finish.",
        (
            "Try again.",
            "If it keeps failing, click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=True,
    ),
    ErrorCode.SSH_TRANSPORT_FAILED: RecoveryInfo(
        "SSH connection dropped",
        "The device accepted the SSH connection, then closed it before login. "
        "This can happen while the device is starting up or busy.",
        ("Wait a minute, then try again.", "If this keeps happening, restart the device."),
        retryable=True,
    ),
    ErrorCode.DEVICE_PROBE_FAILED: RecoveryInfo(
        "Device check failed",
        "TimeCapsuleSMB logged in over SSH but could not read the device's system information.",
        ("Try again.", "Run “Checkup” for details."),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # Configure and set-ssh: ACP took the request but SSH did not open within
    # services.reboot.REBOOT_UP_TIMEOUT_SECONDS.
    ErrorCode.SSH_ENABLE_TIMEOUT: RecoveryInfo(
        "SSH has not opened yet",
        "Turning on SSH restarts the device. Some devices take longer to restart than TimeCapsuleSMB waits.",
        ("Wait a few minutes, then try again.", "If SSH still does not open, restart the device, then try again."),
        retryable=True,
    ),
    # services.reboot.reboot_device: every reboot that does not finish.
    ErrorCode.REBOOT_NOT_STARTED: RecoveryInfo(
        "Reboot did not start",
        "The reboot request was sent, but the device did not restart.",
        ("Power-cycle the device.", "Try again once the device is reachable."),
        retryable=True,
        suggested_operation="doctor",
    ),
    ErrorCode.REBOOT_NOT_FINISHED: RecoveryInfo(
        "Reboot did not finish",
        (
            "The device went offline or restarted, but did not come back in time. It may still be "
            "starting up, or it may have a new IP address."
        ),
        (
            "Wait a few more minutes.",
            "Make sure you are connected to the same network or Wi-Fi as the device.",
            "If the device still doesn't answer, unplug it, wait 10 seconds and plug it back in.",
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # Apple's firmware, not ours: a stuck ACPd refused before anything changed,
    # tore itself down without restarting the device, stopped answering
    # syUT (services.runtime.require_acpd_ready, services.reboot), or left its
    # port unanswered on a device that answers on port 22 (services.acp_ssh,
    # checks.network.closed_ssh_port_error).
    ErrorCode.ACPD_BROKEN: RecoveryInfo(
        "Apple ACPd is broken",
        (
            "Apple's AirPort service (ACPd) on the device is stuck or no longer running. This is a bug in Apple's "
            "firmware, not TimeCapsuleSMB."
        ),
        (
            "Unplug the device, wait 10 seconds and plug it back in.",
            "Wait 2 minutes for the device to start up, then try again.",
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # ACPd quit: the wait for the runtime ended once ACPd was gone, Activate
    # refused before starting it (services.activation.require_acpd_for_start),
    # or a command refused before changing anything (services.runtime.
    # require_acpd_ready), where a Samba started before ACPd quit may still
    # serve. A restart brings ACPd back; NetBSD 4 then needs Activate unless
    # its boot hook starts file sharing.
    ErrorCode.ACPD_QUIT: RecoveryInfo(
        "Apple ACPd quit",
        (
            "Apple's AirPort service (ACPd) on the device quit and does not start again until the device "
            "restarts. Without it, file sharing cannot start and the device cannot be updated or restarted over "
            "the network. This is a bug in Apple's firmware, not TimeCapsuleSMB."
        ),
        (
            "Unplug the device, wait 10 seconds and plug it back in.",
            "Wait 2 minutes for the device to start up, then run “Checkup”.",
            "On NetBSD 4 devices, run “Activate” if the checkup says Samba is not running.",
        ),
        retryable=False,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # ACPd answers the device's own acp and, through SSH, the network, but
    # not this Mac: the path is blocked (services.runtime.require_acpd_ready).
    ErrorCode.ACP_UNREACHABLE: RecoveryInfo(
        "AirPort ACP not reachable",
        "This Mac cannot reach AirPort ACP (port 5009) on the device, although the device's own ACP works.",
        (
            "Connect this Mac to the device's network by Wi-Fi or one of its LAN ports.",
            "Turn off any VPN or firewall that filters local network traffic, then try again.",
        ),
        retryable=True,
    ),
    # fsck: the reboot finished, but the installed runtime did not start again
    # (services.activation.start_netbsd4_runtime_after_reboot).
    ErrorCode.RUNTIME_NOT_RESTARTED: RecoveryInfo(
        "File sharing did not restart",
        "The device restarted, but the installed TimeCapsuleSMB services did not start.",
        ("Run “Activate”.", "Run “Checkup” for details."),
        retryable=True,
        suggested_operation="activate",
        action_ids=("start_smb", "run_checkup"),
    ),
    # Activation or a deploy's check found no payload on any disk
    # (device.errors.DiskPayloadMissingError).
    ErrorCode.PAYLOAD_MISSING_FROM_DISK: RecoveryInfo(
        "Samba files are missing",
        "The TimeCapsuleSMB files are missing from every disk on the device, as after an Erase in AirPort Utility.",
        ("Run “Install / Update Samba”.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    # A payload binary on the disk did not match its deployed checksum
    # (device.errors.DiskPayloadDamagedError).
    ErrorCode.PAYLOAD_DAMAGED_ON_DISK: RecoveryInfo(
        "Samba files are damaged",
        "The TimeCapsuleSMB files on the device's disk are damaged and no longer match what was installed.",
        ("Run “Install / Update Samba”.", "If the files are damaged again after reinstalling, the disk may be failing."),
        retryable=False,
        suggested_operation="deploy",
    ),
    # The payload has no rsync or rsyncd.conf; Samba serves without it
    # (device.errors.RsyncMissingError).
    ErrorCode.RSYNC_MISSING_FROM_DISK: RecoveryInfo(
        "rsync files are missing",
        "The rsync files are missing from the disk, so rsync is off. File sharing and Time Machine keep working.",
        ("Run “Install / Update Samba”.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    # rsync on the disk did not match its deployed checksum; Samba serves
    # without it (device.errors.RsyncDamagedError).
    ErrorCode.RSYNC_DAMAGED_ON_DISK: RecoveryInfo(
        "rsync file is damaged",
        "The rsync file on the disk is damaged and no longer matches what was installed, so rsync is off. "
        "File sharing and Time Machine keep working.",
        ("Run “Install / Update Samba”.", "If the files are damaged again after reinstalling, the disk may be failing."),
        retryable=False,
        suggested_operation="deploy",
    ),
    # The disk could not return smbd or its checksums, or only too slowly
    # (device.errors.DiskPayloadUnreadableError): the manager's own evidence
    # of a failing disk, which NetBSD 4's kernel log often keeps no line of.
    # The way out is the failing disk's.
    ErrorCode.PAYLOAD_UNREADABLE_ON_DISK: RecoveryInfo(
        "The disk cannot read the Samba files",
        "The device's disk could not read the TimeCapsuleSMB files, or only too slowly, so it is probably failing "
        "and file sharing is off. Reinstalling will not fix the disk, and AirPort Utility may still show it as OK.",
        _DISK_FAILING_STEPS,
        retryable=False,
    ),
    # The disk could not return rsync, or only too slowly; Samba serves
    # without it (device.errors.RsyncUnreadableError). The disk is failing,
    # so a reinstall would not help: the data comes first, then a new disk.
    ErrorCode.RSYNC_UNREADABLE_ON_DISK: RecoveryInfo(
        "The disk cannot read rsync",
        "The device's disk could not read the rsync file, or only too slowly, so rsync is off and the disk is "
        "probably failing. File sharing and Time Machine keep working for now.",
        (
            "Copy off the files you need.",
            "Replace the disk.",
            "Erase the new disk in AirPort Utility, then run “Install / Update Samba”. Time Machine starts a new, "
            "full backup on it.",
        ),
        retryable=False,
    ),
    # File sharing serves, but rsync did not start, with no verdict saying why
    # (device.errors.RsyncNotRunningError): a port in use, a configuration
    # rsync refuses, a crash. Checkup says which; a reinstall fixes only some.
    ErrorCode.RSYNC_NOT_RUNNING: RecoveryInfo(
        "rsync is not running",
        "File sharing and Time Machine are running, but rsync did not start.",
        ("Run “Checkup” for details.", "Run “Install / Update Samba”."),
        retryable=False,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    # This boot's kernel log says the disk failed transfers it could not
    # recover from, and file sharing is off (device.disk_errors). Nothing on
    # the device fixes that: the data comes first, then a new disk.
    ErrorCode.DISK_FAILING: RecoveryInfo(
        "The disk is failing",
        (
            "The device's disk reported errors it could not recover from, so it is probably failing. File sharing "
            "and Time Machine will not work reliably on it, and reinstalling will not fix the disk. AirPort Utility "
            "may still show the disk as OK: its own check does not catch these errors."
        ),
        _DISK_FAILING_STEPS,
        retryable=False,
    ),
    # The internal disk reports a size far below its real one, and its identity
    # without Apple's prefix (device.storage.disk_size_misreported). Erasing it
    # would format it as that size, so no step offers an erase or a wipe.
    ErrorCode.DISK_SIZE_MISREPORTED: RecoveryInfo(
        "The disk is reporting the wrong size",
        "The device reports the internal disk as much smaller than it is. A restart sometimes clears this; "
        "if it keeps happening, the disk is probably failing.",
        (
            "Restart the device, then run “Install / Update Samba” again after the disk spins up.",
            "If it keeps reporting the wrong size, the disk needs replacing. Its files cannot be reached while it "
            "does this; a data recovery service may be able to get them back.",
            "Run “Checkup” and copy the diagnostics if you need help.",
        ),
        retryable=True,
    ),
    # AirPort Utility's erase formatted volumes on a partition table Apple's
    # gpt could not replace, and Install, Disk Repair, Uninstall or Wipe
    # Internal Disk (USB volumes only) stopped because its restart would take
    # them and anything saved there (services.storage.require_no_in_memory_volumes).
    ErrorCode.DISK_VOLUMES_IN_MEMORY: RecoveryInfo(
        "The disk's volumes vanish at the next restart",
        "AirPort Utility's “Erase Disk” could not replace the internal disk's partition table, so its volumes exist "
        "only until the next restart. Anything saved on them since that erase, Time Machine backups included, is "
        "lost at the next restart. This restarts the device, so it stopped before changing anything.",
        (
            "Copy off anything you need from the disk first.",
            "Then use “Wipe Internal Disk”. It deletes everything on the internal disk and formats it for the "
            "device.",
            "Try again after the wipe.",
        ),
        retryable=False,
        action_ids=("wipe_disk",),
    ),
    # Wipe Internal Disk never takes a USB disk, so no button: disconnecting it
    # lets the command go on.
    ErrorCode.USB_DISK_VOLUMES_IN_MEMORY: RecoveryInfo(
        "The disk's volumes vanish at the next restart",
        "AirPort Utility's “Erase Disk” could not replace the USB disk's partition table, so its volumes exist "
        "only until the next restart. Anything saved on them since that erase, Time Machine backups included, is "
        "lost at the next restart. This restarts the device, so it stopped before changing anything.",
        (
            "Copy off anything you need from the disk first.",
            "Then disconnect the disk from the device and try again.",
            "Before connecting the disk to the device again, erase it on a Mac in Disk Utility as "
            "“Mac OS Extended (Journaled)”.",
        ),
        retryable=False,
    ),
    # Wipe Internal Disk's own codes carry no "wipe_disk" action: Wipe
    # Internal Disk's screen is the one showing them.
    # Wipe Internal Disk stopped before it changed the disk
    # (services.wipe_disk): the disk's size unreadable, the erase request not
    # accepted, or file sharing not stopped. A restart clears each.
    ErrorCode.WIPE_DISK_RESTART_NEEDED: RecoveryInfo(
        "Restart the device first",
        "The device could not start wiping the disk. Nothing on the disk was changed.",
        (
            "Restart the device.",
            "Then run “Wipe Internal Disk” again.",
            "If it still does not start, try “Erase Disk” in AirPort Utility instead.",
        ),
        retryable=True,
    ),
    # A format of this disk still running at the end of the wait: a restart
    # now would leave a broken volume.
    ErrorCode.WIPE_DISK_ERASE_RUNNING: RecoveryInfo(
        "An erase is still running",
        "The device is still erasing or formatting this disk. Restarting it now could leave the disk unusable.",
        ("Wait half an hour.", "Then restart the device, or run “Wipe Internal Disk” again."),
        retryable=True,
    ),
    # diskd is formatting a disk, or checking or mounting a volume, before the
    # wipe (services.wipe_disk.require_no_disk_jobs): its jobs name no disk.
    ErrorCode.WIPE_DISK_JOB_RUNNING: RecoveryInfo(
        "The device is busy with a disk",
        "The device is formatting a disk, or checking or mounting a volume. Wiping a disk now could interrupt it.",
        ("Wait until it finishes.", "Then run “Wipe Internal Disk” again."),
        retryable=True,
    ),
    # Another disk carries Apple's partition names, or its tables could not
    # be read (services.wipe_disk.disks_with_apple_labels): the device's erase
    # could format that disk instead of the internal one.
    ErrorCode.WIPE_DISK_OTHER_DISK: RecoveryInfo(
        "Disconnect the other disk first",
        "Another connected disk has partitions named like an internal disk's, or could not be read. While it is "
        "connected, the device's erase can format that disk instead of the internal one.",
        (
            "Disconnect every USB disk from the device.",
            "Then run “Wipe Internal Disk” again.",
            "Connect the USB disks again after the wipe.",
        ),
        retryable=True,
    ),
    # diskd said it is already erasing this disk: a secure erase started in
    # AirPort Utility runs for hours, and a restart would cut it short.
    ErrorCode.WIPE_DISK_BUSY: RecoveryInfo(
        "Another erase is running",
        "The device is already erasing this disk, perhaps with a secure erase started in AirPort Utility, which can "
        "take hours. Restarting the device now could leave the disk unusable.",
        ("Wait until that erase finishes.", "Then run “Wipe Internal Disk” again."),
        retryable=True,
    ),
    # MaSt no longer lists the internal disk under the confirmed uuid, or
    # drvctl's size differs right before the zeroing (services.wipe_disk).
    # Nothing was written; in the second case file sharing is already off,
    # and the restart turns it back on.
    ErrorCode.WIPE_DISK_CHANGED: RecoveryInfo(
        "The disk no longer matches",
        "The device stopped reporting the internal disk the way it did when you confirmed the wipe, so nothing was "
        "wiped.",
        ("Restart the device.", "Then run “Wipe Internal Disk” again."),
        retryable=True,
    ),
    # Past the wipe: the device did not format the disk, or the new volume was
    # gone after the restart. The disk's old files are gone either way.
    ErrorCode.WIPE_DISK_ERASE_FAILED: RecoveryInfo(
        "The disk was not formatted",
        "The device did not finish formatting the disk after wiping it, or the new volume was gone after the "
        "restart. The files that were on the disk are gone.",
        ("Run “Wipe Internal Disk” again.", "If it fails again, the disk is probably failing; replace it."),
        retryable=True,
    ),
    ErrorCode.SSH_STILL_ENABLED: RecoveryInfo(
        "SSH is still enabled",
        "The device restarted, but SSH was still enabled afterwards.",
        ("Disable SSH again in “SSH Access”.", "If SSH stays enabled, restart the device, then try again."),
        retryable=True,
        action_ids=("open_ssh_access",),
    ),
    ErrorCode.CONFIRMATION_REQUIRED: RecoveryInfo(
        "Confirmation required",
        "This operation changes the device and needs explicit confirmation.",
        ("Review the plan.", "Confirm the operation in the app before retrying."),
        retryable=True,
    ),
    ErrorCode.CANCELLED: RecoveryInfo(
        "Operation cancelled",
        "It stopped before it finished.",
        ("Try again when you are ready.",),
        retryable=True,
    ),
    # The device's own error shows below the guidance, so this stays general.
    ErrorCode.REMOTE_ERROR: RecoveryInfo(
        "The device reported an error",
        "A step on the device did not finish as expected.",
        (
            "Make sure the device is turned on and on the same network as this Mac, then try again.",
            "If it keeps failing, run “Checkup”.",
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    ErrorCode.OPERATION_FAILED: RecoveryInfo(
        "Something went wrong",
        "TimeCapsuleSMB ran into a problem it did not expect.",
        (
            "Try again.",
            "If it keeps failing, click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=True,
    ),
    ErrorCode.DISK_WRITE_TEST_UNRESPONSIVE: RecoveryInfo(
        "The disk did not respond",
        "The disk did not answer a test write. It may be failing or unable to spin up.",
        ("Run “Disk Repair”.", "If this keeps happening, the disk may need replacing."),
        retryable=True,
        action_ids=("disk_repair",),
    ),
}


_STUCK_DEVICE_STEPS = (
    "Unplug the device, wait 10 seconds and plug it back in.",
    "Wait 2 minutes for the device to start up, then run “Install / Update Samba” again.",
)
_MIGRATION_DISK_MESSAGE = (
    "During the install, Samba metadata from the earlier version moves into the files' extended attributes. "
    "What was already moved is kept. A slow or failing disk often causes this."
)
_MIGRATION_DISK_STEPS = ("Run “Disk Repair”.", "Then run “Install / Update Samba” again.")
_MIGRATION_RERUN_MESSAGE = (
    "What was already moved into the files' extended attributes is kept; the rest moves when the install runs again."
)
_UNREADABLE_FOLDER_TITLE = "A folder on the disk cannot be read"
_UNREADABLE_FOLDER_MESSAGE = (
    "The device could not list the contents of a folder the Samba metadata migration must read. "
    "Samba was stopped for the install and is not running now."
)
_UNREADABLE_FOLDER_REPAIR = "Run “Disk Repair” on the disk, then run “Install / Update Samba” again."

_OPERATION_CODE_RECOVERY: dict[tuple[str, str], RecoveryInfo] = {
    ("deploy", ErrorCode.PAYLOAD_UPLOAD_TIMEOUT): RecoveryInfo(
        "Copying the installation files timed out",
        "The disk may be failing or slow to spin up, or the connection to the device may be too slow, "
        "such as a weak Wi-Fi link.",
        (
            "Run “Disk Repair”.",
            "Connect this Mac or the device by Ethernet, then run “Install / Update Samba” again.",
            "If this keeps happening, the disk may need replacing.",
        ),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("deploy", ErrorCode.MANAGER_STOP_TIMEOUT): RecoveryInfo(
        "A service on the device is stuck",
        "It did not stop for the install. A failing disk often causes this.",
        _STUCK_DEVICE_STEPS,
        retryable=True,
    ),
    ("deploy", ErrorCode.PREVIOUS_MIGRATION_STALLED): RecoveryInfo(
        "An earlier metadata migration is stuck",
        "A Samba metadata migration left running by an earlier install stopped making progress. "
        "A failing disk often causes this.",
        _STUCK_DEVICE_STEPS,
        retryable=True,
    ),
    ("deploy", ErrorCode.INSUFFICIENT_FLASH_SPACE): RecoveryInfo(
        "Not enough room in the device's flash storage",
        "The device's built-in flash storage has no room for the TimeCapsuleSMB startup files, "
        "even after removing the old ones.",
        (
            "Click “Copy Diagnostics” and report it at https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=False,
    ),
    ("deploy", ErrorCode.FLASH_CAPACITY_PROBE_FAILED): RecoveryInfo(
        "Could not check the device's flash storage",
        "The device did not report how much room its built-in flash storage has, so the install stopped.",
        ("Run “Install / Update Samba” again.",),
        retryable=True,
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_TIMEOUT): RecoveryInfo(
        "The metadata migration took too long",
        _MIGRATION_DISK_MESSAGE,
        _MIGRATION_DISK_STEPS,
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_STALLED): RecoveryInfo(
        "The metadata migration made no progress",
        _MIGRATION_DISK_MESSAGE,
        _MIGRATION_DISK_STEPS,
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_CRASHED): RecoveryInfo(
        "The metadata migration crashed",
        _MIGRATION_RERUN_MESSAGE,
        ("Run “Install / Update Samba” again.",),
        retryable=True,
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_STOPPED): RecoveryInfo(
        "The metadata migration was stopped",
        _MIGRATION_RERUN_MESSAGE,
        ("Run “Install / Update Samba” again.",),
        retryable=True,
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_CONNECTION_LOST): RecoveryInfo(
        "Connection lost during the metadata migration",
        "The migration may still be running on the device. Running the install again waits for it to finish.",
        ("Run “Install / Update Samba” again.",),
        retryable=True,
    ),
    ("deploy", ErrorCode.XATTR_MIGRATION_FAILED): RecoveryInfo(
        "The metadata migration failed",
        "Samba metadata from the earlier version could not all be moved into the files' extended attributes. "
        "What was already moved is kept.",
        (
            *_MIGRATION_DISK_STEPS,
            "If it still fails, click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("deploy", ErrorCode.REBOOT_NOT_FINISHED): RecoveryInfo(
        "Reboot did not finish",
        "The installation files were copied and the restart was requested, but the device did not "
        "answer over SSH again in time. It may still be starting up, or it may have come back "
        "at a different IP address.",
        (
            "Wait a few more minutes.",
            "Make sure you are connected to the same network or Wi-Fi as the device.",
            "On NetBSD 4 devices, run “Activate” once SSH is reachable; the install did not "
            "get far enough to activate Samba after the reboot.",
            "If your device resets itself, see "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues/177.",
            "If the device still doesn't answer, unplug it, wait 10 seconds and plug it back "
            "in.",
        ),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
        # The app offers a device that answers at a new address to save it.
        cli_extra=("If the device answers at a new IP address, update TC_HOST or run `tcapsule configure` again.",),
    ),
    ("configure", ErrorCode.AUTH_FAILED): RecoveryInfo(
        "AirPort password rejected",
        "ACP or SSH authentication failed while configuring the device.",
        ("Re-enter the AirPort admin password.", "Confirm the selected device is the intended Apple device."),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    ("configure", ErrorCode.UNSUPPORTED_DEVICE): RecoveryInfo(
        "Unsupported device",
        "This AirPort model cannot run TimeCapsuleSMB.",
        (
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
            "Add your Time Capsule or AirPort Extreme instead.",
        ),
        retryable=False,
    ),
    # The device answered SSH but its ACPd quit, so its AirPort identity
    # cannot be read: after the restart, adding it again works.
    ("configure", ErrorCode.ACPD_QUIT): RecoveryInfo(
        "Apple ACPd quit",
        (
            "Apple's AirPort service (ACPd) on the device quit, so the device cannot be identified until it "
            "restarts. This is a bug in Apple's firmware, not TimeCapsuleSMB."
        ),
        (
            "Unplug the device, wait 10 seconds and plug it back in.",
            "Wait 2 minutes for the device to start up, then try again.",
        ),
        retryable=True,
        suggested_operation="configure",
    ),
    ("configure", ErrorCode.SSH_COMPATIBILITY_FAILED): RecoveryInfo(
        "SSH connection could not be set up",
        "The device offers only older SSH security methods, and this Mac's SSH program did not "
        "accept them.",
        (
            "Update TimeCapsuleSMB, then try again.",
            "If it keeps failing, click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues.",
        ),
        retryable=True,
        suggested_operation="configure",
    ),
    ("deploy", ErrorCode.CONFIRMATION_REQUIRED): RecoveryInfo(
        "Install confirmation required",
        "“Install / Update Samba” needs confirmation before uploading the installation files, rebooting, or activating NetBSD4.",
        ("Review the install plan.", "Confirm the install and any required reboot or activation prompt."),
        retryable=True,
    ),
    ("deploy", ErrorCode.VALIDATION_FAILED): RecoveryInfo(
        "The install could not start",
        "Some of the app's installation files are missing, or a setting is not valid.",
        (
            "Open “Diagnostics”.",
            "Reinstall TimeCapsuleSMB if files are missing, or correct the setting, then try "
            "again.",
        ),
        retryable=True,
        suggested_operation="validate-install",
        action_ids=("open_diagnostics",),
    ),
    ("deploy", ErrorCode.UNSUPPORTED_DEVICE): RecoveryInfo(
        "Unsupported device",
        "This AirPort model cannot run TimeCapsuleSMB.",
        (
            "TimeCapsuleSMB supports AirPort Time Capsule and AirPort Extreme. AirPort Express is not supported.",
            "Forget this device, then add your Time Capsule or AirPort Extreme.",
        ),
        retryable=False,
    ),
    ("deploy", ErrorCode.DEPLOY_NO_DISK_DETECTED): RecoveryInfo(
        "No internal disk detected",
        "The device did not report any internal disk.",
        (
            "Check that the disk is connected and seated.",
            "Power-cycle the device and retry after the disk spins up.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.DEPLOY_NO_USB_DISK_DETECTED): RecoveryInfo(
        "No USB disk detected",
        "An AirPort Extreme has no internal disk, and the device reported no USB disk.",
        (
            "Connect a USB disk formatted for Mac (HFS+).",
            "If a disk is connected, check its power and cable, then retry.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.DEPLOY_NO_HFS_PARTITION): RecoveryInfo(
        "The disk is not ready for installation",
        "A disk was found, but it has no usable HFS partition for TimeCapsuleSMB. "
        "The disk may need formatting (for example, if it is formatted as APFS, exFAT, FAT32 or NTFS), "
        "its partition information may be damaged, or the disk itself may be failing.",
        (
            "Try running “Install / Update Samba” again in a few minutes.",
            "If it still fails, restart the device and try again after the disk spins up.",
            "If it is the internal disk and it holds nothing you need, use “Wipe Internal Disk”. It deletes everything "
            "on the internal disk and formats it for the device.",
            "Run “Install / Update Samba” again after the wipe.",
        ),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("wipe_disk",),
    ),
    ("deploy", ErrorCode.DEPLOY_DISK_NOT_WRITABLE): RecoveryInfo(
        "No writable volume for the installation files",
        "The device found HFS volumes, but none accepted the TimeCapsuleSMB installation folder.",
        (
            "Wake or remount the disk.",
            "Check available free space.",
            "Run “Install / Update Samba” again.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.DEPLOY_DISK_NOT_MOUNTED): RecoveryInfo(
        "HFS disk not mounted",
        "The disk has an HFS volume, but the device has not mounted it, and did not when asked.",
        (
            "Wait a minute, then run “Install / Update Samba” again.",
            "Restart the device if the disk still does not mount.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    # Deploy offers this only when uninstall deletes every legacy database
    # (services.deploy), so the second step cannot fail the same way.
    ("deploy", ErrorCode.XATTR_MIGRATION_UNREADABLE_FOLDER): RecoveryInfo(
        _UNREADABLE_FOLDER_TITLE,
        _UNREADABLE_FOLDER_MESSAGE,
        (
            _UNREADABLE_FOLDER_REPAIR,
            "If it still fails, run “Uninstall”, then “Install / Update Samba”. Uninstall also deletes any Samba "
            "metadata from an earlier version that is not migrated yet, such as Finder tags; your files stay on "
            "the disk.",
        ),
        # Running deploy again unchanged fails the same way.
        retryable=False,
        action_ids=("disk_repair", "uninstall"),
    ),
    # A legacy database outside the install folder survives Uninstall, and one
    # on a disk this deploy could not mount was never seen, so the next deploy
    # could walk the same folder: Uninstall is not offered. Copy Diagnostics is
    # on every recovery card.
    ("deploy", ErrorCode.XATTR_MIGRATION_UNREADABLE_FOLDER_REPAIR_ONLY): RecoveryInfo(
        _UNREADABLE_FOLDER_TITLE,
        _UNREADABLE_FOLDER_MESSAGE,
        (
            _UNREADABLE_FOLDER_REPAIR,
            "If it still fails, the disk may be failing. Click “Copy Diagnostics” and report it at "
            "https://github.com/jamesyc/TimeCapsuleSMB/issues. Reports there are public, so remove any names you "
            "do not want to share.",
        ),
        retryable=False,
        action_ids=("disk_repair",),
    ),
    ("deploy", ErrorCode.DEPLOY_DISK_NOT_CONFIRMED): RecoveryInfo(
        "Disk not kept mounted",
        "An HFS volume is mounted, but the device would not keep it mounted for TimeCapsuleSMB, "
        "so it could be unmounted during the install.",
        ("Wait a minute, then run “Install / Update Samba” again.", "Restart the device if this keeps happening."),
        retryable=True,
        suggested_operation="deploy",
    ),
    # services.deploy raises it only when the connection to the device was lost
    # while the old runtime was stopped and the install unfinished.
    ("deploy", ErrorCode.INSTALL_INTERRUPTED): RecoveryInfo(
        "Install interrupted",
        "The install was interrupted. Run “Install / Update Samba” again to complete the install.",
        (),
        retryable=True,
        cli_extra=("Run `tcapsule deploy` again to complete the install.",),
    ),
    ("flash", ErrorCode.SECONDARY_BANK_INVALID): RecoveryInfo(
        "Backup firmware bank is damaged",
        "The secondary (backup) firmware bank is not a valid copy, so patching the primary bank is refused: "
        "an interrupted write would leave nothing to start from.",
        (
            "Choose “Plan Restore” to rewrite the backup bank with Apple firmware.",
            "Then choose “Back Up and Inspect Again”, and patch.",
        ),
        retryable=False,
    ),
    ("flash", ErrorCode.SECONDARY_BANK_READ_MISMATCH): RecoveryInfo(
        "Backup firmware bank read inconsistently",
        "This backup's read of the secondary (backup) firmware bank does not agree with the device's own check "
        "of it. The bank may still be a good copy, so it is not rewritten and patching is refused.",
        (
            "Choose “Back Up and Inspect”.",
            "If the reads keep disagreeing, leave the firmware as it is: this device's flash may be failing.",
        ),
        retryable=False,
        cli_extra=(
            "When patching, `tcapsule flash --patch --force` patches the primary bank anyway, after you review "
            "the backup status.",
        ),
    ),
    # Patching needs both banks to be valid backups and the primary to be the
    # bank the device runs; the CLI alone can patch anyway.
    ("flash", ErrorCode.FLASH_PATCH_REFUSED): RecoveryInfo(
        "Patching was refused",
        "The firmware banks are not in a state where patching is safe, so the firmware was not changed.",
        (
            "Choose “Back Up and Inspect Again” and review each bank's status.",
            "If a bank is damaged, leave the firmware as it is.",
        ),
        retryable=False,
        cli_extra=("`tcapsule flash --patch --force` patches the primary bank anyway, after you review that status.",),
    ),
    ("activate", ErrorCode.RUNTIME_NOT_INSTALLED): RecoveryInfo(
        "TimeCapsuleSMB not installed",
        "The device has no TimeCapsuleSMB installation to start.",
        ("Run “Install / Update Samba”.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    ("activate", ErrorCode.RUNTIME_OUTDATED): RecoveryInfo(
        "Installation is out of date",
        "The installed TimeCapsuleSMB is too old for this app to start or check.",
        ("Run “Install / Update Samba”.",),
        retryable=False,
        suggested_operation="deploy",
    ),
    ("activate", ErrorCode.CLIENT_OUTDATED): RecoveryInfo(
        "App is out of date",
        "The installed TimeCapsuleSMB is from a newer major version, which this app cannot start or check.",
        ("Update TimeCapsuleSMB, then start it again.",),
        retryable=False,
    ),
    ("activate", ErrorCode.CONFIRMATION_REQUIRED): RecoveryInfo(
        "Activation confirmation required",
        "NetBSD4 activation starts the deployed runtime and must be confirmed.",
        ("Review the NetBSD4 activation guidance.", "Confirm activation before retrying."),
        retryable=True,
        action_ids=("start_smb",),
    ),
    ("uninstall", ErrorCode.CONFIRMATION_REQUIRED): RecoveryInfo(
        "Uninstall confirmation required",
        "Uninstall removes managed files and may reboot the device.",
        ("Review the uninstall plan.", "Confirm uninstall and reboot before retrying."),
        retryable=True,
        action_ids=("uninstall",),
    ),
    ("fsck", ErrorCode.CONFIRMATION_REQUIRED): RecoveryInfo(
        "Disk repair confirmation required",
        "Disk repair runs fsck, stops file sharing, unmounts the selected HFS disk, and may reboot the device.",
        ("Review the selected volume.", "Confirm disk repair before retrying."),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("fsck", ErrorCode.VALIDATION_FAILED): RecoveryInfo(
        "Volume selection failed",
        "“Disk Repair” could not choose a mounted HFS volume.",
        (
            "Select a specific HFS volume.",
            "Refresh the volume list, then try again.",
        ),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    # A repair whose session dropped keeps running on the device
    # (services.maintenance): a second one must wait for it.
    ("fsck", ErrorCode.FSCK_ALREADY_RUNNING): RecoveryInfo(
        "Disk repair already running",
        "fsck_hfs from an earlier disk repair is still running on the device. Restarting or unplugging the device now would interrupt it.",
        (
            "Keep the Time Capsule plugged in and wait; on a large disk the repair can take an hour or more.",
            "Then run “Disk Repair” again. It restarts the device when it finishes.",
        ),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    # fsck_hfs never started on a volume still mounted, and file sharing,
    # stopped for it, stays off until a restart starts it again.
    ("fsck", ErrorCode.FSCK_VOLUME_NOT_UNMOUNTED): RecoveryInfo(
        "The disk could not be unmounted",
        "The repair did not start because the disk could not be confirmed unmounted. File sharing stays off "
        "until the device restarts.",
        ("Restart the device.", "Then run “Disk Repair” again."),
        retryable=True,
        action_ids=("disk_repair",),
    ),
    ("fsck", ErrorCode.FSCK_SESSION_DROPPED): RecoveryInfo(
        "Connection lost during disk repair",
        "The connection to the device ended while fsck_hfs was repairing the disk. The repair keeps running on the device, and file sharing stays off until the device restarts.",
        (
            "Keep the Time Capsule plugged in and do not restart it; on a large disk the repair can take an hour or more.",
            "Later, run “Disk Repair” again. It will not start while the earlier repair runs, and it restarts the device when it finishes.",
        ),
        retryable=True,
        action_ids=("disk_repair",),
    ),
}


_STAGE_RECOVERY: dict[tuple[str, str, str], RecoveryInfo] = {
    # The erase request got no answer and ACPd is silent (services.wipe_disk):
    # the table is already cleared, and only unplugging restarts the device.
    ("wipe-disk", ErrorCode.ACPD_BROKEN, "erase_disk"): RecoveryInfo(
        "Apple ACPd stopped during the wipe",
        "Apple's AirPort service (ACPd) on the device stopped answering after the disk's partition table was "
        "cleared, so the device cannot restart itself. This is a bug in Apple's firmware, not TimeCapsuleSMB.",
        (
            "If the device is still formatting the disk, wait until it finishes.",
            "Unplug the device, wait 10 seconds and plug it back in.",
            "Wait 2 minutes for the device to start up, then run “Wipe Internal Disk” again.",
        ),
        retryable=True,
    ),
    ("configure", ErrorCode.REMOTE_ERROR, "acp_port_probe"): RecoveryInfo(
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
    ("configure", ErrorCode.REMOTE_ERROR, "acp_enable_ssh"): RecoveryInfo(
        "ACP SSH enablement failed",
        "Turning on SSH through AirPort ACP did not work.",
        (
            "Verify the AirPort admin password.",
            "Power-cycle the device if AirPort Utility also cannot manage it.",
        ),
        retryable=True,
        suggested_operation="configure",
        action_ids=("replace_password",),
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "read_mast"): RecoveryInfo(
        "No HFS volumes found",
        "The device did not report an HFS disk that TimeCapsuleSMB can install to.",
        (
            "Wake the disk by opening it in Finder.",
            "Check the disk is installed and formatted HFS.",
            "Run “Install / Update Samba” again.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "select_payload_home"): RecoveryInfo(
        "No writable volume for the installation files",
        "The device found HFS volumes, but none accepted the TimeCapsuleSMB installation folder.",
        (
            "Wake or remount the disk.",
            "Check available free space.",
            "Run “Install / Update Samba” again.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "verify_payload_upload"): RecoveryInfo(
        "The installation files could not be checked",
        "The copied installation files could not be checked on the HFS disk.",
        (
            "Wake the disk, then try again.",
            "If it keeps failing, run “Disk Repair”.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "verify_payload_upload_after_sync"): RecoveryInfo(
        "The installation files changed after saving",
        "The copied installation files did not match after the writes were saved to the disk.",
        (
            "Run “Install / Update Samba” again.",
            "If it keeps failing, run “Disk Repair”.",
        ),
        retryable=True,
        suggested_operation="deploy",
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "verify_runtime_reboot"): RecoveryInfo(
        "Runtime not ready",
        "The device rebooted, but the managed Samba runtime did not become healthy.",
        ("Run “Checkup” for details.", "Check boot logs from the CLI if the checkup still fails."),
        retryable=True,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "activate_runtime"): RecoveryInfo(
        "Runtime activation failed",
        "The deployed Samba runtime could not be started without rebooting.",
        ("Run “Install / Update Samba” again.", "Run “Checkup” for detailed service checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "post_reboot_activation"): RecoveryInfo(
        "Post-reboot activation failed",
        "The device rebooted, but the deployed Samba runtime could not be started after SSH returned.",
        ("Run “Install / Update Samba” again.", "Run “Checkup” for detailed service checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    ("deploy", ErrorCode.REMOTE_ERROR, "verify_runtime_activation"): RecoveryInfo(
        "Activated runtime not ready",
        "The deployed Samba runtime was started but did not become healthy.",
        ("Run “Install / Update Samba” again.", "Run “Checkup” for detailed service checks."),
        retryable=True,
        suggested_operation="deploy",
        action_ids=("run_checkup",),
    ),
    # ACPd tore itself down at the deploy's reboot request: unlike the
    # refusal before the upload, the update is on the device already.
    ("deploy", ErrorCode.ACPD_BROKEN, "wait_for_reboot_up"): RecoveryInfo(
        "Apple ACPd is broken",
        (
            "The update is installed, but Apple's AirPort service (ACPd) stopped without restarting the device. "
            "This is a bug in Apple's firmware, not TimeCapsuleSMB."
        ),
        (
            "Unplug the device, wait 10 seconds and plug it back in.",
            "Wait 2 minutes for the device to start up, then run “Checkup”.",
            "On NetBSD 4 devices, run “Activate” if the checkup says Samba is not running.",
        ),
        retryable=False,
        suggested_operation="doctor",
        action_ids=("run_checkup",),
    ),
    ("uninstall", ErrorCode.REMOTE_ERROR, "verify_post_uninstall"): RecoveryInfo(
        "Post-uninstall verification failed",
        "Managed TimeCapsuleSMB files were still present after reboot.",
        ("Run “Uninstall” again.", "Run “Checkup” if the device is reachable."),
        retryable=True,
        suggested_operation="uninstall",
        action_ids=("uninstall",),
    ),
    ("fsck", ErrorCode.VALIDATION_FAILED, "select_fsck_volume"): RecoveryInfo(
        "Volume selection failed",
        "“Disk Repair” could not choose exactly one HFS volume.",
        (
            "Select the volume to repair.",
            "Refresh the volume list, then try again.",
        ),
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
_OPERATION_CODE_RECOVERY[("discover", ErrorCode.DISCOVERY_TIMEOUT_TOO_SHORT)] = replace(
    _DEFAULTS[ErrorCode.VALIDATION_FAILED], localization_key="discover.discovery_timeout_too_short",
)
_OPERATION_CODE_RECOVERY[("discover", ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED)] = replace(
    _DEFAULTS[ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED],
    actions=_DEFAULTS[ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED].actions[:2] + ("Enable access, then retry discovery.",),
    suggested_operation="discover", localization_key="discover.local_network_permission_denied",
)
_STAGE_RECOVERY = {
    (operation, code, stage): replace(info, localization_key=f"{operation}.{code}.{stage}")
    for (operation, code, stage), info in _STAGE_RECOVERY.items()
}
# ACPd quit at the deploy's start after its restart, with the boot hook
# (verify_runtime_reboot) or by NetBSD 4's activation (services.activation.
# start_netbsd4_runtime_after_reboot): unlike the refusal before the upload,
# the update is on the device already. One card for both.
_DEPLOY_ACPD_QUIT_AFTER_UPDATE = replace(
    _DEFAULTS[ErrorCode.ACPD_QUIT],
    message=(
        "The update is installed, but Apple's AirPort service (ACPd) on the device quit, so file sharing cannot "
        "start until the device restarts. This is a bug in Apple's firmware, not TimeCapsuleSMB."
    ),
    localization_key="deploy.acpd_quit.after_update",
)
_STAGE_RECOVERY.update(dict.fromkeys(
    (("deploy", ErrorCode.ACPD_QUIT, stage) for stage in ("verify_runtime_reboot", "verify_runtime_activation")),
    _DEPLOY_ACPD_QUIT_AFTER_UPDATE,
))
# The disk failed while Wipe Internal Disk zeroed its ends (wipe_disk) or
# read them back (check_wipe): unlike a failing disk found anywhere else, its
# partition table may be gone, so a Mac may not show its volumes, and file
# sharing is off until a restart (nothing reboots a disk that stopped
# answering). One card for both.
_WIPE_DISK_FAILED_DURING_WIPE = RecoveryInfo(
    "The disk failed while it was being wiped",
    "The disk did not respond, or did not keep the zeros written to it, so it is probably failing. Its partition "
    "table may already be cleared, so its volumes may not show up anywhere. File sharing stays off until the device "
    "restarts.",
    (
        "Restart the device.",
        "If you need the files that were on it, a data recovery service may be able to get them back.",
        *_DISK_FAILING_STEPS[1:],
    ),
    retryable=False,
    localization_key="wipe-disk.disk_failing.during_wipe",
)
_STAGE_RECOVERY.update(dict.fromkeys(
    (("wipe-disk", ErrorCode.DISK_FAILING, stage) for stage in ("wipe_disk", "check_wipe")),
    _WIPE_DISK_FAILED_DURING_WIPE,
))


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


def _entry(operation: str, code: ErrorCode, stage: str | None) -> RecoveryInfo:
    if code == ErrorCode.ACP_NO_ROUTE:
        # Every ACP attempt refused at once with no route is, on this Mac, Local
        # Network access.
        code = ErrorCode.LOCAL_NETWORK_PERMISSION_DENIED
    if stage:
        policy = _STAGE_RECOVERY.get((operation, code, stage))
        if policy is not None:
            return policy
    return _OPERATION_CODE_RECOVERY.get((operation, code)) or _DEFAULTS.get(code) or _DEFAULTS[ErrorCode.OPERATION_FAILED]


def recovery_for(
    operation: str,
    code: ErrorCode,
    *,
    stage: str | None = None,
) -> dict[str, object]:
    return _entry(operation, code, stage).to_jsonable()


# How the CLI says what a step tells the app's user to do. Each phrase must
# match some step (tests/test_app_recovery.py), so a reworded app step cannot
# silently lose its CLI form.
_CLI_WORDS = (
    ("Open “System Settings” > “Privacy & Security” > “Local Network”.",
     "Open System Settings > Privacy & Security > Local Network."),
    # macOS asks Local Network access of the app a command runs in.
    ("Enable TimeCapsuleSMB in “Local Network”.", "Turn on the app this command runs in, such as Terminal."),
    ("Quit and reopen TimeCapsuleSMB, then try again.", "Then run the command again."),
    ("If TimeCapsuleSMB is already on, turn it off and on again. If that doesn't help, delete other copies of "
     "TimeCapsuleSMB and restart the Mac.", "If it is already on, turn it off and on again."),
    ("Open “SSH Access” and choose “Enable SSH”", "Run `tcapsule set-ssh --enable`"),
    ("Disable SSH again in “SSH Access”.", "Run `tcapsule set-ssh --disable` again."),
    ("Re-enter the AirPort admin password.", "Run `tcapsule configure` again and enter the AirPort admin password."),
    ("Choose “Plan Restore” to rewrite", "Run `tcapsule flash --restore` to rewrite"),
    ("Then choose “Back Up and Inspect Again”, and patch.", "Then run `tcapsule flash --patch` again."),
    ("Choose “Back Up and Inspect Again” and review each bank's status.", "Review each bank's status above."),
    ("Choose “Back Up and Inspect”.", "Run `tcapsule flash --read-only` to back up and inspect the banks again."),
    ("Refresh discovery and reconnect the saved device.", "Run `tcapsule configure` to find the device again."),
    ("Forget this device, then add your Time Capsule or AirPort Extreme.",
     "Run `tcapsule configure` and choose your Time Capsule or AirPort Extreme."),
    ("Add your Time Capsule or AirPort Extreme instead.",
     "Run `tcapsule configure` again and choose your Time Capsule or AirPort Extreme."),
    ("Check boot logs from the CLI if the checkup still fails.",
     "If the checkup still fails, check the device's boot logs over SSH."),
    ("Review the highlighted fields.", "Check the command's arguments."),
    ("Open the configuration step.", "Run `tcapsule configure`."),
    ("Confirm the operation in the app before retrying.", "Run the command again and confirm when it asks."),
    ("Select a specific HFS volume.", "Name the volume with `tcapsule fsck --volume`."),
    ("Select the volume to repair.", "Name the volume with `tcapsule fsck --volume`."),
    ("“Install / Update Samba”", "`tcapsule deploy`"),
    ("“Disk Repair”", "`tcapsule fsck`"),
    ("“Wipe Internal Disk”", "`tcapsule wipe-internal-disk`"),
    # Disk Utility's format name, not one of our controls.
    ("“Mac OS Extended (Journaled)”", "Mac OS Extended (Journaled)"),
    ("“Erase Disk”", '"Erase Disk"'),
    ("“Checkup”", "`tcapsule doctor`"),
    ("“Uninstall”", "`tcapsule uninstall`"),
    ("“Activate”", "`tcapsule activate`"),
    ("Open “Diagnostics”.", "Run `tcapsule validate-install`."),
    ("click “Copy Diagnostics” and report it at", "report it, with this command's output, at"),
    ("Click “Copy Diagnostics” and report it at", "Report it, with this command's output, at"),
    ("this Mac", "this computer"),
)


# App steps about a setting only the app has; the CLI leaves them out. Any
# other step reaches the CLI, so one naming an app control the table above does
# not cover fails tests/test_app_recovery.py instead of vanishing.
_APP_ONLY_STEPS = frozenset({
    "If “Helper path” is set in “Settings”, clear it.",
    "Refresh the volume list, then try again.",
})


def cli_steps(operation: str, code: ErrorCode, *, stage: str | None = None) -> tuple[str, ...]:
    """The next steps the CLI prints for an error: the app's, in the CLI's
    words, then the entry's CLI-only steps."""
    entry = _entry(operation, code, stage)
    steps = []
    for step in entry.actions:
        if step in _APP_ONLY_STEPS:
            continue
        for app_words, cli_words in _CLI_WORDS:
            step = step.replace(app_words, cli_words)
        steps.append(step)
    return (*steps, *entry.cli_extra)


def ssh_timeout_slow_device_recovery(*, device_name: str | None = None) -> dict[str, object]:
    recovery = _SSH_TIMEOUT_SLOW_DEVICE_RECOVERY.to_jsonable()
    recovery["message"] = ssh_timeout_slow_device_message(device_name)
    recovery["localization_values"] = {
        "device_name": (device_name or "").strip() or "device",
    }
    return recovery
