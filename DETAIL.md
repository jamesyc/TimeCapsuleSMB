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
  - managed `_smb._tcp`
  - managed `_adisk._tcp`
  - Apple-cloned `_airport._tcp`
  - Apple-cloned `_afpovertcp._tcp`
  - other Apple-cloned records
- authenticated SMB access using:
  - Samba username: `admin`
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
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/smbd`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/mdns-advertiser`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/nbns-advertiser`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/smb.conf.template`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/private/smbpasswd`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/private/username.map`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/private/nbns.enabled`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/private/xattr.tdb`
  - `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>/cache`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/common.sh`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/watchdog.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/mdns-advertiser`
  - `/mnt/Flash/allmdns.txt`
  - `/mnt/Flash/applemdns.txt`
- transient runtime on RAM disk:
  - `/mnt/Memory/samba4`
  - `/mnt/Locks`

This gives:
- persistence on disk
- safe execution from RAM
- only tiny always-mounted files on flash

Current naming split:
- `TC_PAYLOAD_DIR_NAME` controls the persistent HDD payload directory
- the live RAM runtime path is intentionally fixed at `/mnt/Memory/samba4`

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
  - [build/downloadoldle.sh](build/downloadoldle.sh)
  - [build/bootstrapoldle.sh](build/bootstrapoldle.sh)
- NetBSD 7 Samba 4 lane:
  - [build/downloadsamba4.sh](build/downloadsamba4.sh)
  - [build/samba4.sh](build/samba4.sh)
- NetBSD 4 Samba 4 lane:
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
- NetBSD 4 Samba 3 exploratory lane:
  - [build/downloadsamba3oldle.sh](build/downloadsamba3oldle.sh)
  - [build/downloadsamba3oldbe.sh](build/downloadsamba3oldbe.sh)
  - [build/samba3oldle.sh](build/samba3oldle.sh)
  - [build/samba3oldbe.sh](build/samba3oldbe.sh)

The direct scripts target the NetBSD 7 lane by default. The `*oldle.sh` and `*oldbe.sh` wrappers select the NetBSD 4 little-endian and big-endian lanes.

## Why We Snapshot Apple’s mDNS And Override Only SMB / Time Machine

This was investigated deeply.

Apple’s stack does have a native SMB/mDNS path involving:
- `/etc/cifs/cm_cfg.txt`
- `/etc/cifs/cs_cfg.txt`
- `wcifsfs`
- `mDNSResponder`
- `ACPd`

Important findings:
- Apple’s own `_smb._tcp` and `_adisk._tcp` paths are coupled to Apple’s file-sharing stack
- when Apple’s stack owns those paths, Finder tends to reconnect through Apple SMB/AFP rather than our Samba service
- Apple’s `_airport._tcp` is still valuable because AirPort Utility depends on it
- some Apple-advertised services such as USB printer advertisements should be preserved if present

So the current system does not fully replace Apple mDNS with a hardcoded record set. Instead it uses a separate tiny helper:
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

This helper:
- can save a raw LAN-wide mDNS snapshot to `/mnt/Flash/allmdns.txt`
- can save a filtered Apple identity snapshot to `/mnt/Flash/applemdns.txt`
- gracefully kills Apple `mDNSResponder` during takeover
- replays Apple snapshot records afterward
- overrides only:
  - `_smb._tcp.local.`
  - `_adisk._tcp.local.`
- continues to point clients at our `smbd` on port `445`

Current practical result:
- Our `_smb._tcp` and `_adisk._tcp` remain authoritative
- Apple `_airport._tcp` and other records can be preserved
- snapshot replay preserves non-ASCII or binary host targets via `HOST_HEX`

## Bonjour Discovery Boundaries

Local Bonjour discovery is intentionally service-centric. `timecapsulesmb.discovery.bonjour.discover()` returns one normalized record per service instance, not one merged record per physical device.

