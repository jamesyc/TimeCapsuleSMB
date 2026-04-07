from __future__ import annotations

import shlex

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.smb import try_authenticated_smb_listing
from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import run_ssh


def verify_post_deploy(values: dict[str, str]) -> None:
    samba_user = values["TC_SAMBA_USER"]
    password = values["TC_PASSWORD"]
    host_label = values["TC_MDNS_HOST_LABEL"]

    print("Post-deploy verification:")

    if command_exists("dns-sd"):
        try:
            _, discovered_instance, target = run_bonjour_checks(values["TC_MDNS_INSTANCE_NAME"])
            if discovered_instance:
                print(f"  Advertised service name: {discovered_instance}")
            else:
                print("  Advertised service name: not found")
            if target:
                print(f"  Advertised hostname: {target}")
            else:
                print("  Advertised hostname: not resolved")
        except Exception as e:
            print(f"  Bonjour verification failed: {e}")
    else:
        print("  Bonjour verification skipped: dns-sd not found")

    if command_exists("smbutil"):
        servers = [f"{host_label}.local"]
        tc_host = values.get("TC_HOST", "")
        if "@" in tc_host:
            tc_host = tc_host.split("@", 1)[1]
        if tc_host and tc_host not in servers:
            servers.append(tc_host)
        result = try_authenticated_smb_listing(samba_user, password, servers)
        if result.status == "PASS":
            server = result.message.removeprefix("authenticated SMB listing works for ")
            print(f"  Authenticated SMB listing: ok ({server})")
        else:
            failure = result.message.removeprefix("authenticated SMB listing failed: ")
            print(f"  Authenticated SMB listing: failed ({failure})")
    else:
        print("  SMB listing verification skipped: smbutil not found")


def verify_post_uninstall(host: str, password: str, ssh_opts: str, plan: UninstallPlan) -> bool:
    print("Post-uninstall verification:")
    script_lines = [
        "missing=0",
    ]
    for target in plan.verify_absent_targets:
        quoted = shlex.quote(target)
        script_lines.append(f"if [ -e {quoted} ]; then echo PRESENT:{target}; missing=1; else echo ABSENT:{target}; fi")
    script_lines.append("exit \"$missing\"")
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote('; '.join(script_lines))}", check=False)

    ok = proc.returncode == 0
    for line in proc.stdout.strip().splitlines():
        if line.startswith("ABSENT:"):
            print(f"  ok: removed {line.removeprefix('ABSENT:')}")
        elif line.startswith("PRESENT:"):
            print(f"  failed: still present {line.removeprefix('PRESENT:')}")
    return ok
