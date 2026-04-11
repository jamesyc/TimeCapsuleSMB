# TimeCapsuleSMB Detail Reference

This file is the long-form engineering reference for the current system.

It is intentionally denser than [README.md](README.md). The README is the user-facing overview. This file is for maintainers, contributors, and users who want the actual constraints, rationale, and implementation details in one place before they start modifying the box or the tooling.

## Current Working State

The current system works end to end on the target Apple AirPort Time Capsule.

What is working now:
- static Samba 4.8.x built from NetBSD 7 sources
- static tiny SMB / Time Machine mDNS advertiser
- boot-time runtime staging via `/mnt/Flash/rc.local`
- boot-time watchdog for `smbd` and the mDNS helper
- direct SMB service on port `445`
- Bonjour advertisement for:
  - `_smb._tcp`
  - optional `_adisk._tcp`
  - `_device-info._tcp`
- authenticated SMB access using:
  - Samba username: `admin`
  - password: the same password provided in `.env` as `TC_PASSWORD`
- guest access disabled
- deploy-time device compatibility detection
- clean uninstall via `tcapsule uninstall`

Current user experience:
- the Time Capsule advertises `_smb._tcp`
- the Time Capsule can advertise `_adisk._tcp` for Time Machine
- the Time Capsule advertises `_device-info._tcp` with a Finder model hint
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

The important target device facts are:

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

Current compatibility classification in the repo is:
- NetBSD 6.x `evbarm`: current supported deploy target, corresponding to 5th generation Time Capsules
- NetBSD 4.x `evbarm`: detected explicitly as older 1st-4th generation hardware, but not supported by the current checked-in Samba 4 payload

## Why The Current Architecture Exists

### Flash is too small

The flash filesystem cannot hold the real Samba runtime.

### Root is too small

The root filesystem is also too small to be the main runtime home.

### RAM is too small to be the persistent home

`/mnt/Memory` is only about `16 MiB`, and the staged Samba runtime consumes most of it. It is good for transient execution, not for persistence.

### The HDD is large but unreliable as an execution root

The internal HDD can be mounted locally and is fully usable for reads and writes.

However, Apple may later unmount or sleep the disk. Running `smbd` directly from `/Volumes/dk2` is therefore unsafe.

### Final result

The actual working split is:

