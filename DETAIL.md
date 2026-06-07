# TimeCapsuleSMB Detail Reference

This file is the long-form engineering reference for the current system.

It is intentionally denser than [README.md](README.md). The README is the user-facing overview. This file is for maintainers, contributors, and users who want the actual constraints, rationale, and implementation details in one place before they start modifying the box or the tooling.

## Current Working State

The current system works end to end on the target Apple AirPort Time Capsule.

What is working now:
- static Samba 4.24.3 built from NetBSD 7 sources for NetBSD 6-era AirPort storage devices
- static Samba 4.24.3 built from NetBSD 4 sources for older NetBSD 4-era AirPort storage devices
- static tiny SMB / Time Machine mDNS advertiser
- static NBNS responder for NetBIOS name discovery
- boot-time runtime staging via `/mnt/Flash/rc.local`
- boot-time manager for `smbd`, the mDNS helper, and the NBNS helper when enabled
- direct SMB service on port `445`
- Bonjour advertisement for:
  - managed `_smb._tcp`
  - managed `_adisk._tcp`
  - generated `_device-info._tcp`
  - generated `_airport._tcp`
  - generated `_afpovertcp._tcp`
  - generated USB printer records when applicable
- authenticated SMB access using:
  - examples and docs use Samba username `admin`
  - generated auth stores a `root` Samba account
  - incoming SMB usernames are mapped to Unix `root`
  - password: the same password provided in `.env` as `TC_PASSWORD`
- guest access disabled
- deploy-time device compatibility detection
- manual NetBSD 4 activation via `tcapsule activate`
- manual disk repair via `tcapsule fsck`
- clean uninstall via `tcapsule uninstall`

Current validation status:
- NetBSD 6 is validated end to end with reboot-persistent startup
- tested NetBSD 4 gen1 hardware is validated with manual `tcapsule activate` after reboot
- other NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet confirmed

Current user experience:
- the Time Capsule advertises `_smb._tcp`
- the Time Capsule advertises `_adisk._tcp` for Time Machine
- the Time Capsule replays Apple `_airport._tcp` for AirPort Utility compatibility
- the Time Capsule can optionally answer NBNS name queries for the active runtime NetBIOS name
- the Bonjour instance name and Samba server string are derived from Apple `syNm`
- the Bonjour host label and Samba NetBIOS name are derived from `/bin/hostname`, with `syNm` fallbacks
- shares are derived from Apple `MaSt` volume metadata and are available as:
  - `smb://<advertised-host>.local/<volume name>`

Current auth model:
- the docs and examples use SMB login user `admin`
- `TC_PASSWORD` is reused as the SMB password
- generated Samba auth stores a `root` SMB account hash
- the username map currently maps incoming SMB usernames to Unix `root`
- filesystem access still runs as `root`
- this avoids the privilege-switch failures seen with non-root identities on this firmware

## Device Profile

The important target families are:

- NetBSD 6.x `evbarm`: 5th generation Time Capsules and same-era AirPort storage devices
- NetBSD 4.x `evbarm`: older little-endian AirPort storage devices
- NetBSD 4.x `armeb`: older big-endian AirPort storage devices
- AirPort Extreme devices with attached USB storage are supported by the same deploy/runtime model, but are less broadly validated than Time Capsule hardware

The details differ by generation, but the important shared constraints are:
- root fs is tiny
- flash is tiny
- `/mnt/Memory` is only about `16 MiB`
- the runtime has to fit in RAM while lock/cache databases can grow during client activity

Relevant mount points:
- `/` on `/dev/md0a`
- `/mnt/Flash` on `/dev/flash2a`
- `/mnt/Memory` on `tmpfs`
- internal HDD usually appears as `/dev/dk2` or `/dev/dk3`
- Apple’s expected mount point is `/Volumes/dk2` or `/Volumes/dk3`

Current live storage numbers observed during development:
- `/`: about `15.5 MiB` total, about `4.7 MiB` free
- `/mnt/Flash`: about `1 MiB` total, about `933 KiB` free
- `/mnt/Memory`: `16 MiB` total, with limited free headroom once Samba is staged
- `/Volumes/dk2`: effectively the large 2 TB data disk

These constraints drive almost every design decision in this repo.