That distinction matters:
- `_airport._tcp.local.` is the Apple device identity and is the only service configure uses for the interactive device list
- `_smb._tcp.local.` is the managed Samba service identity and is what doctor/deploy Bonjour checks use
- `_device-info._tcp.local.` may share the same name, hostname, and IP as `_smb._tcp.local.`, but it must remain a separate raw record

Do not merge `_airport`, `_smb`, and `_device-info` records inside `bonjour.discover()`. Merging service records creates ambiguous objects with one name/hostname but multiple meanings, and it causes duplicate-looking or misleading configure/doctor output. The stored `service_type` should remain the raw observed value. Callers should filter raw discovery results by the service prefix they actually need, such as `_airport` for configure and `_smb` for doctor/deploy. Prefix filtering intentionally matches both `_smb._tcp.local.` and `_smb._tcp.local`.

## Apple mDNS Snapshot File

The mDNS snapshot files are:

- `/mnt/Flash/allmdns.txt`
- `/mnt/Flash/applemdns.txt`

Current behavior:
- `start-samba.sh` gives Apple a short chance to start its own stack
- `mdns-advertiser --save-all-snapshot` captures a raw LAN-wide snapshot into `allmdns.txt`
- `mdns-advertiser --save-snapshot` captures only the local Apple identity into `applemdns.txt`
- `mdns-advertiser --load-snapshot` then kills `mDNSResponder` and replays the snapshot
- if snapshot load fails, the helper falls back to the generated managed records

The raw `allmdns.txt` file is intentionally diagnostic and may contain all Apple records that were captured on the LAN.

The filtered `applemdns.txt` file is the one used for replay:
- when local AirPort identity MACs are available, snapshot save keeps only records tied to the matching local `_airport._tcp` identity
- if a new capture cannot be tied back to the local unit, `applemdns.txt` is not refreshed
- if no local identity MACs are available, the helper saves the raw capture for diagnostics but still refuses to trust it for replay

However, on replay:
- `_smb._tcp` from the snapshot is ignored
- `_adisk._tcp` from the snapshot is ignored
- our managed `_smb._tcp` and `_adisk._tcp` are advertised instead

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
2. prepares the dedicated Samba lock ramdisk at `/mnt/Locks`
3. recreates the RAM runtime tree
4. waits for the device IP on the configured network interface
   - default: `bridge0`
5. starts the mDNS snapshot capture phase without taking over UDP 5353 yet
6. waits briefly for an Apple-mounted data root under `/Volumes/dk2` or `/Volumes/dk3`, giving a chance for Apple to mount the disk so Airport Utility does not give a "disk corrupt" error
7. if Apple did not mount it, falls back to bounded manual `mount_hfs` attempts
8. discovers or initializes the real data root by checking:
   - `ShareRoot/.com.apple.timemachine.supported`
   - `Shared/.com.apple.timemachine.supported`
9. finds the persistent payload directory
10. copies `smbd` into `/mnt/Memory/samba4/sbin`
11. if `private/nbns.enabled` exists in the persistent payload, also copies `nbns-advertiser` into `/mnt/Memory/samba4/sbin`
12. renders `smb.conf` from the template
13. starts `smbd` and waits briefly until the process is observed
14. starts the final `mdns-advertiser` phase, which takes over UDP 5353 and advertises the configured services plus the captured Apple records when available
15. starts the NBNS responder if enabled

The boot log is written to:
- `/mnt/Memory/samba4/var/rc.local.log`

Important bug lessons from getting this stable:
- the script cannot assume `/dev/dk2` exists immediately
- the script must use `-b` for block devices, not `-c`
- it cannot call non-existent utilities like `dirname`
- it must tolerate a long delay before the disk appears
- the Samba lock TDBs need their own ramdisk because `/mnt/Memory` is too small for the runtime plus growing lock databases
- on NetBSD 4, cache state is kept on the HDD instead of `/mnt/Memory` to preserve RAM-disk headroom
- the persistent `xattr.tdb` must stay on the HDD because it records extended attribute state for files on the share

### `/mnt/Locks`