- persistent payload on HDD:
  - `/Volumes/dkX/samba4/smbd`
  - `/Volumes/dkX/samba4/mdns-smbd-advertiser`
  - `/Volumes/dkX/samba4/smb.conf.template`
  - `/Volumes/dkX/samba4/private/smbpasswd`
  - `/Volumes/dkX/samba4/private/username.map`
  - `/Volumes/dkX/samba4/private/xattr.tdb`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/watchdog.sh`
  - `/mnt/Flash/dfree.sh`
- transient runtime on RAM disk:
  - `/mnt/Memory/samba4`

This gives:
- persistence on disk
- safe execution from RAM
- only tiny always-mounted files on flash

## Why Samba 4.8

The project did not land on Samba 4.8 by accident.

### Samba 3

Samba 3.x worked well enough to prove the device could serve files, and was a small 6MB, but has issues with directory traversal with NetBSD 6. This meant `ls` would not work in the Samba share. As Samba 3.x was the first version with SMB2 support, it was rather incomplete and buggy.

### Samba 4.0

Tried 4.0 as it in theory had better SMB2 support than 3.x but it had the same directory traversal bug. It was significantly harder to compile than 3.x but a lot easier than 4.2-4.8, so it served well as a stepping stone in getting 4.8 to work as trying to compile 4.8 from scratch at first drove me crazy.

### Samba 4.2

Samba 4.2 was built successfully, but it hit a runtime bug on-device:
- a `talloc` / `loadparm` use-after-free class issue on first client session

Separately, the NetBSD 10-era toolchain path also exposed incompatible directory API behavior on the NetBSD 6 box.

### Samba 4.3

Samba 4.3 was an important stepping stone, but it was not enough. It did not run into any bugs as a network file share. It worked as a normal authenticated network share, but not as a real Time Machine target. 

In practice, 4.3 proved the architecture and deployment model, while 4.8 is the version that enables the full Time Machine-oriented share behavior.

### Samba 4.8

Samba 4.8 is the current target because it gives the project a usable Time Machine stack through `vfs_fruit`.

With the current static-module build, the shipped config supports:
- `fruit`
- `streams_xattr`
- `acl_xattr`
- `xattr_tdb`
- `fruit:time machine = yes`

## NetBSD 6 build path

As the Time Capsule ran NetBSD 6, initial attempts used the NetBSD 6 source code to attempt to build. This failed terribly, as it turns out the NetBSD 6 source did not support earmv4 build output. I presume Apple used some custom toolchain. 

### NetBSD 10 build path

My VM was running NetBSD 10. A NetBSD 10-generated static binary could execute, and it worked fine for Samba 3.x, but later direct directory probes confirmed that important directory APIs failed on the Time Capsule. That made the NetBSD 10 route unacceptable for full Samba serving.

### NetBSD 7 build path

The working result came from:
- NetBSD 7 source tree
- static `earmv4` build
- Samba 4.8.x

That combination:
- builds reproducibly
- executes correctly on the Time Capsule
- serves files successfully
- supports Time Machine semantics through `vfs_fruit`

The important build logic is now under [build/](build).

## Why We Do Not Use Apple’s Native SMB Bonjour Path

This was investigated deeply.

Apple’s stack does have a native SMB/mDNS path involving:
- `/etc/cifs/cm_cfg.txt`
- `/etc/cifs/cs_cfg.txt`
- `wcifsfs`
- `mDNSResponder`
- `ACPd`

Important findings:
- Apple’s own `_smb._tcp` path is coupled to Apple’s file-sharing stack
- when Apple’s stack owns that path, Finder tends to reconnect through Apple SMB/AFP rather than our Samba service
- letting Apple reclaim that path conflicts with the goal of running our own `smbd`

So the current system deliberately does not use Apple’s SMB advertisement path or Apple’s ownership of those records.

Instead it uses a separate tiny helper:
- [bin/mdns/mdns-smbd-advertiser](bin/mdns/mdns-smbd-advertiser)

This helper:
- advertises `_smb._tcp.local.`
- can also advertise `_adisk._tcp.local.` for Time Machine when started with `--adisk-share`
- advertises `_device-info._tcp.local.` with a `model=...` TXT record for Finder device identification
- resolves to the custom host label, by default `timecapsulesamba4.local`
- points clients at our `smbd` on port `445`

## Boot Flow In Detail

The boot logic lives in:
- [src/timecapsulesmb/assets/boot/samba4/rc.local](src/timecapsulesmb/assets/boot/samba4/rc.local)
- [src/timecapsulesmb/assets/boot/samba4/start-samba.sh](src/timecapsulesmb/assets/boot/samba4/start-samba.sh)
- [src/timecapsulesmb/assets/boot/samba4/watchdog.sh](src/timecapsulesmb/assets/boot/samba4/watchdog.sh)

### `rc.local`

`rc.local` is intentionally tiny. It just backgrounds `start-samba.sh` and `watchdog.sh`.

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
6. waits for the device IP on the configured network interface
   - default: `bridge0`
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

### `watchdog.sh`

`watchdog.sh` is a simple long-running supervisor launched at boot from flash.

Current behavior:
- polls every `300` seconds
- if `smbd` is missing, starts it again
- if `mdns-smbd-advertiser` is missing, starts it again

This is intentionally simple:
- SMB transfers are not interrupted because `smbd` is only restarted when absent
- the mDNS helper is also only restarted when absent

The watchdog log is written to:
- `/mnt/Memory/samba4/var/watchdog.log`

Important implementation detail:
- on this NetBSD firmware, `pkill` matches the truncated process name `mdns-smbd-advert`, not the full `mdns-smbd-advertiser`
- the watchdog therefore uses the truncated process name for liveness checks and restarts

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

Current persistent Time Machine metadata state also lives on the HDD:
- `/Volumes/dk2/samba4/private/xattr.tdb`

Current rendered Samba config characteristics:
- `security = user`
- `min protocol = SMB2`
- `max protocol = SMB3`
- `guest ok = no`
- `valid users = admin root`
- `force user = root`
- `force group = wheel`
- `path = /Volumes/dk2/ShareRoot` on the tested box
- `vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb`
- `fruit:resource = file`
- `fruit:metadata = stream`
- `fruit:time machine = yes`
- `fruit:posix_rename = yes`
- `streams_xattr:store_stream_type = no`
- `acl_xattr:ignore system acls = yes`
- `xattr_tdb:file = /Volumes/dk2/samba4/private/xattr.tdb` on the tested box

Current auth mapping:
- `admin` maps to Unix `root`
- the `smbpasswd` backend contains a `root` entry
- `username.map` contains:
  - `root = admin`

This is intentionally pragmatic:
- login is authenticated
- the filesystem still runs as `root`
- it avoids the earlier non-root privilege-switch failures on this firmware

Operational note:
- the live runtime config at `/mnt/Memory/samba4/etc/smb.conf` is regenerated on each boot
- `/mnt/Memory` is a RAM disk, so live edits there are ephemeral
- temporary debug edits such as one-off `log level = ...` lines will disappear after reboot
- watchdog logs under `/mnt/Memory/samba4/var` are also ephemeral for the same reason

## mDNS Advertiser Details

The mDNS helper is:
- [bin/mdns/mdns-smbd-advertiser](bin/mdns/mdns-smbd-advertiser)

It is built from:
- [build/mdns-advertiser.c](build/mdns-advertiser.c)
- [build/mdns.sh](build/mdns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary
- about `198 KiB` stripped in the current checked-in artifact
- small enough to stage into RAM without materially changing the overall design

At runtime it advertises:
- service type: `_smb._tcp.local.`
- optional Time Machine service type: `_adisk._tcp.local.`
- device metadata service type: `_device-info._tcp.local.`
- instance name: by default `Time Capsule Samba 4`
- host label: by default `timecapsulesamba4`
- port: `445`
- A record: current IPv4 from the configured network interface
  - default: `bridge0`
- `_device-info._tcp` TXT:
  - `model=<configured-device-model>`

Current validation and behavior notes:
- service instance names and host labels are validated as single DNS labels
- service types are validated as dotted DNS names
- `_adisk._tcp` TXT payload sizing is validated before advertisement
- `_device-info._tcp` `model=...` TXT sizing is validated before advertisement
- `_device-info._tcp` exists to influence Finder identification and icon behavior, not to expose a separate connectable service

## Current User-Facing Workflow

The intended user flow is:

1. bootstrap the local host
   - [`./tcapsule bootstrap`](./tcapsule)
2. generate local config
   - [src/timecapsulesmb/cli/configure.py](src/timecapsulesmb/cli/configure.py)
3. enable or disable SSH as needed
   - [src/timecapsulesmb/cli/prep_device.py](src/timecapsulesmb/cli/prep_device.py)
4. deploy and reboot
   - [src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py)
5. run local diagnostics
   - [src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py)
6. remove the payload later if needed
   - [src/timecapsulesmb/cli/uninstall.py](src/timecapsulesmb/cli/uninstall.py)

`tcapsule configure` writes repo-root `.env`.

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
- `TC_MDNS_DEVICE_MODEL`

Current defaults:
- `TC_SHARE_NAME=Data`
- `TC_SAMBA_USER=admin`
- `TC_NETBIOS_NAME=TimeCapsule`
- `TC_PAYLOAD_DIR_NAME=samba4`
- `TC_MDNS_INSTANCE_NAME=Time Capsule Samba 4`
- `TC_MDNS_HOST_LABEL=timecapsulesamba4`
- `TC_MDNS_DEVICE_MODEL=TimeCapsule`

Workflow details:
- `configure` now starts by attempting mDNS discovery of the Time Capsule on the local network
- if SSH is already reachable, `configure` validates the SSH target/password and can infer an `mDNS device model hint` from the detected device generation
- `configure` validates user-facing mDNS/share inputs before writing `.env`
- the command entrypoints live under [src/timecapsulesmb/cli/](src/timecapsulesmb/cli)
- the deploy/runtime logic lives under [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy) and [src/timecapsulesmb/device/](src/timecapsulesmb/device)
- the checked-in binaries and build tooling are visible in the repo, so advanced users can swap binaries, rebuild artifacts, or trace the exact boot/runtime layout

## Host-Side Architecture

Current important package areas:
- [src/timecapsulesmb/cli/](src/timecapsulesmb/cli): command entrypoints for `bootstrap`, `discover`, `configure`, `prep-device`, `deploy`, `doctor`, and `uninstall`
- [src/timecapsulesmb/core/](src/timecapsulesmb/core): shared config parsing, defaults, and common models
- [src/timecapsulesmb/transport/](src/timecapsulesmb/transport): local command execution plus SSH and SCP helpers
- [src/timecapsulesmb/discovery/](src/timecapsulesmb/discovery): Bonjour-based device discovery
- [src/timecapsulesmb/integrations/](src/timecapsulesmb/integrations): AirPyrt-backed SSH enable and disable flows
- [src/timecapsulesmb/checks/](src/timecapsulesmb/checks): reusable local, network, Bonjour, and SMB verification checks
- [src/timecapsulesmb/device/](src/timecapsulesmb/device): remote probing for device-specific layout such as the active `dk2` or `dk3` volume root, plus generation / compatibility classification
- [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy): auth generation, template rendering, deployment planning, execution, dry-run formatting, artifact resolution, and post-deploy verification
- [src/timecapsulesmb/assets/](src/timecapsulesmb/assets): packaged boot templates and artifact metadata
- [build/](build): maintainer build tooling, including Samba cross-exec record/replay helpers

Practical consequence:
- if you want to modify how the box is discovered, start in `discovery/`
- if you want to change what gets uploaded, start in `deploy/planner.py` and `deploy/executor.py`
- if you want to change the on-device boot behavior, inspect the packaged boot assets and the runtime layout sections below
- if you want to replace binaries or rebuild them, inspect the artifact manifest plus the `build/` tree

## Doctor Command

[src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py) is a non-destructive local diagnostic helper.

It checks:
- `.env` completeness
- required local tools
- whether the required checked-in binaries exist and match the expected checksums
- SSH reachability
- SMB reachability
- `_smb._tcp` browse and resolve
- authenticated `smbutil view`
- authenticated SMB file operations on the mounted share
- that the configured share name is present in the authenticated SMB listing

It does not:
- deploy
- reboot
- change the device

Current output behavior:
- in normal human-readable mode, checks are printed as they complete rather than being buffered until the end
- `--json` still emits one structured payload at the end

Typical usage:

```bash
.venv/bin/tcapsule doctor
```

Machine-readable output:

```bash
.venv/bin/tcapsule doctor --json
```

Optional skips:

```bash
.venv/bin/tcapsule doctor --skip-ssh
.venv/bin/tcapsule doctor --skip-bonjour
.venv/bin/tcapsule doctor --skip-smb
```

The normal goal is to use it as a quick health check after:
- local setup
- deploy
- reboot

## Deploy Details

[src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py) is now mostly an orchestrator over shared modules in [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy) and [src/timecapsulesmb/device/](src/timecapsulesmb/device).

Current deploy flow:

- loads `.env`
- validates the required binary artifacts against the artifact manifest
- discovers the correct volume root on the Time Capsule
- probes device compatibility and rejects unsupported targets before upload
- computes the device-specific runtime and payload paths
- builds a deployment plan before execution
- creates the persistent payload dir under `/Volumes/dkX/samba4`
- uploads the checked-in binaries:
  - `smbd`
  - `mdns-smbd-advertiser`
- renders and uploads the packaged boot/runtime files:
  - `smb.conf.template`
  - `rc.local`
  - `start-samba.sh`
  - `watchdog.sh`
  - `dfree.sh`
- generates and installs:
  - `private/smbpasswd`
  - `private/username.map`
- applies the required permissions on files and directories
- reboots by default
- verifies Bonjour and authenticated SMB access after reboot in the normal path using the same shared checks used by `doctor`

Current compatibility behavior:
- NetBSD 6 `evbarm` devices are accepted for the current `samba4` payload family
- NetBSD 4 `evbarm` devices are detected as older hardware and rejected by `deploy`
- `configure` reuses the same classification logic to choose a better default Finder model hint

The current password flow is:
- `TC_PASSWORD` is also used as the Samba password
- no separate Apple auth backend is used

This gives a near-enough user experience:
- same password as the device password already entered during setup
- without reverse-engineering Apple’s actual SMB auth backend

Useful operator modes:

```bash
.venv/bin/tcapsule deploy --dry-run
.venv/bin/tcapsule deploy --dry-run --json
```

Those are intended for users who want to inspect the exact remote actions before touching the box.

## Artifact Resolution

The active deployable binaries live in the repo under [bin/](bin).

The host-side code does not hardcode the binary repo paths directly. Artifact path knowledge is centralized in:
- [src/timecapsulesmb/assets/artifact-manifest.json](src/timecapsulesmb/assets/artifact-manifest.json)
- [src/timecapsulesmb/deploy/artifact_resolver.py](src/timecapsulesmb/deploy/artifact_resolver.py)
- [src/timecapsulesmb/deploy/artifacts.py](src/timecapsulesmb/deploy/artifacts.py)

This is useful if you are hacking on the repo because:
- deploy and doctor now resolve artifacts by logical name instead of constructing `bin/...` paths ad hoc
- checksum validation and path resolution happen through one layer
- future work can change where artifacts come from without rewriting deploy and doctor again

## What The Build Pipeline Produces

The build pipeline under [build/](build) is for maintainers, not normal users.

Current important outputs:
- [bin/samba4/smbd](bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](bin/mdns/mdns-smbd-advertiser)

It assumes:
- a NetBSD VM
- root-owned cross-build tree under `/root`
- `su` for the actual build steps

Important note:
- the active supported build path is NetBSD 7, not NetBSD 10

Current Samba configure probe modes:
- live mode:
  - [build/samba4.sh](build/samba4.sh)
- record mode:
  - [build/samba4record.sh](build/samba4record.sh)
  - writes cross-exec probe captures under `$OUT/compat` by default
- replay mode:
  - [build/samba4replay.sh](build/samba4replay.sh)
  - reuses a previously recorded compat file instead of SSHing to a live device

This is useful when comparing NetBSD 4 and NetBSD 6 devices:
- one replay file can be captured per device family
- later configure runs can be replayed offline from those saved probe results

## Important Historical Findings

These are the findings that matter to future maintainers.

### The internal disk can be mounted locally

This was a major breakthrough. The Time Capsule can locally mount `/dev/dk2` with `mount_hfs` without needing a Mac to first trigger Apple sharing.

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
- [README.md](README.md)

## Summary

The current system is no longer just an experiment:
- it builds reproducibly
- deploys from checked-in artifacts
- survives reboot
- advertises itself over Bonjour
- authenticates as `admin`
- serves the internal disk through Samba 4.8
- supports Time Machine via `vfs_fruit`

The main remaining “nice to have” work is polish, not core functionality.
