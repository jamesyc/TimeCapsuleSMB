# TimeCapsuleSMB Detail Reference

This file is the long-form engineering reference for the current system. Most of this is AI generated summaries of my notes. 

It is intentionally denser than [README.md](/Users/jameschang/git/TimeCapsuleSMB/README.md). The README is the user-facing overview. This file is for maintainers who want the actual constraints, rationale, and important implementation details in one place.

## Current Working State

The current system works end to end on the target Apple AirPort Time Capsule.

What is working now:
- static Samba 4.3.13 built from NetBSD 7 sources
- static tiny `_smb._tcp` advertiser
- boot-time runtime staging via `/mnt/Flash/rc.local`
- direct SMB service on port `445`
- Bonjour advertisement under a separate service name
- authenticated SMB access using:
  - Samba username: `admin`
  - password: the same password provided in `.env` as `TC_PASSWORD`
- guest access disabled

Current user experience:
- the Time Capsule advertises `_smb._tcp`
- the default instance name is `Time Capsule Samba 4`
- the default host label is `timecapsulesamba4`
- the share is available at:
  - `smb://timecapsulesamba4.local/Data`

Current auth model:
- SMB login user: `admin`
- the login is mapped to Unix `root`
- filesystem access still runs as `root`
- this avoids the privilege-switch failures seen with non-root identities on this firmware

## Device Profile

The target device facts are documented in [plan/device-profile.md](/Users/jameschang/git/TimeCapsuleSMB/plan/device-profile.md), but the important points are:

- OS: `NetBSD 6.0`
- arch: `evbarm`
- CPU family: `ARM Cortex-A9`
- memory: `256 MiB`
- root fs is tiny
- flash is tiny
- `/mnt/Memory` is only about `16 MiB`

Relevant mount points:
- `/` on `/dev/md0a`
- `/mnt/Flash` on `/dev/flash2a`
- `/mnt/Memory` on `tmpfs`
- internal HDD usually appears as `/dev/dk2` or `/dev/dk3`
- Apple’s expected mount point is `/Volumes/dk2` or `/Volumes/dk3`

Current live storage numbers observed during development:
- `/`: about `15.5 MiB` total, about `4.7 MiB` free
- `/mnt/Flash`: about `1 MiB` total, about `933 KiB` free
- `/mnt/Memory`: `16 MiB` total, often under `2 MiB` free once Samba is staged
- `/Volumes/dk2`: effectively the large 2 TB data disk

These constraints drive almost every design decision in this repo.

## Why The Current Architecture Exists

### Flash is too small

The flash filesystem cannot hold the real Samba runtime.

### Root is too small

The root filesystem is also too small to be the main runtime home.

### RAM is too small to be the persistent home

`/mnt/Memory` is only about `16 MiB`, and the staged Samba runtime consumes most of it. It is good for transient execution, not for persistence.

### The HDD is large but unreliable as an execution root

The internal HDD can be mounted locally and is fully usable for reads and writes. This was confirmed in [plan/disk-investigation-results.md](/Users/jameschang/git/TimeCapsuleSMB/plan/disk-investigation-results.md).

However, Apple may later unmount or sleep the disk. Running `smbd` directly from `/Volumes/dk2` is therefore unsafe.

### Final result

The actual working split is:

