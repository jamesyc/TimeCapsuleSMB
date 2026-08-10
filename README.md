# TimeCapsuleSMB

[![CI](https://github.com/jamesyc/TimeCapsuleSMB/actions/workflows/ci.yml/badge.svg)](https://github.com/jamesyc/TimeCapsuleSMB/actions/workflows/ci.yml)
[![Latest Release](https://img.shields.io/github/v/release/jamesyc/TimeCapsuleSMB)](https://github.com/jamesyc/TimeCapsuleSMB/releases/latest)
[![License](https://img.shields.io/github/license/jamesyc/TimeCapsuleSMB)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)

Apple AirPort Time Capsules only speak AFP and SMB1 natively. macOS removed SMB1 support long ago (and AFP in macOS 27), so modern macOS can no longer back up to a stock Time Capsule. TimeCapsuleSMB runs a modern Samba 4 server **on the Time Capsule itself**: the device advertises itself over Bonjour (it appears automatically in Finder → Network), accepts authenticated SMB3 connections, and works as a Time Machine destination — even if its IP address changes.

This project has two parts:

- a fork of Samba 4, prebuilt as static binaries for the device (in `bin/`)
- the installers for those binaries: a Python CLI (`tcapsule`) and a macOS GUI app

## Requirements

- A Mac (macOS 14+) or Linux machine on the same local network as the Time Capsule
- The Time Capsule password
- Python 3.9+; `smbclient` for `doctor` (and `sshpass` for NetBSD 4 devices) — installed automatically when possible

AirPort Extreme devices are not officially supported (no hard drive, so not enough space for the binaries).

## Quick Start (macOS app)

1. Download the latest release, unzip, and open it (disable Gatekeeper if prompted).
2. Grant Local Network permission (System Settings → Privacy & Security → Local Network), then quit and reopen the app.
3. Add Device, select your Time Capsule, enter its password, and Save Device.
4. Click Install/Update on the device's Install tab. (If deploy fails, remove the saved device and add it again — it sometimes takes more than one deploy.)
5. Gen 1–4 devices only: on the Maintenance page, install the "Persistent NetBSD4 Boot Hook" (Back Up and Inspect → Plan Patch → Write Patch) so Samba auto-starts after reboots.
6. Optional: wait 5–10 minutes for Samba to start, then run a Checkup.

## Quick Start (Python CLI)

```bash
./tcapsule bootstrap               # the only repo-root command; builds .venv
.venv/bin/tcapsule configure       # discovers the device, enables SSH if needed, writes .env
.venv/bin/tcapsule deploy          # installs Samba on the device
.venv/bin/tcapsule doctor          # verifies everything works
```

### Commands

| Command | Purpose |
|---|---|
| `configure` | Discover the device (mDNS/Bonjour), enable SSH via ACP if closed, write `.env` |
| `deploy` | Install/update Samba; reboots by default (`--yes`, `--no-reboot`, `--dry-run --json`) |
| `activate` | Start the deployed runtime without re-copying (needed after every reboot on NetBSD 4) |
| `flash` | Back up flash memory; `--patch` installs the persistent boot hook (unplug to reboot after success); `--restore` restores a bank from Apple stock firmware; `--check-apple` verifies banks |
| `fsck` | Repair the internal disk before deploy |
| `doctor` | Non-destructive diagnostics (`--json`) |
| `discover` | List mDNS/Bonjour devices (`--json`) |
| `repair-xattrs` | Repair broken on-disk xattrs |
| `uninstall` | Remove Samba and boot files (`--yes`, `--no-reboot`, `--dry-run --json`) |
| `validate-install`, `paths`, `set-ssh` | Local install checks and helpers |

The same workflow is available as numbered Makefile stages — `make install` → `discover`/`validate-install` → `configure` → `deploy`/`deploy-dry-run` → `activate`/`flash-backup`/`flash-patch`/`flash-restore` → `doctor` → `fsck`/`repair-xattrs` → `uninstall`/`uninstall-dry-run` → `set-ssh` (see `make help`; `yes=1`/`json=1`/`dry=1`/`no-reboot=1`/`reboot=1` pass the corresponding flags).

## How it works

The Time Capsule hardware is old and constrained, with three storage areas:

- `/mnt/Flash` — persistent, ~900 KB free: only a small boot loader lives here
- `/mnt/Memory` — 16 MB ramdisk: the Samba runtime runs from here
- Internal HDD — large, but Apple unmounts it when idle (you cannot run binaries from it)

At boot, the loader waits for the internal disk, copies the payload into `/mnt/Memory`, and starts `smbd` from RAM; tiny mDNS/NBNS helper binaries advertise the share over Bonjour.

- **Auth**: any username with the Time Capsule's device password. At boot the device reads its live AirPort `syPW` value, generates the Samba password file in RAM (so password changes are picked up after reboot), and starts with guest access disabled.
- **Boot persistence**: NetBSD 6 devices auto-start on boot. NetBSD 4 devices need `activate` after every reboot, or `flash --patch` to install a persistent boot hook.
- **Uninstall** removes the managed files; Apple wipes everything except `/mnt/Flash` after reboot, so deleting the 7 loader files plus the `.samba4` folder restores a factory-clean device.

## Security

LAN-only setup — do not expose this SMB service to the public internet or forward ports to it. SMB access maps to `root` internally (a deliberate compatibility choice for this old firmware). Telemetry and logging are enabled by default.

## Troubleshooting

- **Device not in Finder**: run `tcapsule doctor`, then `dns-sd -B _smb._tcp local.` — the service can be up and correct even when Finder browsing is slow.
- **Finder still can't connect**: reboot, then try `smb://<advertised-host>.local/<share>` directly, or use the IP from your `.env`.
- **Deploy says SMB listing failed right after reboot**: these old CPUs are slow — wait a little, then run `tcapsule doctor`.
- Anything else: see [FAQ.md](FAQ.md), or file an issue.

## For developers

- Full technical story, engineering constraints, and historical dead ends: [DETAIL.md](DETAIL.md)
- Contributing, testing, and device constraints: [CONTRIBUTING.md](CONTRIBUTING.md)
- Rebuilding the NetBSD binaries: `build/` (requires a NetBSD VM; the checked-in `bin/` files are ready to use)
- Release process: [RELEASE.md](RELEASE.md)