Samba lock state now lives on a dedicated second ramdisk:
- `lock directory = /mnt/Locks`

Current mount behavior:
- NetBSD 6 mounts `tmpfs` at `/mnt/Locks`
- NetBSD 4 mounts `mfs` at `/mnt/Locks`
- if the NetBSD 6 tmpfs mount fails, startup falls back to a plain `/mnt/Locks` directory on the root filesystem
- if the NetBSD 4 mfs mount fails, startup aborts instead of falling back to the tiny root filesystem

Operational behavior:
- `start-samba.sh` clears `/mnt/Locks/*` before starting `smbd`
- `watchdog.sh` also clears `/mnt/Locks/*` before restarting `smbd`

### `watchdog.sh`

`watchdog.sh` is a simple long-running supervisor launched at boot from flash.

Current behavior:
- sleeps `30` seconds before the first management pass
- uses real elapsed wall-clock time since watchdog start
- polls every `10` seconds while any managed service is unhealthy
- polls every `300` seconds once all managed services are healthy
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
- `rc.local` uses a subshell-scoped `set +e` workaround only around the watchdog probe/start block
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
- `/Volumes/dk2/.samba4/private/smbpasswd`
- `/Volumes/dk2/.samba4/private/username.map`

Current optional NBNS state lives on the HDD:
- `/Volumes/dk2/.samba4/nbns-advertiser`
- `/Volumes/dk2/.samba4/private/nbns.enabled`

Current persistent Time Machine metadata state also lives on the HDD:
- `/Volumes/dk2/.samba4/private/xattr.tdb`

Current NetBSD 4 Samba cache state lives on the HDD to preserve RAM headroom:
- `/Volumes/dk2/.samba4/cache`

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
- `reset on zero vc = yes`
- `path = /Volumes/dk2/ShareRoot` on the tested box
- `pid directory = /mnt/Memory/samba4/var`
- `lock directory = /mnt/Locks`
- `state directory = /mnt/Memory/samba4/var`
- `cache directory = /mnt/Memory/samba4/var` on NetBSD 6
- `cache directory = /Volumes/dk2/.samba4/cache` on NetBSD 4
- `private dir = /mnt/Memory/samba4/private`
- `max log size = 256` in the normal shipped template
- `deadtime = 60`
- `vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb`
- `fruit:resource = file`
- `fruit:veto_appledouble = yes`
- `fruit:metadata = stream`
- `fruit:time machine = yes`
- `fruit:posix_rename = yes`
- `streams_xattr:store_stream_type = no`
- `acl_xattr:ignore system acls = yes`
- `xattr_tdb:file = /Volumes/dk2/.samba4/private/xattr.tdb` on the tested box

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
- static NetBSD 4 little-endian `earmv4` binary for the NetBSD 4 little-endian payload
- static NetBSD 4 big-endian `armeb` binary for the NetBSD 4 big-endian payload
- see the artifact section below for current checked-in binary sizes
- installed on both the HDD payload and `/mnt/Flash`
- run from `/mnt/Flash` to save RAM-disk space

At runtime it can:
- advertise managed `_smb._tcp.local.`
- advertise managed `_adisk._tcp.local.`
- advertise loaded snapshot records
- optionally advertise fallback generated `_airport._tcp.local.`
- save an Apple snapshot with `--save-snapshot`
- load and replay an Apple snapshot with `--load-snapshot`
- answer A queries for loaded snapshot host targets using the current configured IPv4

Current validation and behavior notes:
- mDNS host labels are validated as DNS-label-safe host labels
- mDNS instance names may contain spaces and are validated separately from host labels
- service types are validated as dotted DNS names
- `_adisk._tcp` TXT payload sizing is validated before advertisement
- `_airport._tcp` fields are all optional; missing fields are simply omitted from the TXT payload
- snapshot replay preserves non-ASCII or binary hostnames using `HOST_HEX`
- when snapshot mode is active, `_device-info._tcp` is not generated unless it comes from the snapshot

## NBNS Responder Details

