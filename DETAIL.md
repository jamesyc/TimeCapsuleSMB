# TimeCapsuleSMB Detail Reference

This file is the long-form engineering reference for the current system.

It is intentionally denser than [README.md](README.md). The README is the user-facing overview. This file is for maintainers, contributors, and users who want the actual constraints, rationale, and implementation details in one place before they start modifying the box or the tooling.

## Current Working State

The current system works end to end on the target Apple AirPort Time Capsule.

What is working now:
- static Samba 4.8.x built from NetBSD 7 sources for NetBSD 6-era Time Capsules
- static Samba 4.8.x built from NetBSD 4 sources for older NetBSD 4-era Time Capsules
- static tiny SMB / Time Machine mDNS advertiser
- optional static NBNS responder for NetBIOS name discovery
- boot-time runtime staging via `/mnt/Flash/rc.local`
- boot-time watchdog for `smbd`, the mDNS helper, and the optional NBNS helper
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
- manual NetBSD 4 activation via `tcapsule activate`
- clean uninstall via `tcapsule uninstall`

Current user experience:
- the Time Capsule advertises `_smb._tcp`
- the Time Capsule can advertise `_adisk._tcp` for Time Machine
- the Time Capsule advertises `_device-info._tcp` with a Finder model hint
- the Time Capsule can optionally answer NBNS name queries for the configured NetBIOS name
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
- NetBSD 4.x `evbarm`: supported as older 1st-4th generation hardware, with a separate NetBSD 4 artifact set and activation path

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
  - `/Volumes/dkX/samba4/mdns-advertiser`
  - `/Volumes/dkX/samba4/nbns-advertiser`
  - `/Volumes/dkX/samba4/smb.conf.template`
  - `/Volumes/dkX/samba4/private/smbpasswd`
  - `/Volumes/dkX/samba4/private/username.map`
  - `/Volumes/dkX/samba4/private/nbns.enabled`
  - `/Volumes/dkX/samba4/private/xattr.tdb`
  - `/Volumes/dkX/samba4/cache`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/watchdog.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/mdns-advertiser`
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

Current maintainer build lanes:
- NetBSD 7 SDK lane:
  - [build/download.sh](build/download.sh)
  - [build/bootstrap.sh](build/bootstrap.sh)
- NetBSD 4 SDK lane:
  - [build/downloadold.sh](build/downloadold.sh)
  - [build/bootstrapold.sh](build/bootstrapold.sh)
- NetBSD 7 Samba 4 lane:
  - [build/downloadsamba4.sh](build/downloadsamba4.sh)
  - [build/samba4.sh](build/samba4.sh)
- NetBSD 4 Samba 3 lane:
  - [build/downloadsamba3old.sh](build/downloadsamba3old.sh)
  - [build/samba3old.sh](build/samba3old.sh)

The direct scripts target the NetBSD 7 lane by default. The `*old.sh` wrappers select the NetBSD 4 lane.

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
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

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

1. kills any prior `smbd`, mDNS advertiser, and NBNS responder
2. recreates the RAM runtime tree
3. waits for `/dev/dk2` or `/dev/dk3`
4. mounts the corresponding volume under `/Volumes/dk2` or `/Volumes/dk3`
5. discovers the real data root by checking:
   - `ShareRoot/.com.apple.timemachine.supported`
   - `Shared/.com.apple.timemachine.supported`
6. waits for the device IP on the configured network interface
   - default: `bridge0`
7. finds the persistent payload directory
8. copies `smbd` into `/mnt/Memory/samba4/sbin`
9. if `private/nbns.enabled` exists in the persistent payload, also copies `nbns-advertiser` into `/mnt/Memory/samba4/sbin`
10. renders `smb.conf` from the template
11. starts the mDNS advertiser
12. starts the NBNS responder if enabled
13. starts `smbd`

The boot log is written to:
- `/mnt/Memory/samba4/var/rc.local.log`

Important bug lessons from getting this stable:
- the script cannot assume `/dev/dk2` exists immediately
- the script must use `-b` for block devices, not `-c`
- it cannot call non-existent utilities like `dirname`
- it must tolerate a long delay before the disk appears
- on NetBSD 4, cache state is kept on the HDD instead of `/mnt/Memory` to preserve RAM-disk headroom
- the persistent `xattr.tdb` must stay on the HDD because it records extended attribute state for files on the share

### `watchdog.sh`

`watchdog.sh` is a simple long-running supervisor launched at boot from flash.

Current behavior:
- polls every `300` seconds
- if `smbd` is missing, starts it again
- if `mdns-advertiser` is missing, starts it again
- if `nbns-advertiser` is enabled and missing, starts it again

This is intentionally simple:
- SMB transfers are not interrupted because `smbd` is only restarted when absent
- the mDNS helper is also only restarted when absent

The watchdog log is written to:
- `/mnt/Memory/samba4/var/watchdog.log`

Important implementation detail:
- `mdns-advertiser` is short enough to match directly with `pkill`
- the watchdog therefore uses the truncated process name for liveness checks and restarts

NetBSD 4-specific shell note:
- `rc.local` disables `set -e` only around the watchdog probe/start block
- this avoids a NetBSD 4 `/bin/sh` edge case where launching a background job from an `if` branch can make the script report status `1`
- backgrounded jobs redirect stdin from `/dev/null` so they do not hold the SSH session open during manual activation

## SMB Runtime Layout

When boot succeeds, the runtime tree under `/mnt/Memory/samba4` contains:
- `sbin/smbd`
- optionally `sbin/nbns-advertiser`
- `etc/smb.conf`
- `var/`
- `locks/`
- `private/`

Current persistent auth files live on the HDD:
- `/Volumes/dk2/samba4/private/smbpasswd`
- `/Volumes/dk2/samba4/private/username.map`

Current optional NBNS state lives on the HDD:
- `/Volumes/dk2/samba4/nbns-advertiser`
- `/Volumes/dk2/samba4/private/nbns.enabled`

Current persistent Time Machine metadata state also lives on the HDD:
- `/Volumes/dk2/samba4/private/xattr.tdb`

Current NetBSD 4 Samba cache state lives on the HDD to preserve RAM headroom:
- `/Volumes/dk2/samba4/cache`

NetBSD 6 note:
- the normal NetBSD 6 runtime keeps Samba cache state in `/mnt/Memory/samba4/var`
- the HDD cache path above is used for the NetBSD 4 payload family because the NetBSD 4 RAM disk is too tight for the full runtime plus cache TDB growth

Current rendered Samba config characteristics:
- `security = user`
- `min protocol = SMB2`
- `max protocol = SMB3`
- `guest ok = no`
- `valid users = admin root`
- `force user = root`
- `force group = wheel`
- `path = /Volumes/dk2/ShareRoot` on the tested box
- `pid directory = /mnt/Memory/samba4/var`
- `lock directory = /mnt/Memory/samba4/locks`
- `state directory = /mnt/Memory/samba4/var`
- `cache directory = /mnt/Memory/samba4/var` on NetBSD 6
- `cache directory = /Volumes/dk2/samba4/cache` on NetBSD 4
- `private dir = /mnt/Memory/samba4/private`
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
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

It is built from:
- [build/mdns-advertiser.c](build/mdns-advertiser.c)
- [build/mdns.sh](build/mdns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 `earmv4` binary for the NetBSD 4 payload
- about `198 KiB` stripped in the current checked-in artifact
- installed on both the HDD payload and `/mnt/Flash`
- run from `/mnt/Flash` to save RAM-disk space

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

## NBNS Responder Details

The optional NBNS helper is:
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)

It is built from:
- [build/nbns-advertiser.c](build/nbns-advertiser.c)
- [build/nbns.sh](build/nbns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 `earmv4` binary for the NetBSD 4 payload
- about `188 KiB` stripped in the current checked-in artifact
- not enabled by default at runtime
- always deployed to the HDD payload, but only staged into RAM when explicitly enabled

Current behavior:
- binds UDP port `137`
- answers NBNS name queries for the configured NetBIOS name
- replies for both NetBIOS suffixes:
  - `0x00`
  - `0x20`
- returns the current IPv4 for the configured interface

Enablement model:
- the binary is uploaded to `/Volumes/dkX/samba4/nbns-advertiser` on every deploy
- runtime enablement is controlled by the marker file:
  - `/Volumes/dkX/samba4/private/nbns.enabled`
- `tcapsule deploy --install-nbns` creates that marker
- `--install-nbns` is rejected on NetBSD 4 because the RAM disk is too constrained
- plain `deploy` leaves the marker unchanged
- `uninstall` removes both the binary and the marker because it removes the entire payload tree

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
5. activate older NetBSD 4 devices if they do not auto-start Samba after reboot
   - [src/timecapsulesmb/cli/activate.py](src/timecapsulesmb/cli/activate.py)
6. run local diagnostics
   - [src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py)
7. remove the payload later if needed
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

Optional deploy flag:
- `--install-nbns`
  - enables the bundled NBNS responder on the next boot by creating `private/nbns.enabled`
  - rejected on NetBSD 4 because the RAM disk does not have enough headroom for NBNS in the normal payload

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
- [src/timecapsulesmb/cli/](src/timecapsulesmb/cli): command entrypoints for `bootstrap`, `discover`, `configure`, `prep-device`, `deploy`, `activate`, `doctor`, `repair-xattrs`, and `uninstall`
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
- optional NBNS name resolution when `private/nbns.enabled` is present on the device
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

## Repair Xattrs Command

[src/timecapsulesmb/cli/repair_xattrs.py](src/timecapsulesmb/cli/repair_xattrs.py) is a macOS-side repair helper for files whose SMB extended-attribute metadata became unreadable.

This was added after observing files on the mounted Samba share where:
- normal POSIX permissions looked fine
- TextEdit could open the file but could not save it back in place
- `xattr -l <file>` failed with `Invalid argument`
- `ls -lO@ <file>` showed the macOS `arch` file flag

The repair is intentionally narrow. It scans regular files, identifies files where `xattr -l` fails and the `arch` flag is present, then repairs by running:

```bash
chflags noarch <file>
```

Typical scan-and-prompt usage:

```bash
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data
```

If `TC_SHARE_NAME` is set in `.env` and the share is mounted under `/Volumes/<TC_SHARE_NAME>`, `--path` can be omitted:

```bash
.venv/bin/tcapsule repair-xattrs
```

Useful modes:

```bash
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data --dry-run
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data --yes
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data/some-folder --no-recursive
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data --max-depth 2
```

Default safety behavior:
- prompts before changing files unless `--yes` is passed
- verifies file size is unchanged after repair
- verifies `xattr -l` succeeds after repair
- skips symlinks
- skips hidden dot paths unless `--include-hidden` is passed
- skips Time Machine and bundle-like paths unless `--include-time-machine` is passed

This command should be treated as a targeted cleanup tool for user files, not as a general metadata migration command. Do not run it over Time Machine backup bundles unless you are deliberately investigating that path.

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
  - `mdns-advertiser`
  - `nbns-advertiser`
- renders and uploads the packaged boot/runtime files:
  - `smb.conf.template`
  - `rc.local`
  - `start-samba.sh`
  - `watchdog.sh`
  - `dfree.sh`
- generates and installs:
  - `private/smbpasswd`
  - `private/username.map`
- optionally enables:
  - `private/nbns.enabled` when `--install-nbns` is used
- applies the required permissions on files and directories
- reboots by default
- verifies Bonjour and authenticated SMB access after reboot in the normal path using the same shared checks used by `doctor`
- on NetBSD 4, deploy uploads the NetBSD 4 artifact set and immediately runs the activation sequence instead of rebooting

Current compatibility behavior:
- NetBSD 6 `evbarm` devices are accepted for the current `samba4` payload family
- NetBSD 4 `evbarm` devices are accepted as older hardware and use the `netbsd4_samba4` payload family
- `deploy --install-nbns` is rejected on NetBSD 4 because there is not enough RAM-disk space for the NBNS helper
- `configure` reuses the same classification logic to choose a better default Finder model hint

NetBSD 4 activation behavior:
- `tcapsule deploy` stops Apple SMB/mDNS, runs `/mnt/Flash/rc.local`, and verifies `smbd` on TCP `445` plus `mdns-advertiser` on UDP `5353`
- `tcapsule activate` repeats that activation sequence without re-uploading files
- tested 1st-generation NetBSD 4 hardware does not persist an `/etc` boot hook and therefore needs manual activation after reboot
- other NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet proven
- `activate` is intentionally idempotent: it stops the existing watchdog, stops Apple SMB/mDNS if present, and starts the packaged runtime again

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
.venv/bin/tcapsule activate --dry-run
.venv/bin/tcapsule activate
```

