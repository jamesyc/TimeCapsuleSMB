from __future__ import annotations

from dataclasses import asdict

from timecapsulesmb.deploy.planner import DeploymentPlan


def format_deployment_plan(plan: DeploymentPlan) -> str:
    lines: list[str] = []
    lines.append("Dry run: deployment plan")
    lines.append("")
    lines.append("Target:")
    lines.append(f"  host: {plan.host}")
    lines.append(f"  volume root: {plan.volume_root}")
    lines.append(f"  payload dir: {plan.payload_dir}")
    lines.append("")
    lines.append("Remote directories:")
    for directory in plan.remote_directories:
        lines.append(f"  mkdir -p {directory}")
    lines.append("")
    lines.append("Uploads:")
    for upload in plan.uploads:
        lines.append(f"  {upload.source} -> {upload.destination}")
    lines.append("")
    lines.append("Generated auth:")
    for generated in plan.generated_auth_files:
        lines.append(f"  {generated.source} -> {generated.destination}")
    lines.append("")
    lines.append("Permissions:")
    for command in plan.permission_commands:
        lines.append(f"  {command}")
    lines.append("")
    lines.append("Reboot:")
    lines.append(f"  {'yes' if plan.reboot_required else 'no'}")
    lines.append("")
    lines.append("Post-deploy checks:")
    lines.append("  Bonjour _smb._tcp browse/resolve")
    lines.append("  Authenticated SMB listing")
    return "\n".join(lines)


def deployment_plan_to_jsonable(plan: DeploymentPlan) -> dict[str, object]:
    data = asdict(plan)
    data["smbd_path"] = str(plan.smbd_path)
    data["mdns_path"] = str(plan.mdns_path)
    data["post_deploy_checks"] = [
        "bonjour_browse_resolve",
        "authenticated_smb_listing",
    ]
    return data