The optional NBNS helper is:
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)

It is built from:
- [build/nbns-advertiser.c](build/nbns-advertiser.c)
- [build/nbns.sh](build/nbns.sh)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 little-endian `earmv4` binary for the NetBSD 4 little-endian payload
- static NetBSD 4 big-endian `armeb` binary for the NetBSD 4 big-endian payload
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
- the binary is uploaded to `/Volumes/dkX/.samba4/nbns-advertiser` on every deploy
- runtime enablement is controlled by the marker file:
  - `/Volumes/dkX/.samba4/private/nbns.enabled`
- `tcapsule deploy --install-nbns` creates that marker
- `--install-nbns` is supported on both NetBSD 6 and NetBSD 4
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
7. optionally repair the HDD before redeploying
   - [src/timecapsulesmb/cli/fsck.py](src/timecapsulesmb/cli/fsck.py)
8. remove the payload later if needed
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
- `TC_AIRPORT_SYAP`
- `TC_CONFIGURE_ID`

Current `.bootstrap` values include:
- `INSTALL_ID`
- optional `TELEMETRY=false`

Optional deploy flag:
- `--install-nbns`
  - enables the bundled NBNS responder on the next boot by creating `private/nbns.enabled`

Current defaults:
- `TC_SHARE_NAME=Data`
- `TC_SAMBA_USER=admin`
- `TC_NETBIOS_NAME=TimeCapsule`
- `TC_PAYLOAD_DIR_NAME=.samba4`
- `TC_MDNS_INSTANCE_NAME=Time Capsule Samba 4`
- `TC_MDNS_HOST_LABEL=timecapsulesamba4`
- `TC_MDNS_DEVICE_MODEL=TimeCapsule`

Current validation behavior:
- `TC_HOST`: must be non-empty.
- `TC_NET_IFACE`: must be a safe interface name.
- `TC_SHARE_NAME`: must be Samba/adisk-safe; spaces are allowed, but path separators, control characters, and shell-hostile characters are rejected.
- `TC_SAMBA_USER`: must be non-empty, fit Samba's username length limit, and avoid whitespace or control characters.
- `TC_NETBIOS_NAME`: must fit the 15-byte NetBIOS limit and use NetBIOS-safe characters.
- `TC_PAYLOAD_DIR_NAME`: must be one safe path component; slashes, `.`/`..`, control characters, traversal, and shell-hostile characters are rejected.
- `TC_MDNS_INSTANCE_NAME`: may contain spaces, but must be valid printable mDNS instance text within DNS name limits.
- `TC_MDNS_HOST_LABEL`: must be a single DNS-safe label using letters, numbers, and hyphens; spaces are rejected.
- `TC_MDNS_DEVICE_MODEL`: must be `TimeCapsule` or one of the supported Time Capsule model identifiers.
- `TC_AIRPORT_SYAP`: must be one of the known Apple syAP codes.
- when `TC_AIRPORT_SYAP` is one of the known exact generation values, `TC_MDNS_DEVICE_MODEL` must match it rather than remaining a mismatched generic or wrong generation.
- `TC_CONFIGURE_ID`: is a local configuration revision ID and is not user-validated.

Workflow details:
- `configure` now starts by attempting mDNS discovery of the Time Capsule on the local network
- if SSH is already reachable, `configure` validates the SSH target/password and then probes the device directly
- `configure` can now choose `TC_AIRPORT_SYAP` and `TC_MDNS_DEVICE_MODEL` from several sources, in priority order:
  - discovered `_airport._tcp` `syAP`
  - exact probed compatibility match
  - probed Apple identity from `ACPData.bin`
  - model derived from the chosen `syAP`
  - saved valid values from `.env`
