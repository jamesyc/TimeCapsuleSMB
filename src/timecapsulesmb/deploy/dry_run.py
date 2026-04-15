from __future__ import annotations

from dataclasses import asdict

from timecapsulesmb.deploy.commands import render_remote_actions
from timecapsulesmb.deploy.planner import DeploymentPlan, UninstallPlan


def format_deployment_plan(plan: DeploymentPlan) -> str:
    lines: list[str] = []
    lines.append("Dry run: deployment plan")
    lines.append("")
    lines.append("Target:")
    lines.append(f"  host: {plan.host}")
    lines.append(f"  volume root: {plan.volume_root}")
    lines.append(f"  payload dir: {plan.payload_dir}")
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
        lines.append("  NetBSD4 activation is immediate.")
        lines.append("  Tested gen1 devices need activate after reboot; other generations may auto-start rc.local.")
    lines.append("")
    lines.append("Post-deploy checks:")
    if plan.activation_actions:
        lines.append("  fstat shows smbd bound to TCP 445")
        lines.append("  fstat shows mdns-advertiser bound to UDP 5353")
    else:
        lines.append("  Bonjour _smb._tcp browse/resolve")
        lines.append("  Authenticated SMB listing")
    return "\n".join(lines)


def deployment_plan_to_jsonable(plan: DeploymentPlan) -> dict[str, object]:
    data = asdict(plan)
    data["smbd_path"] = str(plan.smbd_path)
    data["mdns_path"] = str(plan.mdns_path)
    data["nbns_path"] = str(plan.nbns_path)
    if plan.activation_actions:
        data["post_deploy_checks"] = [
            "netbsd4_smbd_bound_445",
            "netbsd4_mdns_bound_5353",
        ]
    else:
        data["post_deploy_checks"] = [
            "bonjour_browse_resolve",
            "authenticated_smb_listing",
        ]
    return data


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
    lines.append("  yes")
    lines.append("")
    lines.append("Post-uninstall checks:")
    lines.append("  SSH returns after reboot")
    lines.append("  Managed payload and flash hooks are absent")
    return "\n".join(lines)


def uninstall_plan_to_jsonable(plan: UninstallPlan) -> dict[str, object]:
    data = asdict(plan)
    data["post_uninstall_checks"] = [
        "ssh_returns_after_reboot",
        "managed_files_absent",
    ]
    return data
