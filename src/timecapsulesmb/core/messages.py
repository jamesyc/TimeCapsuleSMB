from __future__ import annotations

from timecapsulesmb.core.summaries import Summary

NETBSD4_REBOOT_GUIDANCE = "NetBSD 4 devices cannot auto-run Samba after a reboot."
NETBSD4_REBOOT_FOLLOWUP = "Run `activate` after a reboot if the device did not auto-start Samba."
NETBSD4_ACTIVATION_COMPLETED = f"NetBSD4 activation completed. {NETBSD4_REBOOT_FOLLOWUP}"


def netbsd4_activation_summary() -> Summary:
    """The result summary of a completed NetBSD 4 activation, from deploy or activate."""
    return Summary("activation_completed_followup", NETBSD4_ACTIVATION_COMPLETED)