- for NetBSD 4 devices, the probe/compatibility layer narrows the allowed `syAP` and model candidates by endianness and then narrows further when `ACPData.bin` identifies the exact generation
- `configure` validates user-facing mDNS/share inputs before writing `.env`
- `deploy`, `activate`, and `doctor` fail early when managed `.env` config values are invalid
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
- [src/timecapsulesmb/identity.py](src/timecapsulesmb/identity.py): local install identity loaded from `.bootstrap`
- [src/timecapsulesmb/telemetry.py](src/timecapsulesmb/telemetry.py): best-effort client telemetry for `configure`, `deploy`, `activate`, and `doctor`
- [build/](build): maintainer build tooling, including Samba cross-exec record/replay helpers

Developer note:
- [src/timecapsulesmb/cli/context.py](src/timecapsulesmb/cli/context.py) owns shared per-command lifecycle state such as timing, command IDs, result state, and finish handling.
- [src/timecapsulesmb/cli/runtime.py](src/timecapsulesmb/cli/runtime.py) owns shared runtime helpers for `.env` loading, SSH connection resolution, validation entrypoints, and compatibility probing.
- Normal users should not need these details; they mostly keep command entrypoints smaller and more consistent.

Practical consequence:
- if you want to modify how the box is discovered, start in `discovery/`
- if you want to change what gets uploaded, start in `deploy/planner.py` and `deploy/executor.py`
- if you want to change the on-device boot behavior, inspect the packaged boot assets and the runtime layout sections below
- if you want to replace binaries or rebuild them, inspect the artifact manifest plus the `build/` tree

## Doctor Command

[src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py) is a non-destructive local diagnostic helper.