- persistent payload on HDD:
  - `/Volumes/dkX/samba4/smbd`
  - `/Volumes/dkX/samba4/mdns-smbd-advertiser`
  - `/Volumes/dkX/samba4/smb.conf.template`
  - `/Volumes/dkX/samba4/private/smbpasswd`
  - `/Volumes/dkX/samba4/private/username.map`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/dfree.sh`
- transient runtime on RAM disk:
  - `/mnt/Memory/samba4`

This gives:
- persistence on disk
- safe execution from RAM
- only tiny always-mounted files on flash

## Why Samba 4.3.13

The project did not land on Samba 4.3 by accident.

### Samba 3

Samba 3 worked well enough to prove the device could serve files, but it had limitations and older behavior bugs, and it was not the final direction.

### Samba 4.8

Earlier Samba 4.8 attempts were too difficult to build and adapt for this environment.

### Samba 4.2

Samba 4.2 was built successfully, but it hit a real runtime bug on-device:
- a `talloc` / `loadparm` use-after-free class issue on first client session

Separately, the NetBSD 10-era toolchain path also exposed incompatible directory API behavior on this NetBSD 6 box.

### NetBSD 10 build path

A NetBSD 10-generated static binary could execute, but a direct directory probe confirmed that important directory APIs failed on the Time Capsule. That made the NetBSD 10 route unacceptable for full Samba serving.

### NetBSD 7 build path

The working result came from:
- NetBSD 7 source tree
- static `earmv4` build
- Samba 4.3.13

That combination:
- builds reproducibly
- executes correctly on the Time Capsule
- serves files successfully

The important build logic is now under [build/](/Users/jameschang/git/TimeCapsuleSMB/build).

## Why We Do Not Use Apple’s Native SMB Bonjour Path

This was investigated deeply.

Apple’s stack does have a native SMB/mDNS path involving:
- `/etc/cifs/cm_cfg.txt`
- `/etc/cifs/cs_cfg.txt`
- `wcifsfs`
- `mDNSResponder`
- `ACPd`

Relevant backups are in:
- [plan/apple-cifs-backup](/Users/jameschang/git/TimeCapsuleSMB/plan/apple-cifs-backup)
- [plan/apple-cifs-backup-2](/Users/jameschang/git/TimeCapsuleSMB/plan/apple-cifs-backup-2)

Important findings:
- Apple’s own `_smb._tcp` path is coupled to Apple’s file-sharing stack
- when Apple’s stack owns that path, Finder tends to reconnect through Apple SMB/AFP rather than our Samba service
- letting Apple reclaim that path conflicts with the goal of running our own `smbd`

So the current system deliberately does not use Apple’s SMB advertisement path.

Instead it uses a separate tiny helper:
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

This helper:
- advertises `_smb._tcp.local.`
- resolves to the custom host label, by default `timecapsulesamba4.local`
- points clients at our `smbd` on port `445`

This was a key design fork.

## Boot Flow In Detail

The boot logic lives in:
- [boot/samba4/rc.local](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/rc.local)
- [boot/samba4/start-samba.sh](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/start-samba.sh)

### `rc.local`

`rc.local` is intentionally tiny. It just backgrounds `start-samba.sh`.

This matters because:
- boot ordering is messy
- the HDD device nodes may not exist yet when `rc.local` first runs
- a longer wait loop belongs in the second-stage script, not directly inline in the boot hook

### `start-samba.sh`

`start-samba.sh` does the real work:

1. kills any prior `smbd` and mDNS advertiser
2. recreates the RAM runtime tree
3. waits for `/dev/dk2` or `/dev/dk3`
4. mounts the corresponding volume under `/Volumes/dk2` or `/Volumes/dk3`
5. discovers the real data root by checking:
   - `ShareRoot/.com.apple.timemachine.supported`
   - `Shared/.com.apple.timemachine.supported`
6. waits for the device IP on `bridge0`
7. finds the persistent payload directory
8. copies `smbd` and `mdns-smbd-advertiser` into `/mnt/Memory/samba4/sbin`
9. renders `smb.conf` from the template
10. starts the mDNS advertiser
11. starts `smbd`

The boot log is written to:
- `/mnt/Memory/samba4/var/rc.local.log`

Important bug lessons from getting this stable:
- the script cannot assume `/dev/dk2` exists immediately
- the script must use `-b` for block devices, not `-c`
- it cannot call non-existent utilities like `dirname`
- it must tolerate a long delay before the disk appears

## SMB Runtime Layout

When boot succeeds, the runtime tree under `/mnt/Memory/samba4` contains:
- `sbin/smbd`
- `sbin/mdns-smbd-advertiser`
- `etc/smb.conf`
- `var/`
- `locks/`
- `private/`

Current persistent auth files live on the HDD:
- `/Volumes/dk2/samba4/private/smbpasswd`
- `/Volumes/dk2/samba4/private/username.map`

Current rendered Samba config characteristics:
- `security = user`
- `min protocol = SMB2`
- `max protocol = SMB3`
- `guest ok = no`
- `valid users = admin root`
- `force user = root`
- `force group = wheel`
- `path = /Volumes/dk2/ShareRoot` on the tested box

Current auth mapping:
- `admin` maps to Unix `root`
- the `smbpasswd` backend contains a `root` entry
- `username.map` contains:
  - `root = admin`

This is intentionally pragmatic:
- login is authenticated
- the filesystem still runs as `root`
- it avoids the earlier non-root privilege-switch failures on this firmware

## mDNS Advertiser Details

The mDNS helper is:
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

It is built from:
- [build/mdns-advertiser.c](/Users/jameschang/git/TimeCapsuleSMB/build/mdns-advertiser.c)
- [build/mdns.sh](/Users/jameschang/git/TimeCapsuleSMB/build/mdns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary
- about `190 KiB` stripped
- small enough to stage into RAM without materially changing the overall design

At runtime it advertises:
- service type: `_smb._tcp.local.`
- instance name: by default `Time Capsule Samba 4`
- host label: by default `timecapsulesamba4`
- port: `445`
- A record: current IPv4 from `bridge0`

This is now the chosen discovery path.

## Current User-Facing Workflow

The intended user flow is:

1. bootstrap the local host
   - [scripts/bootstrap_host.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/bootstrap_host.py)
2. discover the Time Capsule and enable SSH
   - [scripts/prep_device.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/prep_device.py)
3. generate local config
   - [scripts/configure.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/configure.py)
4. deploy and reboot
   - [scripts/deploy.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/deploy.py)
5. run local diagnostics
   - [scripts/doctor.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/doctor.py)

`configure.py` writes repo-root `.env`.

Current important `.env` values include:
- `TC_HOST`
- `TC_PASSWORD`
- `TC_NET_IFACE`
- `TC_SHARE_NAME`
- `TC_SAMBA_USER`
- `TC_NETBIOS_NAME`
- `TC_PAYLOAD_DIR_NAME`
- `TC_MDNS_INSTANCE_NAME`
- `TC_MDNS_HOST_LABEL`

Current defaults:
- `TC_SHARE_NAME=Data`
- `TC_SAMBA_USER=admin`
- `TC_NETBIOS_NAME=TimeCapsule`
- `TC_PAYLOAD_DIR_NAME=samba4`
- `TC_MDNS_INSTANCE_NAME=Time Capsule Samba 4`
- `TC_MDNS_HOST_LABEL=timecapsulesamba4`

## Doctor Script

[scripts/doctor.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/doctor.py) is a non-destructive local diagnostic helper.

It checks:
- `.env` completeness
- required local tools
- whether the checked-in binaries exist
- SSH reachability
- SMB reachability
- `_smb._tcp` browse and resolve
- authenticated `smbutil view`

It does not:
- deploy
- reboot
- change the device

Typical usage:

```bash
.venv/bin/python scripts/doctor.py
```

Optional skips:

```bash
.venv/bin/python scripts/doctor.py --skip-ssh
.venv/bin/python scripts/doctor.py --skip-bonjour
.venv/bin/python scripts/doctor.py --skip-smb
```

The normal goal is to use it as a quick health check after:
- local setup
- deploy
- reboot

## Deploy Details

[scripts/deploy.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/deploy.py) currently does all of the following:

- loads `.env`
- discovers the correct volume root on the Time Capsule
- creates the persistent payload dir under `/Volumes/dkX/samba4`
- copies:
  - `smbd`
  - `mdns-smbd-advertiser`
  - `smb.conf.template`
  - `rc.local`
  - `start-samba.sh`
  - `dfree.sh`
- creates:
  - `private/smbpasswd`
  - `private/username.map`
- reboots by default

The current password flow is:
- `TC_PASSWORD` is also used as the Samba password
- no separate Apple auth backend is used

This gives a near-enough user experience:
- same password as the device password already entered during setup
- without reverse-engineering Apple’s actual SMB auth backend

## What The Build Pipeline Produces

The build pipeline under [build/](/Users/jameschang/git/TimeCapsuleSMB/build) is for maintainers, not normal users.

Current important outputs:
- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

It assumes:
- a NetBSD VM
- root-owned cross-build tree under `/root`
- `su` for the actual build steps

Important note:
- the active supported build path is NetBSD 7, not NetBSD 10

## Important Historical Findings

These are the findings that matter to future maintainers.

### The internal disk can be mounted locally

This was a major breakthrough. The Time Capsule can locally mount `/dev/dk2` with `mount_hfs` without needing a Mac to first trigger Apple sharing.

See:
- [plan/disk-investigation-results.md](/Users/jameschang/git/TimeCapsuleSMB/plan/disk-investigation-results.md)

### Running `smbd` from the HDD is a bad idea

The HDD may be unmounted or slept by Apple later. That is why `smbd` is staged into RAM.

### Running the mDNS helper from the HDD would be less catastrophic, but we currently keep it in RAM too

If it died, discovery would break but file serving would remain up. For now it is still staged into RAM.

### Apple’s SMB advertisement path is not a harmless metadata layer

If Apple’s own SMB/AFP stack is allowed to reclaim its native path, Finder may reconnect through Apple services rather than our Samba.

That is why we chose a separate mDNS helper.

### The Time Capsule firmware is missing small utility commands you might expect

Examples encountered during debugging:
- no `grep`
- no `dirname`
- no `find`
- no `strings`

Shell scripts must be written very conservatively.

### Non-root Unix identity handling is risky

Earlier Samba attempts on this firmware ran into privilege-switch and identity issues with non-root mappings.

That is why the current authenticated design still maps to `root`.

## Known Risks And Caveats

- This is still LAN-only software.
- The current authenticated design still maps file access to `root`.
- `/mnt/Memory` is tight; only about `1-2 MiB` may remain free after staging.
- The repo still assumes Time Capsule-specific behavior such as:
  - `bridge0`
  - `dk2` / `dk3`
  - `ShareRoot` / `Shared`
- Apple firmware behavior may still change runtime mount timing or disk state in edge cases.

## Verification Commands

Current useful checks from the Mac:

Browse SMB service advertisements:

```bash
dns-sd -B _smb._tcp local.
```

Resolve the SMB service:

```bash
dns-sd -L "Time Capsule Samba 4" _smb._tcp local.
```

List shares as authenticated user:

```bash
smbutil view //admin:<password>@timecapsulesamba4.local
```

Mount the share:

```bash
mount_smbfs //admin:<password>@timecapsulesamba4.local/Data /tmp/tc-auth-mount
```

Current expected result:
- `IPC$`
- `Data`

Expected negative test:

```bash
smbutil view //guest:@timecapsulesamba4.local
```

That should fail with an authentication error.

## Files Worth Reading

Short overview:
- [README.md](/Users/jameschang/git/TimeCapsuleSMB/README.md)

Most important investigation docs:
- [plan/device-profile.md](/Users/jameschang/git/TimeCapsuleSMB/plan/device-profile.md)
- [plan/disk-investigation-results.md](/Users/jameschang/git/TimeCapsuleSMB/plan/disk-investigation-results.md)
- [plan/session-handoff-2026-04-03.md](/Users/jameschang/git/TimeCapsuleSMB/plan/session-handoff-2026-04-03.md)

Apple CIFS / Bonjour investigation backups:
- [plan/apple-cifs-backup](/Users/jameschang/git/TimeCapsuleSMB/plan/apple-cifs-backup)
- [plan/apple-cifs-backup-2](/Users/jameschang/git/TimeCapsuleSMB/plan/apple-cifs-backup-2)

## Summary

The current system is no longer just an experiment:
- it builds reproducibly
- deploys from checked-in artifacts
- survives reboot
- advertises itself over Bonjour
- authenticates as `admin`
- serves the internal disk through Samba 4

The main remaining “nice to have” work is polish, not core functionality.
