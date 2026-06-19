from __future__ import annotations

import argparse
from typing import Optional

from timecapsulesmb.cli.context import CommandContext
from timecapsulesmb.cli.runtime import (
    add_config_argument,
    add_no_input_argument,
    no_input_enabled,
    print_json,
)
from timecapsulesmb.core.config import (
    airport_family_display_name_from_identity,
)
from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.identity import ensure_install_id
from timecapsulesmb.device.errors import DeviceError
from timecapsulesmb.telemetry import TelemetryClient
from timecapsulesmb.cli.util import color_green
from timecapsulesmb.services.deploy import (
    DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
    DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE,
    DeployArtifactValidationError,
    DeployCompletionMessages,
    DeployOptions,
    DeployRuntimeConfig,
    complete_deployment_after_upload,
    deploy_upload_stage,
    deployment_plan_to_jsonable,
    format_deployment_plan,
    payload_family_description,
    pre_upload_action_message,
    prepare_deploy_preflight,
    prepare_deployment_plan,
    uploaded_file_message,
    upload_and_verify_deployment_payload,
)
from timecapsulesmb.services.reboot import RebootFlowError
from timecapsulesmb.services.runtime import load_env_config


def _target_family_display_name(target) -> str:
    probe = target.probe_state.probe_result if target.probe_state is not None else None
    return airport_family_display_name_from_identity(
        model=None if probe is None else probe.airport_model,
        syap=None if probe is None else probe.airport_syap,
    )


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be an integer") from e
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the checked-in Samba 4 payload to an AirPort storage device.")
    add_config_argument(parser)
    parser.add_argument("--no-reboot", action="store_true", help="Do not reboot; activate the deployed runtime in place")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Request reboot and return without waiting for SSH or runtime verification",
    )
    parser.add_argument("--yes", action="store_true", help="Do not prompt before reboot")
    add_no_input_argument(parser)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making changes")
    parser.add_argument("--json", action="store_true", help="Output the dry-run deployment plan as JSON")
    parser.add_argument("--allow-unsupported", action="store_true", help="Proceed even if the detected device is not currently supported")
    parser.add_argument("--no-nbns", action="store_true", help="Disable the bundled NBNS responder on the next boot")
    mdns_afp_group = parser.add_mutually_exclusive_group()
    mdns_afp_group.add_argument("--mdns-advertise-afp", action="store_true", help=argparse.SUPPRESS)
    mdns_afp_group.add_argument("--no-mdns-advertise-afp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--mount-wait",
        type=_non_negative_int,
        default=DEFAULT_APPLE_MOUNT_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"Seconds for each deployment-time diskd.useVolume mount guard attempt to wait (default: {DEFAULT_APPLE_MOUNT_WAIT_SECONDS})",
    )
    parser.add_argument("--debug-logging", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.json and not args.dry_run:
        parser.error("--json currently requires --dry-run")

    nbns_enabled = not args.no_nbns
    mdns_advertise_afp = (
        True if args.mdns_advertise_afp
        else False if args.no_mdns_advertise_afp
        else None
    )
    deploy_options = DeployOptions(
        dry_run=args.dry_run,
        no_reboot=args.no_reboot,
        no_wait=args.no_wait,
        mount_wait_seconds=args.mount_wait,
        allow_unsupported=args.allow_unsupported,
    )
    no_wait = deploy_options.effective_no_wait
    ensure_install_id()
    app_paths = resolve_app_paths(config_path=args.config)
    config = load_env_config(env_path=args.config)
    telemetry = TelemetryClient.from_config(config, nbns_enabled=nbns_enabled)
    with CommandContext(telemetry, "deploy", "deploy_started", "deploy_finished", config=config, args=args) as command_context:
        command_context.update_fields(
            nbns_enabled=nbns_enabled,
            reboot_was_attempted=False,
            device_came_back_after_reboot=False,
        )
        if no_input_enabled(args) and not args.yes and not args.no_reboot and not args.dry_run:
            command_context.set_stage("noninteractive_confirmation")
            message = (
                "Running `deploy` with reboot in non-interactive mode requires `--yes` "
                "to approve the reboot or `--no-reboot` to avoid it."
            )
            print(message)
            command_context.fail_with_error(message)
            return 1
        command_context.set_stage("resolve_managed_target")
        if not args.json:
            print("Resolving deployment target...", flush=True)
        target = command_context.resolve_validated_managed_target(profile="deploy", include_probe=True)
        connection = target.connection

        if not args.json:
            print("Validating local artifacts...", flush=True)
        if not args.json:
            print("Checking device compatibility...", flush=True)
        try:
            preflight = prepare_deploy_preflight(
                connection,
                target,
                app_paths.distribution_root,
                deploy_options,
                callbacks=command_context.to_operation_callbacks(),
            )
        except DeployArtifactValidationError as exc:
            raise SystemExit(str(exc)) from exc
        except DeviceError as exc:
            raise SystemExit(str(exc)) from exc
        payload_context = preflight.payload_context
        payload_family = preflight.payload_family
        startup_mode = preflight.startup_mode
        if not args.json:
            print(f"Using {payload_family_description(payload_family)} payload.", flush=True)
        if not args.dry_run and not args.json:
            print("Finding payload volume...", flush=True)
        try:
            prepared_plan = prepare_deployment_plan(
                connection,
                app_paths.distribution_root,
                payload_context,
                dry_run=args.dry_run,
                payload_dir_name=deploy_options.payload_dir_name,
                mount_wait_seconds=deploy_options.mount_wait_seconds,
                callbacks=command_context.to_operation_callbacks(),
                artifacts=preflight.artifacts,
                wait_after_reboot=not no_wait,
            )
        except DeviceError as exc:
            raise SystemExit(str(exc)) from exc
        payload_home = prepared_plan.payload_home
        plan = prepared_plan.plan
        if not args.dry_run and not args.json:
            print(f"Using payload directory {payload_home.payload_dir}.", flush=True)

        if args.dry_run:
            if args.json:
                print_json(deployment_plan_to_jsonable(plan))
            else:
                print(format_deployment_plan(plan))
            command_context.succeed()
            return 0

        print("Deleting old deployed files...", flush=True)
        print("Stopping existing runtime...", flush=True)

        def report_pre_upload_action(action, _index: int, _total: int) -> None:
            message = pre_upload_action_message(action)
            if message is not None:
                print(message, flush=True)

        def report_uploaded_file(transfer) -> None:
            message = uploaded_file_message(transfer)
            if message is not None:
                print(message, flush=True)

        current_upload_stage: str | None = None

        def stage_upload(transfer) -> None:
            nonlocal current_upload_stage
            stage = deploy_upload_stage(transfer)
            if stage == current_upload_stage:
                return
            current_upload_stage = stage
            command_context.set_stage(stage)

        try:
            upload_and_verify_deployment_payload(
                config,
                connection=connection,
                prepared_plan=prepared_plan,
                runtime_config=DeployRuntimeConfig(
                    nbns_enabled=nbns_enabled,
                    debug_logging=args.debug_logging,
                    mdns_advertise_afp=mdns_advertise_afp,
                ),
                callbacks=command_context.to_operation_callbacks(),
                on_pre_upload_action_done=report_pre_upload_action,
                on_before_upload=lambda: print("Uploading deployment payload...", flush=True),
                on_after_upload=lambda: print("Upload phase complete.", flush=True),
                on_uploading=stage_upload,
                on_uploaded=report_uploaded_file,
                on_before_post_upload_actions=lambda: print("Applying file permissions...", flush=True),
                on_before_verify=lambda post_sync: None if post_sync else print("Verifying uploaded payload...", flush=True),
                on_before_flush=lambda: print("Flushing payload to disk...", flush=True),
            )
        except DeviceError as exc:
            raise SystemExit(str(exc)) from exc

        print("Verified uploaded payload.", flush=True)
        print(f"Deployed Samba payload to {plan.payload_dir}", flush=True)
        print("Updated /mnt/Flash boot files.", flush=True)

        if plan.reboot_required and not args.yes:
            device_name = _target_family_display_name(target)
            if startup_mode == DEPLOY_STARTUP_REBOOT_THEN_ACTIVATE:
                if no_wait:
                    prompt = (
                        f"This will request a reboot of the {device_name} and return without "
                        "post-reboot Samba activation or verification. Continue?"
                    )
                else:
                    prompt = f"This will reboot the {device_name}, then activate Samba after SSH returns. Continue?"
            elif no_wait:
                prompt = f"This will request a reboot of the {device_name} and return without waiting for verification. Continue?"
            else:
                prompt = f"This will reboot the {device_name} now. Continue?"
            proceed = command_context.confirm_or_fail(
                prompt,
                default=True,
                noninteractive_message="Running `deploy` with reboot requires confirmation when stdin is not interactive. Use `deploy --yes` to skip the prompt or `deploy --no-reboot`.",
                allow_prompt=not no_input_enabled(args),
            )
            if proceed is None:
                return 1
            if not proceed:
                print("Deployment complete without reboot.", flush=True)
                command_context.cancel_with_error("Cancelled by user at reboot confirmation prompt.")
                return 0

        try:
            completion = complete_deployment_after_upload(
                connection,
                prepared_plan,
                no_wait=no_wait,
                callbacks=command_context.to_operation_callbacks(),
                messages=DeployCompletionMessages(
                    netbsd4_heading="Waiting for NetBSD 4 device activation, this can take a few minutes for Samba to start up...",
                    reboot_request_message="Requesting reboot...",
                    reboot_runtime_wait_message="Waiting for managed runtime to finish starting...",
                    reboot_heading="Wait for device to finish loading; it can take a few minutes for Samba to start up...",
                ),
            )
        except RebootFlowError as exc:
            print(str(exc))
            command_context.fail_with_error(str(exc))
            return 1
        except DeviceError as exc:
            print(str(exc))
            command_context.fail_with_error(str(exc))
            return 1

        if completion.message:
            print(completion.message)
        if completion.reboot_requested and not completion.waited:
            print("Reboot requested; not waiting for the device to go down or come back.")
            print("Post-reboot runtime verification skipped.")
        print(color_green("Deploy Finished."))
        command_context.succeed()
        return 0
    return 1
