from __future__ import annotations

import ipaddress
import shlex

from timecapsulesmb.checks.bonjour import run_bonjour_checks
from timecapsulesmb.checks.smb import try_authenticated_smb_listing
from timecapsulesmb.deploy.planner import UninstallPlan
from timecapsulesmb.transport.local import command_exists
from timecapsulesmb.transport.ssh import run_ssh


def _configured_smb_server(host_label: str) -> str:
    value = host_label.strip()
    if not value:
        return value
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if "." in value:
        return value
    return f"{value}.local"


def wait_for_post_reboot_smbd(host: str, password: str, ssh_opts: str, *, timeout_seconds: int = 120) -> bool:
    script = rf'''
attempt=0
while [ "$attempt" -lt {timeout_seconds} ]; do
    smbd_ready=0

    if /usr/bin/pkill -0 smbd >/dev/null 2>&1; then
        if [ -f /mnt/Memory/samba4/etc/smb.conf ] && [ -f /mnt/Memory/samba4/var/log.smbd ]; then
            smbd_log="$(cat /mnt/Memory/samba4/var/log.smbd 2>/dev/null || true)"
            case "$smbd_log" in
                *daemon_ready*) smbd_ready=1 ;;
            esac
        fi
    fi

    if [ "$smbd_ready" -eq 1 ]; then
        exit 0
    fi

    attempt=$((attempt + 1))
    sleep 1
done
exit 1
'''
    proc = run_ssh(
        host,
        password,
        ssh_opts,
        f"/bin/sh -c {shlex.quote(script)}",
        check=False,
        timeout=timeout_seconds + 30,
    )
    return proc.returncode == 0


def verify_post_deploy(values: dict[str, str]) -> None:
    samba_user = values["TC_SAMBA_USER"]
    password = values["TC_PASSWORD"]
    host_label = values["TC_MDNS_HOST_LABEL"]

    print("Post-deploy verification:")

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

    if command_exists("smbclient"):
        servers = [_configured_smb_server(host_label)]
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
        print("  SMB listing verification skipped: smbclient not found")


def verify_netbsd4_activation(host: str, password: str, ssh_opts: str) -> bool:
    print("NetBSD4 activation verification:")
    script = r'''
if ! command -v fstat >/dev/null 2>&1; then
    echo "FAIL:fstat missing"
    exit 1
fi
attempt=0
while [ "$attempt" -lt 30 ]; do
    out="$(fstat 2>&1)"
    runtime_conf=0
    if [ -f /mnt/Memory/samba4/etc/smb.conf ]; then
        runtime_conf=1
    fi
    runtime_log=0
    if [ -f /mnt/Memory/samba4/var/log.smbd ]; then
        smbd_log="$(cat /mnt/Memory/samba4/var/log.smbd 2>/dev/null || true)"
        case "$smbd_log" in
            *daemon_ready*) runtime_log=1 ;;
        esac
    fi
    case "$out" in
        *smbd*":445"*mdns-advertiser*":5353"*|*mdns-advertiser*":5353"*smbd*":445"*)
            if [ "$runtime_conf" -eq 1 ] && [ "$runtime_log" -eq 1 ]; then
                break
            fi
            ;;
    esac
    attempt=$((attempt + 1))
    sleep 1
done
echo "$out" | sed -n '/\.445/p;/\.5353/p'
status=0
if [ -f /mnt/Memory/samba4/etc/smb.conf ]; then
    echo "PASS:managed runtime smb.conf present"
else
    echo "FAIL:managed runtime smb.conf missing"
    status=1
fi
if [ -f /mnt/Memory/samba4/var/log.smbd ]; then
    smbd_log="$(cat /mnt/Memory/samba4/var/log.smbd 2>/dev/null || true)"
else
    smbd_log=""
fi
case "$smbd_log" in
    *daemon_ready*) echo "PASS:managed smbd reported daemon_ready" ;;
    *) echo "FAIL:managed smbd did not report daemon_ready"; status=1 ;;
esac
case "$out" in
    *smbd*":445"*) echo "PASS:smbd bound to TCP 445" ;;
    *) echo "FAIL:smbd is not bound to TCP 445"; status=1 ;;
esac
case "$out" in
    *mdns-advertiser*":5353"*) echo "PASS:mdns-advertiser bound to UDP 5353" ;;
    *) echo "FAIL:mdns-advertiser is not bound to UDP 5353"; status=1 ;;
esac
exit "$status"
'''
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}", check=False)
    for line in proc.stdout.strip().splitlines():
        if line.startswith("PASS:"):
            print(f"  ok: {line.removeprefix('PASS:')}")
        elif line.startswith("FAIL:"):
            print(f"  failed: {line.removeprefix('FAIL:')}")
        elif line:
            print(f"  {line}")
    return proc.returncode == 0


def netbsd4_activation_is_already_healthy(host: str, password: str, ssh_opts: str) -> bool:
    script = r'''
if ! command -v fstat >/dev/null 2>&1; then
    exit 1
fi
out="$(fstat 2>&1)"
if [ ! -f /mnt/Memory/samba4/etc/smb.conf ]; then
    exit 1
fi
if [ -f /mnt/Memory/samba4/var/log.smbd ]; then
    smbd_log="$(cat /mnt/Memory/samba4/var/log.smbd 2>/dev/null || true)"
else
    smbd_log=""
fi
case "$smbd_log" in
    *daemon_ready*) : ;;
    *) exit 1 ;;
esac
case "$out" in
    *smbd*":445"*mdns-advertiser*":5353"*|*mdns-advertiser*":5353"*smbd*":445"*)
        exit 0
        ;;
    *)
        exit 1
        ;;
esac
'''
    proc = run_ssh(host, password, ssh_opts, f"/bin/sh -c {shlex.quote(script)}", check=False)
    return proc.returncode == 0


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
