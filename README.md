# TimeCapsuleSMB

Run a modern Samba server on an Apple Time Capsule while keeping the disk auto-mount behavior of Apple’s firmware.

This repo documents the approach and will grow into a set of scripts to automate discovery, SSH enablement, deployment, and configuration. For now, follow the manual procedure below.

## What This Does
- Uses mDNS to discover Time Capsules on your network.
- Enables root SSH access using AirPyrt (temporary) to allow configuration.
- Cross-compiles a modern Samba build for the Time Capsule’s NetBSD-derived environment (evbarm target).
- Installs Samba onto the device’s persistent flash, configures shares from the mounted disk, and redirects ports so clients can connect normally.

## Status
- Planning and manual steps are documented.
- No automation code is checked in yet; the goal is to provide a Python CLI that performs discovery, SSH enablement, upload, and configuration.

## Prerequisites
- macOS host (Apple Silicon M1/M2 tested) with Python 3.10+ and `virtualenv`.
- Access to your Time Capsule from the same network.
- AirPyrt tool to enable SSH on the Time Capsule (consult AirPyrt’s README for install/usage).
- Ability to build/cross-compile for NetBSD `evbarm` (toolchain of your choice).

## Device Notes
- Disk mount path: `/Volumes/dk2/ShareRoot` (where your shared data lives).
- Persistent flash: `/mnt/Flash` (useful for configs and binaries that must survive reboots).
- OpenSSH quirk: legacy DSA host keys require `-oHostKeyAlgorithms=+ssh-dss` when connecting via SSH.

## How It Works (Design)
1. Discover Time Capsules via mDNS; prefer the `.local` hostname over raw IP (e.g., `Basement-AirPort-Time-Capsule.local`).
2. Enable SSH to `root` using AirPyrt so we can provision the box.
3. Copy a modern Samba build and config onto the device’s persistent flash.
4. Keep Apple File Sharing enabled so the disk auto-mounts, but redirect ports so your Samba answers SMB connections.
5. Run Samba bound to high ports (e.g., 1445 and 1139), with packet filter rules redirecting 445→1445 and 139→1139.

## Manual Setup (until automated)

1) Discover the Time Capsule
- On macOS, you can enumerate services via `dns-sd` or simply identify the device in AirPort Utility. Prefer the mDNS hostname: `X-AirPort-Time-Capsule.local`.

2) Enable SSH using AirPyrt
- Create and activate a virtualenv on your Mac:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  # install and use AirPyrt per its documentation
  ```
- Use AirPyrt to enable root SSH on the Time Capsule. Refer to the tool’s README for the specific command.

3) SSH into the Time Capsule
- Use the legacy host key option if needed:
  ```bash
  ssh -oHostKeyAlgorithms=+ssh-dss root@Basement-AirPort-Time-Capsule.local
  ```

4) Prepare persistent locations on the device
- Suggested layout on the Time Capsule (created as `root`):
  ```bash
  mkdir -p /mnt/Flash/samba/bin /mnt/Flash/samba/etc /mnt/Flash/samba/var
  ```
- Data lives on the disk at `/Volumes/dk2/ShareRoot`.

5) Build Samba for NetBSD evbarm (on your Mac)
- Use your preferred cross toolchain targeting NetBSD `evbarm`.
- Build Samba with the features you need (consider `vfs_fruit` if you want Time Machine compatibility; review Samba security advisories before enabling).
- Produce a minimal runtime set (e.g., `smbd`, `nmbd`, `winbindd`, required libs/modules). Static linking can simplify deployment.

6) Copy binaries and config
- From your Mac:
  ```bash
  scp -oHostKeyAlgorithms=+ssh-dss -r path/to/samba/bin root@Basement-AirPort-Time-Capsule.local:/mnt/Flash/samba/
  scp -oHostKeyAlgorithms=+ssh-dss smb.conf root@Basement-AirPort-Time-Capsule.local:/mnt/Flash/samba/etc/
  ```
- Minimal `smb.conf` considerations:
  - `pid directory = /mnt/Flash/samba/var`
  - `log file = /mnt/Flash/samba/var/log.smbd`
  - `smb ports = 1445 1139`
  - `fruit:time machine = yes` only if you intend to serve Time Machine (and understand the security trade-offs).
  - Define shares under `/Volumes/dk2/ShareRoot`.

7) Keep the disk mounted and redirect ports
- Leave File Sharing enabled in AirPort Utility so the Time Capsule’s disk auto-mounts on boot.
- Use `pf` to redirect low SMB ports to your high ports where Samba runs. Example rules (interface may differ):
  ```pf
  rdr pass on lo0 proto tcp from any to any port 445 -> 127.0.0.1 port 1445
  rdr pass on lo0 proto tcp from any to any port 139 -> 127.0.0.1 port 1139
  ```
- Load and enable rules with `pfctl` (paths/commands may vary on the firmware):
  ```bash
  pfctl -f /etc/pf.conf
  pfctl -e
  ```

8) Run Samba on the device
- Launch `smbd` and (optionally) `nmbd`/`winbindd` pointing to your config:
  ```bash
  /mnt/Flash/samba/bin/smbd -s /mnt/Flash/samba/etc/smb.conf
  ```
- Confirm it is listening on 1445/1139, and verify `pf` redirects are active.

9) Test from a client
- From macOS:
  - In Finder: Go → Connect to Server → `smb://Basement-AirPort-Time-Capsule.local/YourShare`
  - Or via terminal with `smbutil`/`mount_smbfs`.

## Security Notes
- Be cautious enabling `vfs_fruit` and Time Machine support; follow Samba advisories and keep your build updated.
- Restrict access to trusted subnets/users; avoid exposing SMB to the internet.
- Consider rotating credentials and disabling SSH when not actively administering.

## Troubleshooting
- Disk not mounted: ensure Apple File Sharing remains enabled in AirPort Utility so `/Volumes/dk2/ShareRoot` is present.
- SSH fails with key algo error: add `-oHostKeyAlgorithms=+ssh-dss`.
- No SMB connectivity: verify `pf` rules loaded and Samba is bound to 1445/1139.
- Persistence: place binaries/config in `/mnt/Flash` and ensure your startup hooks re-apply `pf` rules and launch Samba on boot (details depend on firmware hooks available).

## Roadmap
- Python CLI to:
  - Discover Time Capsules via mDNS and prompt for selection.
  - Enable SSH via AirPyrt automatically inside a virtualenv.
  - Upload Samba artifacts and configs.
  - Configure `pf` redirection and launch services; add boot persistence.

---

This project is unaffiliated with Apple or the Samba team. For educational use only; proceed at your own risk.
