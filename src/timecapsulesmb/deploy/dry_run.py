from __future__ import annotations

from dataclasses import asdict

from timecapsulesmb.cli.util import NETBSD4_REBOOT_FOLLOWUP, NETBSD4_REBOOT_GUIDANCE
from timecapsulesmb.deploy.commands import render_remote_actions
from timecapsulesmb.deploy.planner import ActivationPlan, DeploymentPlan, UninstallPlan


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
    lines.append(f"  Apple mount wait: {plan.apple_mount_wait_seconds}s")
    lines.append("")
    lines.append("Remote actions (pre-upload):")
    for command in render_remote_actions(plan.pre_upload_actions):
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Uploads:")
    for upload in plan.uploads:
        lines.append(f"  {upload.source} -> {upload.destination}")
    lines.append("")
    lines.append("Generated auth:")
    for generated in plan.generated_auth_files:
        lines.append(f"  {generated.source} -> {generated.destination}")
    lines.append("")
    lines.append("Remote actions (post-auth):")
    for command in render_remote_actions(plan.post_auth_actions):
        lines.append(f"  {command}")
    lines.append("")
    if plan.activation_actions:
        lines.append("Remote actions (NetBSD4 activation):")
        for command in render_remote_actions(plan.activation_actions):
            lines.append(f"  {command}")
        lines.append("")
    lines.append("Reboot:")
    lines.append(f"  {'yes' if plan.reboot_required else 'no'}")
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
    return data


def format_activation_plan(plan: ActivationPlan) -> str:
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
    lines.append("This will start the deployed Samba payload on the Time Capsule.")
    lines.append(f"{NETBSD4_REBOOT_GUIDANCE}")
    return "\n".join(lines)


def format_uninstall_plan(plan: UninstallPlan) -> str:
    lines: list[str] = []
    lines.append("Dry run: uninstall plan")
    lines.append("")
    lines.append("Target:")
    lines.append(f"  host: {plan.host}")
    lines.append(f"  volume root: {plan.volume_root}")
    lines.append(f"  payload dir: {plan.payload_dir}")
    lines.append("")
    lines.append("Remote actions:")
    for command in render_remote_actions(plan.remote_actions):
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Reboot:")
    lines.append(f"  {'yes' if plan.reboot_required else 'no'}")
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
    return data