The dry-run modes are intended for users who want to inspect the exact remote actions before touching the box.

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
- [bin/samba4-netbsd4/smbd](bin/samba4-netbsd4/smbd)
- [bin/samba3-netbsd4/smbd](bin/samba3-netbsd4/smbd)
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)
- [bin/mdns-netbsd4/mdns-advertiser](bin/mdns-netbsd4/mdns-advertiser)
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)
- [bin/nbns-netbsd4/nbns-advertiser](bin/nbns-netbsd4/nbns-advertiser)

It assumes:
- a NetBSD VM
- root-owned cross-build tree under `/root`
- `su` for the actual build steps

Important note:
- the active supported build paths are NetBSD 7 for NetBSD 6-era devices and NetBSD 4 for older NetBSD 4-era devices
- NetBSD 10 was useful for early experiments but is not the supported Samba 4 build source path

Current validated maintainer flows:
- NetBSD 7 full path:
  - [build/download.sh](build/download.sh)
  - [build/bootstrap.sh](build/bootstrap.sh)
  - [build/downloadsamba4.sh](build/downloadsamba4.sh)
  - [build/samba4.sh](build/samba4.sh)
  - [build/mdns.sh](build/mdns.sh)
  - [build/nbns.sh](build/nbns.sh)
- NetBSD 4 path:
  - [build/downloadold.sh](build/downloadold.sh)
  - [build/bootstrapold.sh](build/bootstrapold.sh)
  - [build/helloold.sh](build/helloold.sh)
  - [build/downloadsamba3old.sh](build/downloadsamba3old.sh)
  - [build/samba3old.sh](build/samba3old.sh)
  - [build/downloadsamba4old.sh](build/downloadsamba4old.sh)
  - [build/samba4old.sh](build/samba4old.sh)
  - [build/mdnsold.sh](build/mdnsold.sh)
  - [build/nbnsold.sh](build/nbnsold.sh)