Current compatibility classification in the repo is:
- NetBSD 6.x `evbarm`: current supported deploy target, corresponding to 5th generation Time Capsules
- NetBSD 4.x `evbarm`: supported as older 3rd-4th generation hardware, with a separate artifact set and activation path
- NetBSD 4.x `armeb`: supported as older 1st-2nd generation hardware, with a separate artifact set and activation path
  - tested gen1-4 hardware needs manual `activate` after reboot
  - other generations may auto-start if their firmware runs `/mnt/Flash/rc.local`, but that is not yet confirmed

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
  - `/Volumes/dkX/.samba4/smbd`
  - `/Volumes/dkX/.samba4/mdns-advertiser`
  - `/Volumes/dkX/.samba4/nbns-advertiser`
  - `/Volumes/dkX/.samba4/private/smbpasswd`
  - `/Volumes/dkX/.samba4/private/username.map`
  - `/Volumes/dkX/.samba4/private/xattr.tdb`
  - `/Volumes/dkX/.samba4/cache`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/common.sh`
  - `/mnt/Flash/boot.sh`
  - `/mnt/Flash/manager.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/mdns-advertiser`
  - `/mnt/Flash/tcapsulesmb.conf`
- transient runtime on RAM disk:
  - `/mnt/Memory/samba4`
  - `/mnt/Locks`

This gives:
- persistence on disk
- safe execution from RAM
- only tiny always-mounted files on flash

Current naming split:
- `.samba4` is the fixed managed persistent HDD payload directory
- the live RAM runtime path is intentionally fixed at `/mnt/Memory/samba4`
- share names are not configured locally; runtime sanitizes and de-duplicates the Apple `MaSt` partition names

## Why Samba 4.8, Then 4.24.3

The project did not land on Samba 4.x by accident. Samba 4.8 was the first fully working Time Machine target on this hardware; the current checked-in deploy artifacts are Samba 4.24.3.

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

In practice, 4.3 proved the architecture and deployment model, while 4.8 was the version that first enabled the full Time Machine-oriented share behavior.

### Samba 4.8

Samba 4.8 was the first stable target because it gave the project a usable Time Machine stack through `vfs_fruit`.

### Samba 4.24.3

Samba 4.24.3 is the current shipped target. It keeps the same static-module deployment model, but uses the newer `samba4x` build lanes and checked-in artifacts.

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

The first working result came from:
- NetBSD 7 source tree
- static `earmv4` build
- Samba 4.8.x

That combination:
- builds reproducibly
- executes correctly on the Time Capsule
- serves files successfully
- supports Time Machine semantics through `vfs_fruit`

The current deploy artifacts use Samba 4.24.3 on the same NetBSD 7 / NetBSD 4 SDK split.

The important build logic is now under [build/](build).

Current maintainer build lanes:
- NetBSD 7 SDK lane:
  - [build/download.sh](build/download.sh)
  - [build/bootstrap.sh](build/bootstrap.sh)
- NetBSD 4 SDK lane:
  - [build/downloadoldle.sh](build/downloadoldle.sh)
  - [build/bootstrapoldle.sh](build/bootstrapoldle.sh)
  - [build/downloadoldbe.sh](build/downloadoldbe.sh)
  - [build/bootstrapoldbe.sh](build/bootstrapoldbe.sh)
- NetBSD 7 current Samba 4.24 lane:
  - [build/downloadsamba4x.sh](build/downloadsamba4x.sh)
  - [build/samba4x.sh](build/samba4x.sh)
- NetBSD 4 current Samba 4.24 lanes:
  - [build/downloadsamba4xoldle.sh](build/downloadsamba4xoldle.sh)
  - [build/downloadsamba4xoldbe.sh](build/downloadsamba4xoldbe.sh)
  - [build/samba4xoldle.sh](build/samba4xoldle.sh)
  - [build/samba4xoldbe.sh](build/samba4xoldbe.sh)
- legacy Samba 4.8 lanes:
  - [build/downloadsamba4.sh](build/downloadsamba4.sh)
  - [build/samba4.sh](build/samba4.sh)
  - [build/downloadsamba4oldle.sh](build/downloadsamba4oldle.sh)
  - [build/downloadsamba4oldbe.sh](build/downloadsamba4oldbe.sh)
  - [build/samba4oldle.sh](build/samba4oldle.sh)
  - [build/samba4oldbe.sh](build/samba4oldbe.sh)
- NetBSD 7 utility lanes:
  - [build/hello.sh](build/hello.sh)
  - [build/mdns.sh](build/mdns.sh)
  - [build/nbns.sh](build/nbns.sh)
- NetBSD 4 utility lanes:
  - [build/hellooldle.sh](build/hellooldle.sh)
  - [build/hellooldbe.sh](build/hellooldbe.sh)
  - [build/mdnsoldle.sh](build/mdnsoldle.sh)
  - [build/mdnsoldbe.sh](build/mdnsoldbe.sh)
  - [build/nbnsoldle.sh](build/nbnsoldle.sh)
  - [build/nbnsoldbe.sh](build/nbnsoldbe.sh)

The direct scripts target the NetBSD 7 lane by default. The `*oldle.sh` and `*oldbe.sh` wrappers select the NetBSD 4 little-endian and big-endian lanes.

## Why We Generate Apple-Compatible mDNS And Override SMB / Time Machine

This was investigated deeply.

Apple’s stack does have a native SMB/mDNS path involving:
- `/etc/cifs/cm_cfg.txt`
- Apple disk metadata exposed through `acp MaSt`
- `wcifsfs`
- `mDNSResponder`
- `ACPd`

Important findings:
- Apple’s own `_smb._tcp` and `_adisk._tcp` paths are coupled to Apple’s file-sharing stack
- when Apple’s stack owns those paths, Finder tends to reconnect through Apple SMB/AFP rather than our Samba service
- Apple’s `_airport._tcp` is still valuable because AirPort Utility depends on it
- some Apple-advertised services such as USB printer advertisements should be preserved if present
- the current Samba runtime uses `MaSt` as the source of truth for volumes and ADISK UUIDs; it does not read `/etc/cifs/cs_cfg.txt`

So the current system does not hand control back to Apple mDNS for SMB and Time Machine, but it also does not discard the Apple device identity users expect. Instead it uses a separate tiny helper:
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

This helper:
- derives Apple-compatible `_airport._tcp` fields from local AirPort identity values
- can advertise USB printer services when a local AirPort printer identity is present
- advertises managed records for:
  - `_smb._tcp.local.`
  - `_adisk._tcp.local.`
- can suppress managed SMB/ADISK records in diskless mode while keeping generated AirPort identity records
- aggressively terminates Apple `mDNSResponder` during takeover and binds UDP `5353`
- continues to point clients at our `smbd` on port `445`

Current practical result:
- Our `_smb._tcp` and `_adisk._tcp` remain authoritative
- Apple `_airport._tcp` identity can still be advertised for AirPort Utility
- attached USB printer advertisements can be generated from local AirPort printer metadata

## Bonjour Discovery Boundaries

Local Bonjour discovery is intentionally service-centric. `timecapsulesmb.discovery.bonjour.discover()` returns one normalized record per service instance, not one merged record per physical device.

That distinction matters:
- `_airport._tcp.local.` is the Apple device identity and is the only service configure uses for the interactive device list
- `_smb._tcp.local.` is the managed Samba service identity and is what doctor/deploy Bonjour checks use
- `_device-info._tcp.local.` may share the same name, hostname, and IP as `_smb._tcp.local.`, but it must remain a separate raw record

Do not merge `_airport`, `_smb`, and `_device-info` records inside `bonjour.discover()`. Merging service records creates ambiguous objects with one name/hostname but multiple meanings, and it causes duplicate-looking or misleading configure/doctor output. The stored `service_type` should remain the raw observed value. Callers should filter raw discovery results by the service prefix they actually need, such as `_airport` for configure and `_smb` for doctor/deploy. Prefix filtering intentionally matches both `_smb._tcp.local.` and `_smb._tcp.local`.

## Generated mDNS Records

Current behavior:
- `boot.sh` prepares the RAM runtime and launches `manager.sh`
- `manager.sh` waits for usable network addresses, payload state, and AirPort identity data
- the manager launches `mdns-advertiser` from `/mnt/Flash` with `--generated-airport-services`
- `mdns-advertiser` generates managed `_smb._tcp`, `_adisk._tcp`, `_device-info._tcp`, and `_airport._tcp` records from live runtime state
- when a USB printer is attached and discoverable through AirPort metadata, the manager also passes `_riousbprint._tcp` and `_pdl-datastream._tcp` arguments
- if the disk payload is unavailable, the manager can launch the advertiser in diskless mode so AirPort identity remains visible while SMB/ADISK records are suppressed
- `mdns-advertiser` kills Apple `mDNSResponder` during takeover and keeps UDP `5353` owned by the managed helper

The old snapshot files `/mnt/Flash/allmdns.txt` and `/mnt/Flash/applemdns.txt` are not part of the active runtime path. Deploy and uninstall deliberately leave those files alone if they exist from older experiments because they are diagnostic artifacts, not current managed state.

## Boot Flow In Detail

The boot logic lives in:
- [src/timecapsulesmb/assets/boot/samba4/rc.local](src/timecapsulesmb/assets/boot/samba4/rc.local)
- [src/timecapsulesmb/assets/boot/samba4/boot.sh](src/timecapsulesmb/assets/boot/samba4/boot.sh)
- [src/timecapsulesmb/assets/boot/samba4/manager.sh](src/timecapsulesmb/assets/boot/samba4/manager.sh)
- [src/timecapsulesmb/assets/boot/samba4/common.d/](src/timecapsulesmb/assets/boot/samba4/common.d)

### `rc.local`

`rc.local` is intentionally tiny. It just backgrounds `boot.sh`.

This matters because:
- boot ordering is messy
- the HDD device nodes may not exist yet when `rc.local` first runs
- a longer wait loop belongs in the second-stage script, not directly inline in the boot hook

### `boot.sh`

`boot.sh` performs the one-shot startup preparation:

1. sources `/mnt/Flash/common.sh` and `/mnt/Flash/tcapsulesmb.conf`
2. kills any prior managed `smbd`, mDNS advertiser, NBNS responder, and manager
3. prepares the dedicated Samba lock ramdisk at `/mnt/Locks`
4. recreates the RAM runtime tree under `/mnt/Memory/samba4`
5. prepares compatibility symlinks under `/root`
6. starts `manager.sh` if it is not already running

The manager owns disk discovery, Samba staging, service startup, mDNS takeover, NBNS startup, and later recovery. This keeps boot short and puts all recurring runtime reconciliation in one process.

The boot log is written to:
- `/mnt/Memory/samba4/var/rc.local.log`

Long-running process logs are written under:
- `<payload>/logs/manager.log`
- `<payload>/logs/mdns.log`
- `<payload>/logs/nbns.log`
- `<payload>/logs/log.smbd`

Important bug lessons from getting this stable:
- the script cannot assume `/dev/dk2` exists immediately
- AirPort Extreme devices may have no internal disk at all
- the script must use `-b` for block devices, not `-c`
- it cannot call non-existent utilities like `dirname`
- it must tolerate a long delay before the disk appears
- the Samba lock TDBs need their own ramdisk because `/mnt/Memory` is too small for the runtime plus growing lock databases
- on NetBSD 4, cache state is kept on the HDD instead of `/mnt/Memory` to preserve RAM-disk headroom
- the persistent `xattr.tdb` must stay in the selected payload home so all shares use a single private database

### `/mnt/Locks`

Samba lock state now lives on a dedicated second ramdisk:
- `lock directory = /mnt/Locks`

Current mount behavior:
- NetBSD 6 mounts a `9 MiB` `tmpfs` at `/mnt/Locks` with `mount_tmpfs -s 9m`
- NetBSD 4 mounts an `mfs` ramdisk at `/mnt/Locks` with `mount_mfs -s 18432`
- if the NetBSD 6 tmpfs mount fails, startup falls back to a plain `/mnt/Locks` directory on the root filesystem
- if the NetBSD 4 mfs mount fails, startup aborts instead of falling back to the tiny root filesystem

Operational behavior:
- `boot.sh` clears `/mnt/Locks/*` during startup preparation
- `manager.sh` clears `/mnt/Locks/*` before restarting `smbd`

### `manager.sh`

`manager.sh` is the long-running supervisor launched at boot from flash.

Current behavior:
- runs a disk/topology pass every `10` seconds
- runs a Samba bind pass every `10` seconds by default
- runs a full managed service pass every `30` seconds
- retries failed recovery work on the next due pass
- reads `MaSt` directly through the shared runtime helpers
- debounces disk topology changes before applying runtime updates
- requests `diskd.useVolume` for valid `MaSt` volumes and builds current share/ADISK state from mounted volumes
- applies share path rules:
  - external volumes always share `/Volumes/dkN`
  - internal volumes share `/Volumes/dkN/ShareRoot` unless `INTERNAL_SHARE_USE_DISK_ROOT=1`
  - internal `ShareRoot` is created when needed
- resolves the persistent payload by scanning mounted `MaSt` volumes in internal-first order for `.samba4`
- writes current `adisk.tsv` under `/mnt/Memory/samba4/var`
- copies `smbd`, auth files, and optional `nbns-advertiser` into RAM when inputs change
- generates `/mnt/Memory/samba4/etc/smb.conf` directly from runtime state
- starts or reloads `smbd` as needed and keeps it bound to the current interfaces
- starts generated mDNS advertisement from `/mnt/Flash/mdns-advertiser`
- starts NBNS when `NBNS_ENABLED=1`
- if the payload volume is unavailable, stops managed Samba/mDNS/NBNS and retries later
- if disk, identity, network, or USB printer state changes, refreshes the affected generated config and service state

This is intentionally simple:
- SMB transfers are not interrupted because `smbd` is only restarted when absent
- the mDNS helper is also only restarted when absent
- disk topology changes restart through the same path as boot, so share generation, mDNS, and smbd config stay coherent

The manager log is written to:
- `<payload>/logs/manager.log` when the payload volume is mounted
- `/mnt/Memory/samba4/var/manager.log` as a RAM fallback while the payload volume is unavailable

Important implementation detail:
- `mdns-advertiser` is short enough to match directly with `pkill`
- the manager therefore uses the truncated process name for liveness checks and restarts

NetBSD 4-specific shell note:
- backgrounded jobs redirect stdin from `/dev/null` so they do not hold the SSH session open during manual activation

## SMB Runtime Layout

When boot succeeds, the runtime tree under `/mnt/Memory/samba4` contains:
- `sbin/smbd`
- optionally `sbin/nbns-advertiser`
- `etc/smb.conf`
- `var/`
- `locks/`
- `private/`

Current persistent auth files live in the selected payload home:
- `/Volumes/dkX/.samba4/private/smbpasswd`
- `/Volumes/dkX/.samba4/private/username.map`

Current NBNS binary also lives in the selected payload home:
- `/Volumes/dkX/.samba4/nbns-advertiser`

NBNS runtime enablement lives in flash config:
- `/mnt/Flash/tcapsulesmb.conf`
- `NBNS_ENABLED=0|1`

Current persistent Time Machine metadata state also lives in the selected payload home:
- `/Volumes/dkX/.samba4/private/xattr.tdb`

Current NetBSD 4 Samba cache state lives on the HDD to preserve RAM headroom:
- `/Volumes/dkX/.samba4/cache`

NetBSD 6 note:
- the normal NetBSD 6 runtime keeps Samba cache state in `/mnt/Memory/samba4/var`
- the HDD cache path above is used for the NetBSD 4 payload family because the NetBSD 4 RAM disk is too tight for the full runtime plus cache TDB growth

Current rendered Samba config characteristics:
- `netbios name = <runtime hostname-derived name>`
- `server string = <runtime Apple syNm-derived name>`
- `security = user`
- `min protocol = SMB2`
- `max protocol = SMB3`
- `guest ok = no`
- `valid users = root`
- `force user = root`
- `force group = wheel`
- `reset on zero vc = yes`
- share paths are generated from `MaSt`
- internal default: `path = /Volumes/dkN/ShareRoot`
- external default: `path = /Volumes/dkN`
- `pid directory = /mnt/Memory/samba4/var`
- `lock directory = /mnt/Locks`
- `state directory = /mnt/Memory/samba4/var`
- `cache directory = /mnt/Memory/samba4/var` on NetBSD 6
- `cache directory = /Volumes/dkX/.samba4/cache` on NetBSD 4
- `private dir = /mnt/Memory/samba4/private`
- `log file = /Volumes/dkX/.samba4/logs/log.smbd`
- `max log size = 128` in the normal generated config
- `deadtime = 60`
- `vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb`
- `fruit:resource = file`
- `fruit:veto_appledouble = yes`
- `fruit:metadata = stream`
- `fruit:time machine = yes`
- `fruit:posix_rename = yes`
- `acl_xattr:ignore system acls = yes`
- `xattr_tdb:file = /Volumes/dkX/.samba4/private/xattr.tdb`
- `veto files = /.samba4/` on every share so the payload is hidden when it lives on a shared disk root

Current auth mapping:
- the docs and examples use `admin` as the normal user-facing SMB login name
- the `smbpasswd` backend contains a `root` entry with the configured password hash
- `username.map` contains:
  - `!root = root`
  - `root = *`
- incoming SMB usernames are mapped to Unix `root`

This is intentionally pragmatic:
- login is authenticated
- the filesystem still runs as `root`
- it avoids the earlier non-root privilege-switch failures on this firmware

Operational note:
- the live runtime config at `/mnt/Memory/samba4/etc/smb.conf` is regenerated on each boot
- `/mnt/Memory` is a RAM disk, so live edits there are ephemeral
- temporary debug edits such as one-off `log level = ...` lines will disappear after reboot
- manager logs under `/mnt/Memory/samba4/var` are also ephemeral for the same reason

## mDNS Advertiser Details

The mDNS helper is:
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

It is built from:
- [build/mdns-advertiser.c](build/mdns-advertiser.c)
- [build/mdns.sh](build/mdns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 little-endian `earmv4` binary for the NetBSD 4 little-endian payload
- static NetBSD 4 big-endian `armeb` binary for the NetBSD 4 big-endian payload
- see the artifact section below for current checked-in binary sizes
- installed on both the HDD payload and `/mnt/Flash`
- run from `/mnt/Flash` to save RAM-disk space

At runtime it can:
- advertise managed `_smb._tcp.local.`
- advertise managed `_adisk._tcp.local.`
- advertise managed `_device-info._tcp.local.`
- advertise generated `_afpovertcp._tcp.local.` on port `548`
- advertise generated `_airport._tcp.local.` records from local AirPort identity fields
- optionally advertise `_riousbprint._tcp.local.` and `_pdl-datastream._tcp.local.` for an attached USB printer
- suppress SMB/ADISK records in diskless mode while preserving generated AirPort identity records
- aggressively take over UDP `5353` from Apple `mDNSResponder`
- track runtime interface changes in auto-IP mode

Current validation and behavior notes:
- mDNS host labels are validated as DNS-label-safe host labels
- mDNS instance names may contain spaces and are validated separately from host labels
- service types are validated as dotted DNS names
- `_adisk._tcp` TXT payload sizing is validated before advertisement
- `_airport._tcp` fields are all optional; missing fields are simply omitted from the TXT payload
- snapshot replay preserves non-ASCII or binary hostnames using `HOST_HEX`
- managed `_device-info._tcp` is generated even in snapshot mode; snapshot `_device-info._tcp` records are ignored

## NBNS Responder Details

The NBNS helper is:
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)

It is built from:
- [build/nbns-advertiser.c](build/nbns-advertiser.c)
- [build/nbns.sh](build/nbns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 little-endian `earmv4` binary for the NetBSD 4 little-endian payload
- static NetBSD 4 big-endian `armeb` binary for the NetBSD 4 big-endian payload
- enabled by default at runtime
- always deployed to the HDD payload, but only staged into RAM when enabled

Current behavior:
- binds UDP port `137`
- answers NBNS name queries for the active runtime NetBIOS name
- replies for both NetBIOS suffixes:
  - `0x00`
  - `0x20`
- returns the current runtime IPv4 selected from the device interfaces

Enablement model:
- the binary is uploaded to `/Volumes/dkX/.samba4/nbns-advertiser` on every deploy
- runtime enablement is controlled by:
  - `NBNS_ENABLED=1` in `/mnt/Flash/tcapsulesmb.conf`
- plain `tcapsule deploy` writes that flash config value
- `--no-nbns` writes `NBNS_ENABLED=0`
- `--no-nbns` is supported on both NetBSD 6 and NetBSD 4
- `uninstall` removes both the binary and flash runtime config

## Current User-Facing Workflow

The intended user flow is:

1. bootstrap the local host
   - [`./tcapsule bootstrap`](./tcapsule)
2. generate local config and enable SSH when needed
   - [src/timecapsulesmb/cli/configure.py](src/timecapsulesmb/cli/configure.py)
3. deploy and reboot
   - [src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py)
4. activate older NetBSD 4 devices if they do not auto-start Samba after reboot
   - [src/timecapsulesmb/cli/activate.py](src/timecapsulesmb/cli/activate.py)
5. run local diagnostics
   - [src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py)
6. optionally repair the HDD before redeploying
   - [src/timecapsulesmb/cli/fsck.py](src/timecapsulesmb/cli/fsck.py)
7. remove the payload later if needed
   - [src/timecapsulesmb/cli/uninstall.py](src/timecapsulesmb/cli/uninstall.py)

`tcapsule set-ssh` still exists as an advanced SSH toggle helper, but it is no longer part of the normal setup flow.

`tcapsule configure` writes repo-root `.env`.

Current important `.env` values include:
- `TC_HOST`
- `TC_PASSWORD`
- `TC_SSH_OPTS`
- `TC_INTERNAL_SHARE_USE_DISK_ROOT`
- `TC_ATA_IDLE_SECONDS`
- `TC_ATA_STANDBY`
- `TC_CONFIGURE_ID`

Current `.bootstrap` values include:
- `INSTALL_ID`
- optional `TELEMETRY=false`

## Local Test Coverage

Fresh clones install `coverage.py` through `requirements.txt` during `./tcapsule bootstrap` or `make install`.

Coverage entry points:
- `make test` runs C compile checks plus the pytest suite
- `make coverage` runs the pytest suite with branch coverage and prints missing source lines
- `make coverage-html` writes the browsable report to `htmlcov/index.html`

Optional deploy flag:
- `--no-nbns`
  - disables the bundled NBNS responder on the next boot by writing `NBNS_ENABLED=0` to `/mnt/Flash/tcapsulesmb.conf`

Current defaults:
- `TC_INTERNAL_SHARE_USE_DISK_ROOT=false`
- `TC_ATA_IDLE_SECONDS=300`
- `TC_ATA_STANDBY=` leaves the standby timer unchanged; set `0` to disable standby
- `TC_SSH_OPTS` includes the legacy SSH algorithms required by AirPort firmware
- docs and examples use SMB username `admin`
- the managed payload directory is fixed at `.samba4`

Samba NetBIOS, Samba server string, Bonjour instance, and Bonjour host labels are derived on the device at runtime from `/usr/bin/acp -q syNm` and `/bin/hostname`; they are not configured in `.env`.

Current validation behavior:
- `TC_HOST`: must be non-empty.
- `TC_PASSWORD`: must be present for commands that authenticate to the device or generate Samba auth.
- `TC_SSH_OPTS`: is written by `configure` with the legacy SSH options needed for AirPort firmware.
- `TC_INTERNAL_SHARE_USE_DISK_ROOT`: hidden boolean; internal disks use `ShareRoot` by default, and external disks always use the disk root.
- `TC_ATA_IDLE_SECONDS`: optional non-negative integer; default `300`, and `0` disables the ATA idle timer through `atactl setidle 0`.
- `TC_ATA_STANDBY`: optional non-negative integer; blank leaves standby unchanged, and `0` disables standby through `atactl setstandby 0`.
- `TC_CONFIGURE_ID`: is a local configuration revision ID and is not user-validated.

Workflow details:
- `configure` now starts by attempting mDNS discovery of the Time Capsule on the local network
- if SSH is already reachable, `configure` validates the SSH target/password and then probes the device directly
- if SSH is closed, `configure` enables SSH with the built-in Python 3 ACP client, reboots the device through ACP, waits for SSH to come back, and then probes the device directly
- ACP authentication failures during `configure` reprompt for the Time Capsule password; non-authentication ACP failures stop configuration with the underlying error
- `configure` uses discovered and probed Apple identity metadata to classify compatibility and present device details, but it does not persist model or `syAP` hints in managed `.env`
- for NetBSD 4 devices, the probe/compatibility layer uses endianness and on-device `acp` identity data to classify the exact generation when possible
- `configure` validates managed `.env` inputs before writing `.env`
- `deploy`, `activate`, and `doctor` fail early when managed `.env` config values are invalid
- the command entrypoints live under [src/timecapsulesmb/cli/](src/timecapsulesmb/cli)
- the deploy/runtime logic lives under [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy) and [src/timecapsulesmb/device/](src/timecapsulesmb/device)
- the checked-in binaries and build tooling are visible in the repo, so advanced users can swap binaries, rebuild artifacts, or trace the exact boot/runtime layout

## Host-Side Architecture

Current important package areas:
- [src/timecapsulesmb/cli/](src/timecapsulesmb/cli): command entrypoints for `bootstrap`, `paths`, `validate-install`, `discover`, `configure`, `set-ssh`, `deploy`, `activate`, `doctor`, `fsck`, `repair-xattrs`, `uninstall`, and the app-facing `api` helper
- [src/timecapsulesmb/core/](src/timecapsulesmb/core): shared config parsing, defaults, and common models
- [src/timecapsulesmb/transport/](src/timecapsulesmb/transport): local command execution plus SSH and SCP helpers
- [src/timecapsulesmb/discovery/](src/timecapsulesmb/discovery): Bonjour-based device discovery
- [src/timecapsulesmb/integrations/](src/timecapsulesmb/integrations): self-contained Python 3 ACP client for SSH enable/reboot support
- [src/timecapsulesmb/checks/](src/timecapsulesmb/checks): reusable local, network, Bonjour, and SMB verification checks
- [src/timecapsulesmb/device/](src/timecapsulesmb/device): remote probing for device-specific layout, `MaSt` volume parsing, payload-home selection, plus generation / compatibility classification
- [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy): auth generation, deployment planning, flash config generation, execution, dry-run formatting, artifact resolution, and post-deploy verification
- [src/timecapsulesmb/assets/](src/timecapsulesmb/assets): packaged boot templates and artifact metadata
- [src/timecapsulesmb/identity.py](src/timecapsulesmb/identity.py): local install identity loaded from `.bootstrap`
- [src/timecapsulesmb/telemetry/](src/timecapsulesmb/telemetry): best-effort client telemetry for user-facing commands
- [build/](build): maintainer build tooling, including Samba cross-exec record/replay helpers

Developer note:
- [src/timecapsulesmb/cli/context.py](src/timecapsulesmb/cli/context.py) owns shared per-command lifecycle state such as timing, command IDs, result state, and finish handling.
- [src/timecapsulesmb/cli/runtime.py](src/timecapsulesmb/cli/runtime.py) owns shared runtime helpers for `.env` loading, SSH connection resolution, validation entrypoints, and compatibility probing.
- Normal users should not need these details; they mostly keep command entrypoints smaller and more consistent.

Practical consequence:
- if you want to modify how the box is discovered, start in `discovery/`
- if you want to change what gets uploaded, start in `deploy/planner.py`, `deploy/executor.py`, and `cli/deploy.py`
- if you want to change the on-device boot behavior, inspect the packaged boot assets and the runtime layout sections below
- if you want to replace binaries or rebuild them, inspect the artifact manifest plus the `build/` tree

## Doctor Command

[src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py) is a non-destructive local diagnostic helper.

It checks:
- `.env` completeness and invalid `.env` values
- required local tools
- whether the required checked-in binaries exist and match the expected checksums
- deployed release/version metadata in `/mnt/Flash/tcapsulesmb.conf`
- SSH reachability
- remote network/interface problems
- advertised Bonjour instance name
- advertised Bonjour host label
- active Samba NetBIOS name
- active Samba share names
- SMB reachability
- `_smb._tcp` browse and resolve
- NBNS name resolution unless `/mnt/Flash/tcapsulesmb.conf` has `NBNS_ENABLED=0`
- authenticated `smbclient -L` listing
- authenticated SMB CRUD operations via `smbclient`
- that at least one active Samba share is present in the authenticated SMB listing
- that the active runtime `xattr_tdb:file` path in `/mnt/Memory/samba4/etc/smb.conf` points at persistent storage instead of the ramdisk

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

Current doctor caveats:
- for SSH-proxied targets, `doctor` now creates a temporary local SMB tunnel and runs the authenticated SMB checks through that forwarded port
- the xattr persistence check inspects the active runtime config under `/mnt/Memory/samba4`, not the persistent template on disk

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
.venv/bin/tcapsule repair-xattrs --path /Volumes/<share-name>
```

When exactly one matching `smbfs` mount is visible locally, `--path` can usually be omitted. The command reads the local `mount` table and matches mounted SMB volumes to the configured `TC_HOST`. If more than one candidate is mounted, pass `--path` explicitly:

```bash
.venv/bin/tcapsule repair-xattrs
```

Useful modes:

```bash
.venv/bin/tcapsule repair-xattrs --path /Volumes/<share-name> --dry-run
.venv/bin/tcapsule repair-xattrs --path /Volumes/<share-name> --yes
.venv/bin/tcapsule repair-xattrs --path /Volumes/<share-name>/some-folder --no-recursive
.venv/bin/tcapsule repair-xattrs --path /Volumes/<share-name> --max-depth 2
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
- validates the managed config before touching the device
- validates the required binary artifacts against the artifact manifest
- probes device compatibility and rejects unsupported targets before upload
- reads Apple `MaSt` disk metadata from the device
- selects exactly one writable persistent payload home:
  - first writable internal `builtin=true` HFS volume
  - else first writable external HFS volume
  - else fails with `no writable persistent volume found`
- computes the device-specific runtime and payload paths from that payload home
- builds a deployment plan before execution
- creates the persistent payload dir under `/Volumes/dkX/.samba4`
- uploads the checked-in binaries:
  - `smbd`
  - `mdns-advertiser`
  - `nbns-advertiser`
- renders and uploads the packaged boot/runtime files:
  - `rc.local`
  - `common.sh`
  - `boot.sh`
  - `manager.sh`
  - `dfree.sh`
- generates and uploads flash runtime config:
  - `/mnt/Flash/tcapsulesmb.conf`
- generates and installs:
  - `private/smbpasswd`
  - `private/username.map`
- enables NBNS by default:
  - `NBNS_ENABLED=1` in flash config unless `--no-nbns` is used
- applies the required permissions on files and directories
- reboots by default
- if the reboot confirmation is rejected, deploy intentionally stops after upload without activating the runtime so the device can be inspected before a later manual reboot
- verifies managed runtime readiness after reboot:
  - managed `smbd` on TCP `445`
  - managed mDNS takeover on UDP `5353`
- on NetBSD 4, deploy uploads the NetBSD 4 artifact set, reboots to clear RAM runtime state, waits for SSH to return, and then runs `/mnt/Flash/rc.local`

Full Bonjour browse/resolve checks, authenticated SMB listings, SMB CRUD checks, share checks, NBNS checks, xattr persistence checks, and deployed-version checks are handled by `doctor`.

Current compatibility behavior:
- NetBSD 6 `evbarm` devices are accepted for the current `samba4` payload family
- NetBSD 4 `evbarm` devices are accepted as older hardware and use either the `netbsd4le_samba4` or `netbsd4be_samba4` payload family
- `configure` reuses the same classification logic for compatibility and displayed device identity

NetBSD 4 activation behavior:
- `tcapsule deploy` uploads the NetBSD 4 payload, reboots, waits for SSH, watches for an already-running `/mnt/Flash/rc.local`, `/mnt/Flash/boot.sh`, or `/mnt/Flash/manager.sh`, runs `/mnt/Flash/rc.local` only if startup is not already in progress, and verifies managed `smbd` plus mDNS takeover
- `tcapsule deploy --no-reboot` uploads the payload, stops the manager plus any legacy watchdog process and `wcifsfs`, runs `/mnt/Flash/rc.local`, and verifies managed `smbd` plus mDNS takeover on both NetBSD 4 and NetBSD 6 devices
- `tcapsule activate` repeats the no-reboot activation sequence without re-uploading files
- Apple `mDNSResponder` takeover is handled inside `mdns-advertiser` during normal generated-advertisement startup
- tested 1st-generation NetBSD 4 hardware does not persist an `/etc` boot hook and therefore needs manual activation after reboot
- other NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet proven
- `activate` is intentionally conservative: if `smbd` already owns TCP `445` and `mdns-advertiser` already owns UDP `5353`, or if `/mnt/Flash/rc.local`, `/mnt/Flash/boot.sh`, or `/mnt/Flash/manager.sh` is already running, it skips running `/mnt/Flash/rc.local`

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

Hidden operator mode:
- `tcapsule deploy --debug-logging` writes `SMBD_DEBUG_LOGGING=1` and `MDNS_DEBUG_LOGGING=1` to flash config.
- at runtime, Samba writes `log.smbd` under `<payload>/logs/`, sets `max log size = 0`, and enables `log level = 5 vfs:8 fruit:8`.
- managed runtime logs under `<payload>/logs/` are normally capped around `128 KiB`; `--debug-logging` leaves them unbounded.
- this flag is intentionally not documented in the normal command help because it is for active debugging, not normal installs.

## Client Telemetry

Client telemetry is now emitted by:
- `tcapsule api`
- `tcapsule bootstrap`
- `tcapsule paths`
- `tcapsule validate-install`
- `tcapsule discover`
- `tcapsule configure`
- `tcapsule set-ssh`
- `tcapsule deploy`
- `tcapsule flash`
- `tcapsule activate`
- `tcapsule doctor`
- `tcapsule fsck`
- `tcapsule repair-xattrs`
- `tcapsule uninstall`

Current event model:
- app helper operations emit operation-specific app events through the `api` command
- `bootstrap_started`
- `bootstrap_finished`
- `paths_started`
- `paths_finished`
- `validate_install_started`
- `validate_install_finished`
- `discover_started`
- `discover_finished`
- `configure_started`
- `configure_finished`
- `set_ssh_started`
- `set_ssh_finished`
- `deploy_started`
- `deploy_finished`
- `flash_started`
- `flash_finished`
- `activate_started`
- `activate_finished`
- `doctor_started`
- `doctor_finished`
- `fsck_started`
- `fsck_finished`
- `repair_xattrs_started`
- `repair_xattrs_finished`
- `uninstall_started`
- `uninstall_finished`

Current identity model:
- `.bootstrap` stores a stable local `INSTALL_ID`
- `.env` stores a rotating `TC_CONFIGURE_ID`

Current transport behavior:
- events are sent to the configured HTTPS telemetry endpoint
- started events are sent asynchronously
- finished events are sent synchronously so they are not lost at process exit
- if `.bootstrap` contains `TELEMETRY=false`, telemetry is disabled

## Uninstall

Current uninstall behavior:
- stops the manager first so it cannot restart `smbd` during teardown
- removes the managed payload, flash hooks, runtime tree, and compatibility symlinks
- runs remote uninstall actions sequentially over SSH
- prompts before reboot by default
- supports `--no-reboot`

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
- [bin/samba4-netbsd4le/smbd](bin/samba4-netbsd4le/smbd)
- [bin/samba4-netbsd4be/smbd](bin/samba4-netbsd4be/smbd)
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)
- [bin/mdns-netbsd4le/mdns-advertiser](bin/mdns-netbsd4le/mdns-advertiser)
- [bin/mdns-netbsd4be/mdns-advertiser](bin/mdns-netbsd4be/mdns-advertiser)
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)
- [bin/nbns-netbsd4le/nbns-advertiser](bin/nbns-netbsd4le/nbns-advertiser)
- [bin/nbns-netbsd4be/nbns-advertiser](bin/nbns-netbsd4be/nbns-advertiser)

Current active deploy artifact sizes:
- NetBSD 6 `smbd`: about `9.7M`
- NetBSD 6 `mdns-advertiser`: about `310K`
- NetBSD 6 `nbns-advertiser`: about `210K`
- NetBSD 4 little-endian `smbd`: about `9.7M`
- NetBSD 4 big-endian `smbd`: about `9.7M`
- NetBSD 4 little-endian `mdns-advertiser`: about `255K`
- NetBSD 4 big-endian `mdns-advertiser`: about `253K`
- NetBSD 4 little-endian `nbns-advertiser`: about `155K`
- NetBSD 4 big-endian `nbns-advertiser`: about `155K`

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
  - [build/downloadsamba4x.sh](build/downloadsamba4x.sh)
  - [build/samba4x.sh](build/samba4x.sh)
  - [build/mdns.sh](build/mdns.sh)
  - [build/nbns.sh](build/nbns.sh)
- NetBSD 4 path:
  - [build/downloadoldle.sh](build/downloadoldle.sh)
  - [build/bootstrapoldle.sh](build/bootstrapoldle.sh)
  - [build/downloadoldbe.sh](build/downloadoldbe.sh)
  - [build/bootstrapoldbe.sh](build/bootstrapoldbe.sh)
  - [build/hellooldle.sh](build/hellooldle.sh)
  - [build/hellooldbe.sh](build/hellooldbe.sh)
  - [build/downloadsamba4xoldle.sh](build/downloadsamba4xoldle.sh)
  - [build/downloadsamba4xoldbe.sh](build/downloadsamba4xoldbe.sh)
  - [build/samba4xoldle.sh](build/samba4xoldle.sh)
  - [build/samba4xoldbe.sh](build/samba4xoldbe.sh)
  - [build/mdnsoldle.sh](build/mdnsoldle.sh)
  - [build/mdnsoldbe.sh](build/mdnsoldbe.sh)
  - [build/nbnsoldle.sh](build/nbnsoldle.sh)
  - [build/nbnsoldbe.sh](build/nbnsoldbe.sh)

Current path split:
- NetBSD 7 SDK output defaults under `/root/tc-earmv4-netbsd7`
- NetBSD 4 little-endian SDK output defaults under `/root/tc-earmv4-netbsd4`
- NetBSD 4 big-endian SDK output defaults under `/root/tc-armeb-netbsd4`
- NetBSD 7 staged runtime outputs default under `/root/tc-netbsd7`
- NetBSD 4 little-endian staged runtime outputs default under `/root/tc-netbsd4le`
- NetBSD 4 big-endian staged runtime outputs default under `/root/tc-netbsd4be`

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
- The repo still assumes AirPort storage firmware behavior such as:
  - AirPort-style IPv4/interface layout
  - `dk1` / `dk2` / `dk3`
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
dns-sd -L "<advertised-instance-name>" _smb._tcp local.
```

List shares as authenticated user:

```bash
smbutil view //admin:<password>@<configured-or-advertised-host>
```

Mount the share:

```bash
mount_smbfs //admin:<password>@<configured-or-advertised-host>/<share-name> /tmp/tc-auth-mount
```

Current expected result:
- `IPC$`
- at least one `MaSt`-derived share name

Expected negative test:

```bash
smbutil view //guest:@<configured-or-advertised-host>
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
- authenticates with the configured password; docs and examples use SMB username `admin`
- serves the internal disk through Samba 4.24.3
- supports Time Machine via `vfs_fruit`

The main remaining “nice to have” work is polish, not core functionality.
