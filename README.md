# TimeCapsuleSMB

Run a modern Samba server on an Apple AirPort Time Capsule.

Current working design:
- static Samba 4.3.13 built from NetBSD 7 sources
- a tiny boot hook on `/mnt/Flash`
- the real runtime staged into `/mnt/Memory` at boot
- the persistent payload stored on the internal HDD
- a separate tiny mDNS advertiser for `_smb._tcp`

This repo now contains both the developer build pipeline and the user-facing deployment workflow.

## Status

Known working:
- Time Capsule discovery from macOS
- enabling SSH with AirPyrt
- deploy from checked-in repo assets
- automatic boot via `rc.local`
- Samba 4 serving the HDD-backed share after reboot
- Bonjour `_smb._tcp` advertisement via a separate helper
- macOS access through `smb://timecapsulesamba4.local/Data`

Known limitations:
- current share access is still guest-based
- real authenticated Samba users are still TODO
- this should be treated as LAN-only
- the build flow is maintainer-oriented and requires a NetBSD VM

## Architecture

The Time Capsule has three relevant storage locations:
- `/mnt/Flash`: tiny persistent flash
- `/mnt/Memory`: small RAM disk
- `/Volumes/dk2` or `/Volumes/dk3`: the large internal HFS+ disk that Apple may unmount when idle

That means the final layout cannot simply run `smbd` from the disk. The current design is:

1. Deploy the persistent payload to the internal HDD:
   - `smbd`
   - `mdns-smbd-advertiser`
   - `smb.conf.template`
2. Install a tiny boot hook on `/mnt/Flash`:
   - `rc.local`
   - `start-samba.sh`
   - `dfree.sh`
3. At boot:
   - wait for the disk device to exist
   - mount the internal disk at Apple’s normal mountpoint
   - discover the real shared data root
   - copy the runtime binaries into `/mnt/Memory`
   - render `smb.conf`
   - start `smbd`
   - start the tiny mDNS advertiser

This avoids executing the main Samba process from a disk Apple may later unmount.

## Repo Layout

- [bin/](/Users/jameschang/git/TimeCapsuleSMB/bin)
  Checked-in deployable binaries.
- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
  Static Samba 4.3.13 server binary.
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)
  Tiny static `_smb._tcp` advertiser.
- [boot/samba4/](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4)
  Boot/runtime templates copied to the Time Capsule.
- [scripts/](/Users/jameschang/git/TimeCapsuleSMB/scripts)
  User-facing workflow scripts for host setup, discovery, configuration, and deploy.
- [build/](/Users/jameschang/git/TimeCapsuleSMB/build)
  Maintainer-only NetBSD VM build scripts.
- [plan/](/Users/jameschang/git/TimeCapsuleSMB/plan)
  Investigation notes and session handoff docs.

## End-User Workflow

If you are not rebuilding Samba yourself, you only need the checked-in binaries under `bin/`.

### 1. Bootstrap the local Mac host

```bash
python3 scripts/bootstrap_host.py
```

This creates the local Python environments and installs the Python dependencies needed for:
- discovery
- AirPyrt integration
- deploy

### 2. Discover the Time Capsule and enable SSH

```bash
.venv/bin/python scripts/prep_device.py
```

This is the discovery / SSH-prep step. It is not the full deploy.

### 3. Generate local config

```bash
.venv/bin/python scripts/configure.py
```

This writes a repo-local `.env` file. The main knobs are:
- `TC_HOST`
- `TC_PASSWORD`
- `TC_NET_IFACE`
- `TC_SHARE_NAME`
- `TC_NETBIOS_NAME`
- `TC_PAYLOAD_DIR_NAME`
- `TC_MDNS_INSTANCE_NAME`
- `TC_MDNS_HOST_LABEL`

Typical defaults:
- share name: `Data`
- NetBIOS name: `TimeCapsule`
- mDNS instance: `Time Capsule Samba 4`
- mDNS hostname label: `timecapsulesamba4`

### 4. Deploy and reboot

```bash
.venv/bin/python scripts/deploy.py
```

By default, `deploy.py` copies the checked-in payload to the Time Capsule and reboots it.

Useful flags:

```bash
.venv/bin/python scripts/deploy.py --yes
.venv/bin/python scripts/deploy.py --no-reboot
.venv/bin/python scripts/deploy.py --dry-run
```

## What Gets Deployed

`deploy.py` uses only checked-in repo assets:

- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)
- [boot/samba4/rc.local](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/rc.local)
- [boot/samba4/start-samba.sh](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/start-samba.sh)
- [boot/samba4/dfree.sh](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/dfree.sh)
- [boot/samba4/smb.conf.template](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/smb.conf.template)

The persistent payload goes onto the internal HDD under:
- `/Volumes/dkX/samba4`

The boot hooks go onto flash:
- `/mnt/Flash/rc.local`
- `/mnt/Flash/start-samba.sh`
- `/mnt/Flash/dfree.sh`

The runtime executes from RAM:
- `/mnt/Memory/samba4`

## Verification

After deploy and reboot, these checks should work from the Mac:

Browse Bonjour SMB services:

```bash
dns-sd -B _smb._tcp local.
```

Resolve the advertised service:

```bash
dns-sd -L "Time Capsule Samba 4" _smb._tcp local.
```

List shares:

```bash
smbutil view //guest:@timecapsulesamba4.local
```

Expected shares:
- `IPC$`
- `Data`

You can also connect directly in Finder:

```text
smb://timecapsulesamba4.local/Data
```

## Build Workflow For Maintainers

Normal users do not need this section.

The `build/` directory is for producing:
- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

It assumes:
- a NetBSD VM
- the NetBSD cross-build environment under `/root`
- use of `su` for the root-owned toolchain/output tree

Typical maintainer flow:

```bash
cp build/.env.example build/.env
# edit build/.env for the VM/toolchain

cd build
sh download.sh
sh bootstrap.sh
sh downloadsamba4.sh
sh samba4.sh
sh mdns.sh
```

The checked-in `bin/` artifacts are the outputs users deploy.

## Security Notes

- Treat this as LAN-only.
- Do not expose this SMB service to the public internet.
- The current working configuration is still guest-based and should be considered temporary.
- Real authentication is the next major missing feature.

## What This Repo No Longer Does

The old README described a different design:
- Samba 3
- persistent flash as the main runtime
- `pf` port redirection from 445/139 to high ports
- piggybacking on Apple file sharing for SMB exposure

That is no longer the active architecture.

The current working system is:
- Samba 4.3.13
- direct listen on port 445
- boot-time RAM staging
- separate mDNS advertisement helper

## Project Notes

- This project is unofficial and unaffiliated with Apple or the Samba team.
- The code and docs reflect a reverse-engineered workflow for a very specific class of old hardware.
- See the files under [plan/](/Users/jameschang/git/TimeCapsuleSMB/plan) for investigation history and current handoff notes.