It checks:
- `.env` completeness and invalid `.env` values
- required local tools
- whether the required checked-in binaries exist and match the expected checksums
- SSH reachability
- remote network/interface problems
- advertised Bonjour instance name
- advertised Bonjour host label
- active Samba NetBIOS name
- active Samba share names
- SMB reachability
- `_smb._tcp` browse and resolve
- optional NBNS name resolution when `private/nbns.enabled` is present on the device
- authenticated `smbclient -L` listing
- authenticated SMB CRUD operations via `smbclient`
- that the configured share name is present in the authenticated SMB listing
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
.venv/bin/tcapsule repair-xattrs --path /Volumes/Data
```

If `TC_SHARE_NAME` is set in `.env`, `--path` can usually be omitted. The command reads the local `mount` table, finds mounted `smbfs` shares, and prefers the mount whose server and share name match the configured `TC_HOST` and `TC_SHARE_NAME`:

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
- validates the managed config before touching the device
- validates the required binary artifacts against the artifact manifest
- discovers the correct volume root on the Time Capsule
- probes device compatibility and rejects unsupported targets before upload
- computes the device-specific runtime and payload paths
- builds a deployment plan before execution
- creates the persistent payload dir under `/Volumes/dkX/<TC_PAYLOAD_DIR_NAME>` (usually `/Volumes/dkX/.samba4`)
- uploads the checked-in binaries:
  - `smbd`
  - `mdns-advertiser`
  - `nbns-advertiser`
- renders and uploads the packaged boot/runtime files:
  - `smb.conf.template`
  - `rc.local`
  - `common.sh`
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
- waits for managed `smbd` readiness after reboot
- waits for managed mDNS takeover after reboot
- then verifies Bonjour and authenticated SMB access using the same shared checks used by `doctor`
- on NetBSD 4, deploy uploads the NetBSD 4 artifact set and immediately runs the activation sequence instead of rebooting

Current compatibility behavior:
- NetBSD 6 `evbarm` devices are accepted for the current `samba4` payload family
- NetBSD 4 `evbarm` devices are accepted as older hardware and use either the `netbsd4le_samba4` or `netbsd4be_samba4` payload family
- `configure` reuses the same classification logic to choose a better default Finder model hint

NetBSD 4 activation behavior:
- `tcapsule deploy` uploads the NetBSD 4 payload, stops the old watchdog plus `wcifsfs`, runs `/mnt/Flash/rc.local`, and verifies `smbd` on TCP `445` plus `mdns-advertiser` on UDP `5353`
- `tcapsule activate` repeats that activation sequence without re-uploading files
- Apple `mDNSResponder` takeover is now handled inside `mdns-advertiser` when `--load-snapshot` is used
- tested 1st-generation NetBSD 4 hardware does not persist an `/etc` boot hook and therefore needs manual activation after reboot
- other NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet proven
- `activate` is intentionally conservative: if `smbd` already owns TCP `445` and `mdns-advertiser` already owns UDP `5353`, it skips running `/mnt/Flash/rc.local`

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
- `tcapsule deploy --debug-logging` renders the Samba config with extended hard-drive logging for debugging.
- it writes `log.smbd` under `<data_root>/samba4-logs/`, sets `max log size = 1048576`, and enables `log level = 5 vfs:8 fruit:8`.
- this flag is intentionally not documented in the normal command help because it is for active debugging, not normal installs.

## Client Telemetry

Client telemetry is now emitted by:
- `tcapsule configure`
- `tcapsule deploy`
- `tcapsule activate`
- `tcapsule doctor`

Current event model:
- `configure_started`
- `configure_finished`
- `deploy_started`
- `deploy_finished`
- `activate_started`
- `activate_finished`
- `doctor_started`
- `doctor_finished`

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
- stops the watchdog first so it cannot restart `smbd` during teardown
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
- [bin/samba3-netbsd4le/smbd](bin/samba3-netbsd4le/smbd)
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)
- [bin/mdns-netbsd4le/mdns-advertiser](bin/mdns-netbsd4le/mdns-advertiser)
- [bin/mdns-netbsd4be/mdns-advertiser](bin/mdns-netbsd4be/mdns-advertiser)
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)
- [bin/nbns-netbsd4le/nbns-advertiser](bin/nbns-netbsd4le/nbns-advertiser)
- [bin/nbns-netbsd4be/nbns-advertiser](bin/nbns-netbsd4be/nbns-advertiser)

Current active deploy artifact sizes:
- NetBSD 6 `smbd`: about `11M`
- NetBSD 6 `mdns-advertiser`: about `249K`
- NetBSD 6 `nbns-advertiser`: about `184K`
- NetBSD 4 little-endian `smbd`: about `11M`
- NetBSD 4 big-endian `smbd`: about `11M`
- NetBSD 4 little-endian `samba3 smbd`: about `8.0M`
- NetBSD 4 little-endian `mdns-advertiser`: about `186K`
- NetBSD 4 big-endian `mdns-advertiser`: about `185K`
- NetBSD 4 little-endian `nbns-advertiser`: about `113K`
- NetBSD 4 big-endian `nbns-advertiser`: about `112K`

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
  - [build/downloadoldle.sh](build/downloadoldle.sh)
  - [build/bootstrapoldle.sh](build/bootstrapoldle.sh)
  - [build/hellooldle.sh](build/hellooldle.sh)
  - [build/hellooldbe.sh](build/hellooldbe.sh)
  - [build/downloadsamba3oldle.sh](build/downloadsamba3oldle.sh)
  - [build/downloadsamba3oldbe.sh](build/downloadsamba3oldbe.sh)
  - [build/samba3oldle.sh](build/samba3oldle.sh)
  - [build/samba3oldbe.sh](build/samba3oldbe.sh)
  - [build/downloadsamba4oldle.sh](build/downloadsamba4oldle.sh)
  - [build/downloadsamba4oldbe.sh](build/downloadsamba4oldbe.sh)
  - [build/samba4oldle.sh](build/samba4oldle.sh)
  - [build/samba4oldbe.sh](build/samba4oldbe.sh)
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
smbutil view //admin:<password>@<configured-or-advertised-host>
```

Mount the share:

```bash
mount_smbfs //admin:<password>@<configured-or-advertised-host>/Data /tmp/tc-auth-mount
```

Current expected result:
- `IPC$`
- `Data`

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
- authenticates as `admin`
- serves the internal disk through Samba 4.8
- supports Time Machine via `vfs_fruit`

The main remaining “nice to have” work is polish, not core functionality.
