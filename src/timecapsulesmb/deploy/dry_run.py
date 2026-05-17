from __future__ import annotations

from dataclasses import asdict

from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE
from timecapsulesmb.deploy.commands import remote_actions_to_jsonable, render_remote_actions
from timecapsulesmb.deploy.planner import ActivationPlan, DeploymentPlan, UninstallPlan


DEPLOY_REBOOT_STRATEGY = "ssh_shutdown_then_reboot"
UNINSTALL_REBOOT_STRATEGY = "acp_then_ssh"


def _append_reboot_request(lines: list[str], reboot_required: bool, *, strategy: str) -> None:
    if not reboot_required:
        return
    lines.append("  request: attempt device reboot")
    lines.append(f"  strategy: {strategy}")
    lines.append("  follow-up: wait for SSH down, then SSH up")


def _add_reboot_request_json(data: dict[str, object], reboot_required: bool, *, strategy: str) -> None:
    if not reboot_required:
        return
    data["reboot_request"] = {
        "mode": "device_reboot",
        "strategy": strategy,
        "follow_up": ["wait_for_ssh_down", "wait_for_ssh_up"],
    }


def format_deployment_plan(plan: DeploymentPlan) -> str:
    lines: list[str] = []
    lines.append("Dry run: deployment plan")
    lines.append("")
    lines.append("Target:")
    lines.append(f"  host: {plan.host}")
    lines.append(f"  volume root: {plan.volume_root}")
    lines.append(f"  payload dir: {plan.payload_dir}")
    lines.append("")
    lines.append("Boot options:")
    lines.append(f"  diskd.useVolume wait: {plan.apple_mount_wait_seconds}s")
    lines.append("")
    lines.append("Remote actions (pre-upload):")
    for command in render_remote_actions(plan.pre_upload_actions):
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Uploads:")
    for upload in plan.uploads:
        timeout = f", timeout {upload.timeout_seconds}s" if upload.timeout_seconds is not None else ""
        lines.append(f"  {upload.description} ({upload.source_id}, {upload.mode}{timeout}) -> {upload.destination}")
    lines.append("")
    lines.append("Remote actions (post-upload):")
    for command in render_remote_actions(plan.post_upload_actions):
        lines.append(f"  {command}")
    lines.append("")
    if plan.activation_actions:
        lines.append("Remote actions (NetBSD4 activation):")
        for command in render_remote_actions(plan.activation_actions):
            lines.append(f"  {command}")
        lines.append("")
    lines.append("Reboot:")
    lines.append(f"  {'yes' if plan.reboot_required else 'no'}")
    _append_reboot_request(lines, plan.reboot_required, strategy=DEPLOY_REBOOT_STRATEGY)
    if plan.activation_actions:
        lines.append("  Deploy will activate Samba immediately without rebooting.")
        lines.append(f"  {NETBSD4_REBOOT_GUIDANCE}")
        lines.append(f"  {NETBSD4_REBOOT_FOLLOWUP}")
    lines.append("")
    lines.append("Post-deploy checks:")
    if plan.post_deploy_checks:
        for check in plan.post_deploy_checks:
            lines.append(f"  {check.description}")
    else:
        lines.append("  none")
    return "\n".join(lines)


def deployment_plan_to_jsonable(plan: DeploymentPlan) -> dict[str, object]:
    data = asdict(plan)
    data["smbd_path"] = str(plan.smbd_path)
    data["mdns_path"] = str(plan.mdns_path)
    data["nbns_path"] = str(plan.nbns_path)
    data["pre_upload_actions"] = remote_actions_to_jsonable(plan.pre_upload_actions)
    data["post_upload_actions"] = remote_actions_to_jsonable(plan.post_upload_actions)
    data["activation_actions"] = remote_actions_to_jsonable(plan.activation_actions)
    _add_reboot_request_json(data, plan.reboot_required, strategy=DEPLOY_REBOOT_STRATEGY)
    return data


def format_activation_plan(plan: ActivationPlan, *, device_name: str = "AirPort storage device") -> str:
    lines: list[str] = []
    lines.append("Dry run: NetBSD4 activation plan")
    lines.append("")
    lines.append("Remote actions:")
    for command in render_remote_actions(plan.actions):
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Pre-activation shortcut:")
    lines.append("  skip rc.local if NetBSD4 payload is already healthy")
    lines.append("")
    lines.append("Post-activation checks:")
    for check in plan.post_activation_checks:
        lines.append(f"  {check.description}")
    lines.append("")
    lines.append(f"This will start the deployed Samba payload on the {device_name}.")
    lines.append(f"{NETBSD4_REBOOT_GUIDANCE}")
    return "\n".join(lines)


def format_uninstall_plan(plan: UninstallPlan) -> str:
    lines: list[str] = []
    lines.append("Dry run: uninstall plan")
    lines.append("")
    lines.append("Target:")
    lines.append(f"  host: {plan.host}")
    lines.append("  volume roots:")
    if plan.volume_roots:
        for volume_root in plan.volume_roots:
            lines.append(f"    {volume_root}")
    else:
        lines.append("    none")
    lines.append("  payload dirs:")
    if plan.payload_dirs:
        for payload_dir in plan.payload_dirs:
            lines.append(f"    {payload_dir}")
    else:
        lines.append("    none")
    lines.append("")
    lines.append("Remote actions:")
    for command in render_remote_actions(plan.remote_actions):
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Reboot:")
    lines.append(f"  {'yes' if plan.reboot_required else 'no'}")
    _append_reboot_request(lines, plan.reboot_required, strategy=UNINSTALL_REBOOT_STRATEGY)
    lines.append("")
    lines.append("Post-uninstall checks:")
    if plan.post_uninstall_checks:
        for check in plan.post_uninstall_checks:
            lines.append(f"  {check.description}")
    else:
        lines.append("  none")
    return "\n".join(lines)


def uninstall_plan_to_jsonable(plan: UninstallPlan) -> dict[str, object]:
    data = asdict(plan)
    data["remote_actions"] = remote_actions_to_jsonable(plan.remote_actions)
    _add_reboot_request_json(data, plan.reboot_required, strategy=UNINSTALL_REBOOT_STRATEGY)
    return data