Current path split:
- NetBSD 7 SDK output defaults under `/root/tc-earmv4-netbsd7`
- NetBSD 4 SDK output defaults under `/root/tc-earmv4-netbsd4`
- NetBSD 7 staged runtime outputs default under `/root/tc-netbsd7`
- NetBSD 4 staged runtime outputs default under `/root/tc-netbsd4`

## Important Historical Findings

These are the findings that matter to future maintainers.

### The internal disk can be mounted locally

This was a major breakthrough. The Time Capsule can locally mount `/dev/dk2` with `mount_hfs` without needing a Mac to first trigger Apple sharing.

### Running `smbd` from the HDD is a bad idea

The HDD may be unmounted or slept by Apple later. That is why `smbd` is staged into RAM.

### Running the mDNS helper from the HDD would be less catastrophic, but we keep it off the HDD

If it died, discovery would break but file serving would remain up. The current runtime starts it from `/mnt/Flash` instead of the HDD or RAM disk, which saves RAM headroom and avoids depending on the HDD staying mounted.

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
- survives reboot on the NetBSD 6 path
- can be manually reactivated after reboot on tested NetBSD 4 gen1 hardware
- advertises itself over Bonjour
- authenticates as `admin`
- serves the internal disk through Samba 4.8
- supports Time Machine via `vfs_fruit`

The main remaining “nice to have” work is polish, not core functionality.
