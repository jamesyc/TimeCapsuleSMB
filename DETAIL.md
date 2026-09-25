# TimeCapsuleSMB Detail Reference

This file is the long-form engineering reference for the current system.

It is intentionally denser than [README.md](README.md). The README is the user-facing overview. This file is for maintainers, contributors, and users who want the actual constraints, rationale, and implementation details in one place before they start modifying the box or the tooling.

## Current Working State

The current system works end to end on the target Apple AirPort Time Capsule.

What is working now:
- static Samba 4.25.0rc2 built from NetBSD 7 sources for NetBSD 6-era AirPort storage devices
- static Samba 4.25.0rc2 built from NetBSD 4 sources for older NetBSD 4-era AirPort storage devices
- one static `service` image containing manager, discovery, telemetry, and diagnostic roles
- boot-time runtime staging via `/mnt/Flash/rc.local`
- native manager for `smbd`, discovery, telemetry, and optional rsync
- direct SMB service on port `445`
- native HFS FinderInfo, extended-attribute, and resource-fork storage shared with Apple's AFP server
- a two-phase deploy migrator for legacy `xattr.tdb` records and `._` AppleDouble resource files
- Bonjour advertisement for:
  - managed `_smb._tcp`
  - managed `_adisk._tcp`
  - optional managed `_afpovertcp._tcp` when AFP advertisement is enabled
  - Apple-published `_device-info._tcp`, `_airport._tcp`, and USB printer records, left untouched
- authenticated SMB access using:
  - examples and docs use Samba username `admin`
  - boot-time generated RAM auth stores a `root` Samba account
  - incoming SMB usernames are mapped to Unix `root`
  - password: the current AirPort device password read from `/usr/bin/acp -q syPW`
- guest access disabled
- deploy-time device compatibility detection
- manual NetBSD 4 activation via `tcapsule activate`
- manual disk repair via `tcapsule fsck`
- managed-file uninstall via `tcapsule uninstall`; firmware boot-hook patches are restored separately with `tcapsule flash --restore`

Current validation status:
- NetBSD 6 is validated end to end with reboot-persistent startup
- tested NetBSD 4 gen1 hardware without the firmware boot-hook patch is validated with manual `tcapsule activate` after reboot
- other unpatched NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet confirmed

Current user experience:
- the Time Capsule advertises `_smb._tcp`
- the Time Capsule advertises `_adisk._tcp` for Time Machine
- Apple's ACPd publishes `_airport._tcp` for AirPort Utility; we never touch it
- the Time Capsule answers NBNS name queries for the active runtime NetBIOS name through Apple's `wcifsnd` whenever eligible
- the Bonjour instance name is managed by Apple's mDNSResponder, including conflict renaming; the Samba server string is derived from Apple `syNm`
- the Samba NetBIOS name is derived from `/bin/hostname`, with `syNm` fallbacks; the Bonjour hostname belongs to Apple's mDNSResponder
- shares are derived from Apple `MaSt` volume metadata and are available as:
  - `smb://<advertised-host>.local/<sanitized and de-duplicated volume share name>`

Current auth model:
- the docs and examples use SMB login user `admin`
- the current AirPort device password is used as the SMB password
- boot-time generated Samba auth stores a `root` SMB account hash in RAM
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

Current deploy compatibility classification uses the NetBSD major version and detected ELF endianness; the reported architecture and AirPort identity narrow the displayed device candidates but do not select the payload by themselves:
- little-endian NetBSD 6.x: current `netbsd6_samba4` target, corresponding to 5th-generation Time Capsules and same-era AirPort devices
- little-endian NetBSD 4.x: `netbsd4le_samba4`, covering 3rd-4th-generation Time Capsules and 3rd-5th-generation AirPort Extreme hardware
- big-endian NetBSD 4.x: `netbsd4be_samba4`, covering 1st-2nd-generation Time Capsules and 1st-2nd-generation AirPort Extreme hardware
  - tested gen1 hardware without the firmware boot-hook patch needs manual `activate` after reboot
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
  - `/Volumes/dkX/.samba4/rsync`
  - `/Volumes/dkX/.samba4/rsyncd.conf`
  - `/Volumes/dkX/.samba4/private/`
  - `/Volumes/dkX/.samba4/private/xattr.tdb`
  - `/Volumes/dkX/.samba4/cache`
  - `/Volumes/dkX/.samba4/logs/`
- tiny persistent boot hook on flash:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/boot.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/service`
  - `/mnt/Flash/tcapsulesmb.conf`
- transient runtime on RAM disk:
  - `/mnt/Memory/samba4`
  - `/mnt/Memory/debug` and `/mnt/Memory/debug.sig` for temporary signed debug execution
  - `/mnt/Locks`

This gives:
- persistence on disk
- safe execution from RAM
- only tiny always-mounted files on flash

Current naming split:
- `.samba4` is the fixed managed persistent HDD payload directory
- the live RAM runtime path is intentionally fixed at `/mnt/Memory/samba4`
- share names are not configured locally; runtime sanitizes and de-duplicates the Apple `MaSt` partition names

## Why Samba 4.8, Then 4.25.0rc2

The project did not land on Samba 4.x by accident. Samba 4.8 was the first fully working Time Machine target on this hardware; the current checked-in deploy artifacts are Samba 4.25.0rc2.

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

### Samba 4.25.0rc2

Samba 4.25.0rc2 is the current shipped target. It keeps the same static-module deployment model, but uses the newer `samba4x` build lanes and checked-in artifacts.

With the current static-module build, the shipped config supports:
- `catia`
- `fruit`
- `streams_xattr`
- `acl_xattr`
- `xattr_tdb`
- optional `aio_fork`, disabled by default and bounded to eight children per share when enabled
- `fruit:time machine = yes`

## Native Mac Metadata Architecture

Apple's HFS implementation stores the Mac concepts Samba must expose in three
different kinds of native object. They should not be collapsed into a single
extended-attribute mechanism:

| Mac concept | SMB representation | Owning VFS layer | Native HFS storage | Future FAT32 storage | Migrator input and output |
| --- | --- | --- | --- | --- | --- |
| FinderInfo | `:AFP_AfpInfo:$DATA` | `fruit` | `com.apple.FinderInfo` catalog metadata | `fruit:metadata=stream\|netatalk` backed by TDB | Selected TDB metadata → native FinderInfo |
| Tags and other Mac xattrs | `:com.apple.…:$DATA` | `streams_xattr` → `xattr_tdb` | Canonical HFS xattr | Encoded stream in `xattr.tdb` | TDB stream → canonical HFS xattr |
| Resource fork | `:AFP_Resource:$DATA` | `fruit` | `file/..namedfork/rsrc` | `._file` through `fruit:resource=file` | AppleDouble resource entry → native resource fork |
| Windows-only ADS | Ordinary named stream | `streams_xattr` → `xattr_tdb` | Encoded/sharded HFS xattrs | `xattr.tdb` | TDB stream/extents → native HFS xattrs |
| NT ACL | Samba security xattr | `acl_xattr` → `xattr_tdb` | Native HFS security xattr | `xattr.tdb` | TDB security xattr → native HFS xattr |

`vfs_fruit` already intercepts the two special Mac streams before
`streams_xattr`. On HFS, its effective metadata backend synthesizes the 60-byte
`AFP_AfpInfo` stream from the native 32-byte FinderInfo value. Its effective
resource backend opens `file/..namedfork/rsrc` as a real descriptor, so large
resource forks use normal offset I/O and are not limited by the device's
3,802-byte ordinary-xattr ceiling.

All other named streams continue down the configured stack:

```text
catia → fruit → streams_xattr → acl_xattr → xattr_tdb
```

On HFS, `xattr_tdb` is an automatic native backend and never opens or creates
the configured TDB. It translates Mac stream names into canonical
`com.apple.*` HFS xattrs, while excluding `com.apple.FinderInfo` and
`com.apple.ResourceFork` because `fruit` owns their special SMB semantics.
Windows-only streams remain encoded and may use HFS xattr extents. A canonical
Apple xattr that exceeds the native HFS limit is rejected rather than exposing
an incomplete first extent to AFP.

FAT32 is not currently supported: TimeCapsuleSMB does not mount or discover
FAT32 volumes. On non-HFS filesystems, the module follows its upstream TDB
behavior; the fallback is kept deliberately so a future FAT32 implementation
can use TDB-backed metadata and AppleDouble resources without another Samba
storage redesign.

### AppleDouble migration

`fruit:resource=file` means a separate `._filename` AppleDouble container, not
an HFS resource fork. A normal container has a header and entry table, a
FinderInfo entry, an optional embedded `ATTR` table containing more xattrs, and
an arbitrarily large resource entry. Deleting it after copying only the resource
entry could therefore discard FinderInfo or tags.

The standalone `xattr-hfs-migrate` helper validates all offsets and lengths,
migrates FinderInfo and embedded xattrs, streams
the resource entry into `file/..namedfork/rsrc`, and verifies the result. It
recognizes Samba's intentionally blank resource-fork placeholder. Malformed
containers, unsupported top-level entries, oversized native xattrs, or failed
read-back verification leave the sidecar untouched and fail that migration
phase.

Every detected payload `xattr.tdb` is a migration input, including incomplete
v2.2.9 and older installations. With no TDB, deploy skips both helper upload and
recursive scans. Migration is split around software replacement:

1. Read old configuration and payload locations, stop every filesystem writer including Apple's `afpserver`, disable `rc.local`, and flush Flash.
2. Upload the helper to RAM. Fingerprint sources and preserve their old metadata decoding mode in adjacent progress files.
3. `copy`: walk each unfinished available HFS volume once, merging logical attributes from all read-only input databases.
4. Replace known project software, verify and flush it, keeping old runtime configuration through cleanup.
5. `cleanup`: verify merged native values, flush files, remove verified sidecars, and save completed-volume key coverage.
6. Retire whole databases in increasing source priority; unresolved older databases retain newer authorities. Fully verified files are deleted, files containing proven orphan metadata are quarantined intact.
7. Write new configuration and `rc.local` last, flush, and reboot.

Conflicting logical values use the source file's `(mtime seconds, nanoseconds,
normalized volume UUID, payload-relative path bytes)`; the greatest tuple wins.
Absent attributes do not delete older unique values. Stream fragments stay with
their winning source. Sources remain read-only throughout both walks, preserving
precedence if deployment is interrupted.

A temporary per-file resource migration marker makes an interrupted large fork
copy distinguishable from a pre-existing native/legacy conflict. The helper
removes the marker after a complete byte-for-byte verification. TDB values win
conflicts during migration: the database stores attribute names and values, not
per-attribute modification times. Cleanup requires exact native readback before
retiring those values. Native-only values and existing native resource forks
keep their existing behavior. Stream extents are
written before their anchor so interrupted exports can be retried. Read failures
are errors, never evidence of a conflict.

Migration runs only during deploy, never during boot or disk hotplug. Each
native operation stops after five minutes without progress. Diagnostics stay in
`.samba4/logs/xattr-migration-copy.log` or `xattr-migration-cleanup.log`.
These logs include the UTC start time, selected metadata representation, TDB
details, scanned roots, and native progress/error output. Deploy distinguishes
native inactivity from SSH loss and retrieves a bounded saved-log
snapshot for up to 30 seconds. The macOS diagnostics export
retains the last deploy's stage, timestamps, operation ID and error code even
after later operations displace its recent events.
An unavailable external disk keeps its legacy TDB rows; attach it and run deploy
again to migrate them. An interrupted deployment can be rerun. Validated `xattr.tdb.migration-progress.json` files remember completed volume
UUIDs and verified key coverage for the entire source cohort. Later deploys skip
those volumes in both phases, preserving subsequent native edits. Missing or
invalid progress may cause replay, which is an accepted recovery behavior.
An unfinished volume may therefore replay authoritative TDB metadata after a
reboot; the legacy database is deliberately presumed newer than an interim AFP
edit until cleanup records that volume as complete.
Changed source contents, new sources, or a changed conversion policy invalidate
completion. Disk-number reassignment alone does not. Known absent source disks
do not invalidate existing completion, but cannot prove new volumes finished.

#### Orphans and unresolved rows (v3.1.0)

Legacy TDB keys are `(st_dev, st_ino)`, never a volume UUID, so a row that no
file claimed is one of two things and the migrator tells them apart:

- a **proven orphan**: its device is one of the roots this run walked completely
  (no scan error, no filesystem boundary crossed) and the inode is gone;
- an **unresolved** row: its device was not walked, so the metadata may belong to
  a disk that is not attached right now.

Unresolved rows keep the database live for a later run. When every row is verified or proven orphaned and at least one is an orphan,
retirement closes the original database and renames it to
`xattr.tdb.orphaned.N` beside the payload (`N` is the first unused slot, never
overwriting an earlier quarantine); nothing is deleted. Structured coverage distinguishes matched, orphaned, and unresolved keys; the
deployment log reports each source and retirement outcome. Note the limit of device-number identity: rows
written by a disk that used to sit at the same `/dev/dkN` as the current one look
like proven orphans of the current disk, which is why quarantine keeps the file.
(This attachment-evidence definition is a deliberate decision: the rows carry
nothing else, and refusing to prove any row would keep every leftover database
live forever.)

Only deploy reads the adjacent JSON completion files. Runtime daemons never
read or write migration state. Old `xattr-migration-completed.txt` files remain
ignored, and quarantined databases remain preserved.

The stream layer allows 3,803 logical bytes for canonical Apple xattrs on HFS:
3,802 native bytes plus its synthetic marker. Windows ADS retain 3,802-byte
physical fragments. Larger canonical Apple values fail before modifying the
existing attribute.

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

The current deploy artifacts use Samba 4.25.0rc2 on the same NetBSD 7 / NetBSD 4 SDK split.

The important build logic is now under [build/](build). The VM-side [build/Makefile](build/Makefile) names the supported per-family and all-lane targets while leaving the expensive SDK download/bootstrap steps explicit rather than making them artifact-build dependencies.

Current maintainer build lanes:
- NetBSD 7 SDK lane:
  - [build/download.sh](build/download.sh)
  - [build/bootstrap.sh](build/bootstrap.sh)
- NetBSD 4 SDK lane:
  - [build/downloadoldle.sh](build/downloadoldle.sh)
  - [build/bootstrapoldle.sh](build/bootstrapoldle.sh)
  - [build/downloadoldbe.sh](build/downloadoldbe.sh)
  - [build/bootstrapoldbe.sh](build/bootstrapoldbe.sh)
- NetBSD 7 current Samba 4.25.0rc2 lane:
  - [build/downloadsamba4x.sh](build/downloadsamba4x.sh)
  - [build/samba4x.sh](build/samba4x.sh)
- NetBSD 4 current Samba 4.25.0rc2 lanes:
  - [build/downloadsamba4xoldle.sh](build/downloadsamba4xoldle.sh)
  - [build/downloadsamba4xoldbe.sh](build/downloadsamba4xoldbe.sh)
  - [build/samba4xoldle.sh](build/samba4xoldle.sh)
  - [build/samba4xoldbe.sh](build/samba4xoldbe.sh)
- current rsync 3.4.4 lanes:
  - [build/downloadrsync.sh](build/downloadrsync.sh)
  - [build/rsync.sh](build/rsync.sh)
  - [build/rsyncoldle.sh](build/rsyncoldle.sh)
  - [build/rsyncoldbe.sh](build/rsyncoldbe.sh)
- NetBSD 7 utility lanes:
  - [build/hello.sh](build/hello.sh)
  - [build/service.sh](build/service.sh)
- NetBSD 4 utility lanes:
  - [build/hellooldle.sh](build/hellooldle.sh)
  - [build/hellooldbe.sh](build/hellooldbe.sh)
  - [build/serviceoldle.sh](build/serviceoldle.sh)
  - [build/serviceoldbe.sh](build/serviceoldbe.sh)

The direct scripts target the NetBSD 7 lane by default. The `*oldle.sh` and `*oldbe.sh` wrappers select the NetBSD 4 little-endian and big-endian lanes.

## How We Use Apple's mDNSResponder

Since v3.1.0 the device's own `mDNSResponder` (Apple's crunched static binary,
version 397.32 on both the NetBSD 4 and NetBSD 6 lanes) is the only mDNS
responder on the device. We never kill it: ACPd does not respawn it and a
hand-started daemon lacks `_airport._tcp`, so a dead daemon is recoverable only
by a reboot. Our `service discovery` role is a small *registrant*: it registers our
records through the standard `dns_sd` Unix-socket IPC at
`/var/run/mDNSResponder`, using Apple's own client stub (vendored, unchanged,
in [build/native/dnssd/](build/native/dnssd/)).

Why this works now and did not before: Apple's `diskd` registers `_smb._tcp`,
`_adisk._tcp` and `_afpovertcp._tcp` for Apple's own file servers
unconditionally, and Finder would follow those to Apple SMB/AFP rather than
our Samba. `diskd` is also load-bearing: it populates `acp -q MaSt` (our
volume/UUID source of truth) and serves `acp rpc diskd.useVolume` (how the
manager mounts volumes). The manager therefore relaunches it as
`/sbin/diskd -i lo0 -d local.`: it keeps doing its real job while its own
registrations never leave loopback. Our registrations use `name=NULL` and flags
`0`, so Apple's mDNSResponder owns the shared default service name and resolves
conflicts. A stock-device test on 2026-09-19 showed `diskd` renaming both SMB and
ADisk to "Name (2)" after an SMB-only conflict, while `syNm` and the hostname
remained unchanged. Registration callbacks accept that name. Doctor identifies
the device through its resolved endpoint and checks for multiple registrations
on that device, rather than rejecting a suffix shared with an unrelated peer.

| Process | Owner | Role |
| --- | --- | --- |
| `/sbin/mDNSResponder -d` | Apple (child of ACPd) | the only responder: host `A`/`AAAA` per interface, `_airport` (via ACPd), `_device-info`, printers (via `printd`), and everything we register. Never killed. |
| `ACPd` | Apple | registers `_airport._tcp` and follows the AirPort Utility WAN switches; serves `acp -q`/`acp rpc` |
| `/sbin/diskd -i lo0 -d local.` | Apple binary, relaunched by the manager | disk topology (`MaSt`), `diskd.useVolume`, spin-down; its `_smb`/`_adisk`/`_afpovertcp` stay on loopback |
| `printd` | Apple | printer discovery and `_riousbprint`/`_pdl-datastream` registration |
| `wcifsfs` | Apple | Apple SMB server, always stopped so Samba owns SMB |
| `/sbin/wcifsnd` | Apple, child owned by `service discovery` | native NBNS registration, conflict handling, WINS behavior, and UDP `137`/`138`; present only while native NBNS is eligible |
| `afpserver` | Apple | kept running regardless of the AFP advertising setting; advertising is controlled through diskd's loopback scope and our Bonjour registrations |
| `service discovery` | ours, `/mnt/Flash/service` | registers Bonjour records through mDNSResponder and owns the foreground `wcifsnd` child used for native NBNS |

What Apple publishes vs what we publish:

| Service | Source | Interfaces |
| --- | --- | --- |
| `_smb._tcp` (port 445, empty TXT) | ours | every link whose plan mask has `SVC_SMB` |
| `_adisk._tcp,_airport` (port 9, `sys=waMA=…,adVF=0x1010` + one `dkN=adVF=…,adVN=…,adVU=…` per disk) | ours | same as `_smb` |
| `_afpovertcp._tcp` (port 548) | ours, only with `MDNS_ADVERTISE_AFP=1` | same as `_smb` |
| `_airport._tcp` | Apple (ACPd) | Apple's rules |
| `_device-info._tcp` (`model=TimeCapsule6,116` …) | Apple (daemon built-in, from `/etc/mdnsd.conf`) | Apple's |
| host `A`/`AAAA` | Apple | fe80 + every IPv4 incl. 169.254, no GUA |
| printers | Apple (printd) | Apple's |

The hostname is Apple's (`AirPort-Time-Capsule.local`, from `syNm`), so SRV
targets of our registrations resolve through Apple's host records. The doctor
compares host labels case-insensitively.

### Discovery policy and Samba networking

The unified service's discovery, telemetry, and diagnostic paths share one collector in
[build/native/common/](build/native/common/): `acp -q` for
`raNA raDS waNM usbF laIP waIP waLL gnRo syNm waMA`, the kernel
interface table via our own `sysctl(NET_RT_IFLIST)` parser (libc
`getifaddrs()` returns garbage names on Apple's NetBSD 4 kernel because its
`struct if_msghdr` is 152 bytes while the SDK's is 144), and the flash config.
From those facts the plan assigns each link a role — `gnRo` owner → GUEST,
`laIP` owner → LAN, NAT mode and `waIP`/`waLL` owner → WAN, anything else →
ISOLATED — and a service mask: LAN gets SMB+ADISK (+AFP when enabled); WAN and
GUEST get the LAN mask only in NAT mode with disks-over-WAN (`usbF & 0x8`)
enabled, exactly the AirPort Utility switch; ISOLATED gets nothing. A failed ACP re-read
keeps the last validated roles on unchanged links and reports the age; a link
recreated with a new index (an AirPort Utility apply) starts isolated until a
coherent read. `service --print-link-plan` prints the whole plan.

The manager does not collect this plan. Samba also does not consume it: the
generated configuration omits `interfaces` and `bind interfaces only`, and
Samba owns IPv4 and IPv6 wildcard TCP 445 listeners. Both tested NetBSD 4 and
NetBSD 6 Time Capsules are dual-stack, including scoped IPv6 link-local SMB.
The Samba build carries a NetBSD-only `NET_RT_IFLIST` reader because Apple's
kernel routing-message layout does not match the SDK/libc layout. Bonjour
policy controls where `_smb` is visible, while Apple's firewall controls WAN
and guest reachability.

Three details from the v3.1.0 review matter here. An ACP key has three outcomes,
not two: `ok`, `unavailable` (`acp` answered that the key is not set — a real
observation; no guest network, no WAN link-local) and `abort` (timeout, exec
failure, or never asked because the 30 s collection budget ran out). Only the
first two can move a role; an aborted `laIP`/`waIP`/`waLL`/`gnRo` is a failed
re-read that keeps the last validated policy (`incomplete reason=<key>`), and an
aborted `syNm`/`waMA` keeps the previous instance name and `waMA`
(`identity … retained=1`) so a slow ACPd never renames the service or withdraws
`_adisk`. At cold start nothing is shared until the plan validates, and a link that owns a
readable `gnRo` is GUEST with no permission regardless — the guest network never
carries file sharing. And a
link plan holds every address the interface table can (64), so a link with many
IPv6 addresses is bound completely or the snapshot is marked incomplete, never
published with a subset.

## Bonjour Discovery Boundaries

Local Bonjour discovery is intentionally service-centric. `timecapsulesmb.discovery.bonjour.discover_snapshot_merged_detailed()` returns one normalized record per service instance, not one merged record per physical device.

That distinction matters:
- `_airport._tcp.local.` is the Apple device identity and is the only service configure uses for the interactive device list
- `_smb._tcp.local.` is the managed Samba service identity and is what doctor Bonjour checks use
- `_device-info._tcp.local.` may share the same name, hostname, and IP as `_smb._tcp.local.`, but it must remain a separate raw record

Do not merge `_airport`, `_smb`, and `_device-info` records inside `bonjour.discover_snapshot_merged_detailed()`. Merging service records creates ambiguous objects with one name/hostname but multiple meanings, and it causes duplicate-looking or misleading configure/doctor output. The stored `service_type` should remain the raw observed value. Callers should filter raw discovery results by the service prefix they actually need, such as `_airport` for configure and `_smb` for doctor. Prefix filtering intentionally matches both `_smb._tcp.local.` and `_smb._tcp.local`.

## Registered mDNS Records

Current behavior:
- `boot.sh` prepares platform directories and the locks filesystem, then executes `service manager`; the manager reconciles Apple's loopback `diskd`
- the manager launches `/mnt/Flash/service discovery` with the canonical Samba name (`--netbios-name`), payload state (`--diskless` when applicable), and ADisk rows (`--adisk-share NAME KEY UUID FLAGS`, repeated per share); identity and link facts otherwise come from ACP, the interface table, and flash config
- the registrant holds one `DNSServiceRef` per (link index, service), re-registers on plan changes (a `PF_ROUTE` socket plus a 30 s ACP poll), and deregisters everything on `SIGTERM` so the daemon sends goodbyes
- in diskless mode the desired set is empty; `_airport` and `_device-info` are Apple's and stay up regardless
- a name conflict or any registration error backs off (1, 2, 4 … 30 s) and retries with the unchanged desired state; an unreachable daemon marks the registrations degraded and is never started by us

## Boot Flow In Detail

`rc.local` backgrounds `boot.sh` with stdin/stdout/stderr detached so Apple's
startup can continue. `boot.sh` performs only platform preparation: RAM
directories, existing-compatible `/root` prefixes, bufcache tuning, and the
4 MiB locks filesystem. It preserves existing files and mounts, then executes
`/mnt/Flash/service manager`.

NetBSD 6 uses `mount_tmpfs -s 4m`, retaining its plain-directory fallback if
mounting fails. NetBSD 4 uses `mount_mfs -s 8192` and refuses rootfs fallback.
Boot never clears active locks. The native manager first locks its existing
executable inode, reconciles old processes, and waits for all Samba workers to
exit before clearing obsolete locks during executable replacement.

### Native manager

The [manager](build/native/service/manager.c) owns repeated work:

- Listen for Apple's `EVFILT_DEVICE` notifications and `PF_ROUTE` changes; use
  monotonic deadlines and a five-second topology confirmation interval.
- Retain MaSt as semantic disk inventory, with a ten-second fallback. Read the
  live mount table before activating or writing a volume. An unavailable MaSt
  read is distinct from a successful empty inventory.
- Use the established `diskd.useVolume` path. Keep Apple's `mDNSResponder` and
  `afpserver` alive; move/recover `diskd` on loopback without replacing Apple's
  filesystem management.
- Prefer a valid internal payload, then an external one. Cache the selected
  generation instead of repeatedly reading HDD software metadata.
- Prepare ShareRoot/markers, RAM executables, authentication and Samba config
  in bounded child jobs. Supervision and shutdown stay responsive during slow
  ACP or disk operations. Failed or superseded jobs remain retryable.
- Own `smbd -F --no-process-group` directly in a separate process group.
  Restart for executable replacement and reload ordinary configuration changes;
  interface/address changes are Samba's responsibility and do not restart it.
- Forward parent reloads to workers. Reset Samba's cwd cache and inspect real
  retained descriptors plus volume/root identity, disconnecting only affected
  trees through Samba's existing asynchronous AIO-draining path.
- Start independent `service discovery` and `service telemetry --daemon`
  processes from the single Flash image. Each collects its own network plan;
  discovery receives only successfully applied share rows in argv.
- Recover exited children with bounded backoff. Discovery owns its native
  `wcifsnd`; healthy checks never kill that child independently. If Apple's
  conflicting `wcifsfs` returns, stop it and replace the discovery generation.
- Stop Samba and rsync if no valid payload remains, even if their old RAM
  executable is present. Discovery can remain diskless without waiting for
  Samba authentication, and telemetry retains its existing signed-job drain.

Internal shares use ShareRoot unless the disk-root option is enabled; external
shares use the volume root. Existing naming, ADisk device keys/UUIDs, protocol,
AIO, metadata, logging and performance preferences are preserved. Only deployment
runs metadata migration; boot and hotplug never scan legacy TDBs or consume old
migration-completed markers.

The manager keeps state in memory. Anonymous pipes carry parent lifetime and
short-lived job results; there is no new PID/status file or network-plan IPC.
Process titles identify manager, discovery, telemetry and setup-job roles.

Logs:

- RAM: `var/rc.local.log`, `var/runtime.log`, `var/telemetry.log`, `var/rsync.log`
  under `/mnt/Memory/samba4`; runtime logs retain a bounded tail in place.
- Discovery: `<payload>/logs/discovery.log`, with a RAM fallback while diskless.
- Samba: `<payload>/logs/log.smbd` and `smbd-console.log`.

The executable [native regressions](tests/native/README.md) replace the old
shell cadence and function-stubbing tests. Patched Samba tests separately cover
reload delivery, volume replacement, descriptor revocation and asynchronous
teardown with another tree still active.

## Optional rsync Daemon

Deploy always installs the device-family rsync binary and its generated daemon configuration in the selected HDD payload:

- `/Volumes/dkX/.samba4/rsync`
- `/Volumes/dkX/.samba4/rsyncd.conf`

Installation and enablement are separate. The macOS app exposes **Enable rsync** in both the Install options and saved device settings, while the CLI uses:

```bash
.venv/bin/tcapsule deploy --enable-rsync
```

That selection is persisted as `RSYNC_ENABLED=0|1` in `/mnt/Flash/tcapsulesmb.conf`. The binary and configuration stay on the HDD either way. On a successful app deploy, the selected value is also saved to the device profile and restored into later Install sessions. Existing profiles that predate this setting default to disabled.

When rsync is enabled, the manager:

1. discovers the currently mounted payload volume and creates its `ShareRoot` if needed
2. copies the rsync binary to `/mnt/Memory/samba4/sbin/rsync`
3. stages `/mnt/Memory/samba4/etc/rsyncd.conf`, rewriting the module path to the payload volume's current `/Volumes/dkN/ShareRoot` mount rather than trusting its deploy-time device number
4. starts the RAM copy with `--daemon --no-detach`
5. verifies the live `rsync` process owns TCP port `873`

The daemon exposes a writable, unauthenticated module named `shareroot`. Only enable it on a trusted network. Its log lives at `/mnt/Memory/samba4/var/rsync.log` and uses the same shared runtime log bounding helper as the other managed services, with the normal `32768`-byte limit.

Disabling rsync on a later deploy leaves the persistent HDD files installed, but the manager stops the daemon and removes the RAM binary and configuration. No rsync PID file is used: the manager owns its foreground child and checks its listeners. This deliberately avoids stale runtime state files.

## SMB Runtime Layout

When boot succeeds, the runtime tree under `/mnt/Memory/samba4` contains:
- `sbin/smbd`
- optionally `sbin/rsync`
- `etc/smb.conf`
- optionally `etc/rsyncd.conf`
- `var/`
- `private/`

Current auth files are generated during runtime staging and live only in RAM:
- `/mnt/Memory/samba4/private/smbpasswd`
- `/mnt/Memory/samba4/private/username.map`

The selected payload home still contains `/Volumes/dkX/.samba4/private/` for persistent Samba metadata such as `xattr.tdb`.

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
- `min protocol = SMB2` and `max protocol = SMB3` by default
- protocol and signing/encryption override modes can omit or replace those defaults
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
- `deadtime = 720`
- `vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb`
- when `TC_VFS_AIO_FORK_ENABLED=true`, append `aio_fork`, cap each share at `aio_fork:max_children = 8`, set 128 KiB SMB2 read/write limits, and enable AIO for requests of at least one byte
- `fruit:resource = file`; this remains the non-HFS and migration-source setting, while HFS shares automatically use the native resource fork
- `fruit:veto_appledouble = yes`
- `fruit:metadata = netatalk` by default, or `fruit:metadata = stream` when Netatalk metadata mode is explicitly disabled; on HFS this selects the preferred legacy migration source while runtime FinderInfo is native
- `fruit:time machine = yes`
- `fruit:posix_rename = yes`
- `acl_xattr:ignore system acls = yes`
- `xattr_tdb:file = /Volumes/dkX/.samba4/private/xattr.tdb`; HFS shares bypass this backend after migration, while the configured path remains available for a future non-HFS filesystem
- `veto files = /.samba4/` on every share so the payload is hidden when it lives on a shared disk root

Current auth mapping:
- the docs and examples use `admin` as the normal user-facing SMB login name
- the RAM `smbpasswd` backend contains a `root` entry generated from live AirPort `syPW`
- RAM `username.map` contains:
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

## Symbolic Links

Symlinks are stored on disk as native POSIX links, the same objects Apple's AFP
server and SSH create, so every protocol sees one link (Samba patch 0045,
`tc:native symlinks` in the generated `smb.conf`):

- macOS clients see native links as links and create them as usual; each new
  link becomes native when its creating handle closes. XSym link files written
  by earlier releases keep working and are never migrated; rewriting one makes
  it native.
- Windows `mklink` works. `mklink /D` (directory symlinks) is refused ("Access is
  denied"); directory links made by other clients are listed as directory
  symlinks and can be followed and removed.
- Linux clients (`fs/smb/client`) create links with the symlink, NFS or WSL
  reparse forms, depending on the `symlink=` mount option, and all three are
  accepted. With `mfsymlinks` they write XSym files, which become native too. FIFOs, sockets and device nodes are refused, which that client
  reports as `EOPNOTSUPP`. The device suite checks this by sending the same SMB2
  requests the Linux source sends; no Linux mount was tested.
- While converting, smbd moves the original aside as `.tc-xsym.<ino>.<pid>`
  and removes it when its handle closes. The generated `smb.conf` vetoes that
  name, and `delete veto files = yes` lets a folder be removed even if a crash
  left one behind.
- Attributes and streams set on a link stay on the link. `touch -h` does not
  change a link's times on HFS, the same as over AFP.

The devices' HFS driver has a journaling bug: a kernel panic, `jnl: start_tr:
active_tr is NULL`, recorded in `/mnt/Flash/dmesg.panic`. Without a workaround,
converting new SMB links triggered it within minutes of link-heavy testing, and
journal replay after the unclean restart can overwrite recently reused blocks.
Each conversion therefore ends with `sync()`. Keep the conversion path and that
`sync()` together when changing either.

Device checks: `.venv/bin/python -m tests.samba.links_device --env .env --afp`
(see `tests/samba/README.md`).

## Discovery Controller Details

The discovery controller is the `service discovery` role of `bin/service/service`.

It is built from:
- [build/native/discovery/](build/native/discovery/) (controller entry point, Bonjour registrant, ADisk TXT generation, and `wcifsnd` lifecycle/IPC)
- [build/native/common/](build/native/common/) (the shared device plan)
- [build/native/dnssd/](build/native/dnssd/) (Apple's `dns_sd` client stub, tag `mDNSResponder-379.38.1`, BSD-licensed, compiled unchanged with `-D_DNS_SD_LIBDISPATCH=0`)
- [build/native/service.sources](build/native/service.sources) and [build/service.sh](build/service.sh) (the unified runtime image)

Important properties:
- static NetBSD 7 `earmv4` binary for the NetBSD 6 payload
- static NetBSD 4 little-endian `earmv4` binary for the NetBSD 4 little-endian payload
- static NetBSD 4 big-endian `armeb` binary for the NetBSD 4 big-endian payload
- see the artifact section below for current checked-in binary sizes
- linked into the unified service installed and run from `/mnt/Flash`
- talks to `/var/run/mDNSResponder` for Bonjour and to the owned `/sbin/wcifsnd` child over Apple's loopback UDP control protocol

CLI: `service discovery [--diskless] [--netbios-name NAME] [--adisk-share NAME KEY UUID FLAGS]... [--debug-logging]`,
plus `--help`. `service --print-link-plan` and
`service --print-mast` are top-level diagnostics, not discovery-role aliases.
Host builds with `TC_NATIVE_TEST` additionally accept `--facts-file F` to
replace live collection with a text snapshot. Device binaries omit this
option and its parser; live diagnostics remain available.

At runtime it:
- registers `_smb._tcp` (port 445, empty TXT) on every link the plan grants `SVC_SMB`
- registers `_adisk._tcp,_airport` (port 9) with the same TXT items as before v3.1.0 (`sys=waMA=…,adVF=0x1010` and one `dkN=adVF=…,adVN=…,adVU=…` per configured share) where the plan grants `SVC_ADISK` and `waMA` is known
- registers `_afpovertcp._tcp` (port 548) only when `MDNS_ADVERTISE_AFP=1`
- uses Apple's shared default instance name with automatic renaming; callback names such as "Name (2)" are accepted without replacing registrations, and ACP name changes do not force a restart
- treats a daemon that stops answering as degraded, retries on the backoff timer, and never spawns `/sbin/mDNSResponder`
- starts `/sbin/wcifsnd` only when the payload is ready, the canonical name is available, and the validated plan has an SMB-eligible IPv4 address
- sequentially registers machine `<00>`, `WORKGROUP<00>` and machine `<20>` exactly once for each fresh child generation; Apple's daemon supplies native conflict processing and WINS-configured behavior
- refreshes the active child with SIGHUP after valid plan refreshes and stops the exact owned child on loss of eligibility or shutdown; the manager removes orphans before replacement
- publishes `nbns=disabled|waiting|starting|ready`, payload mode, diskless state, and the canonical name in its process title. Doctor requires a single controller and, when eligible, a single child whose parent is that controller and which owns UDP `137` and `138`
- logs one line per register/deregister/callback and one per plan change to the manager-provided log file

## Native NBNS Scope

NBNS is provided automatically by Apple's firmware `wcifsnd`; this project no longer ships a separate NBNS responder or an enable/disable preference. Old `NBNS_ENABLED` flash values and saved app preferences are ignored. The removed `--no-nbns` CLI flag and `nbns_enabled` app API parameter are rejected.

`uninstall` stops the discovery role and any orphaned `wcifsnd`, then removes the flash runtime config.

Once enabled, Apple's daemon enumerates interfaces according to firmware policy. The validated plan provides the coarse cold-start eligibility gate, but native NBNS does not promise Bonjour's per-interface `SVC_SMB` filtering. Samba listens on wildcards and Apple's firewall enforces reachability. NBNS provides name registration and conflict handling, not SMB1, NetBIOS session transport, or every legacy Windows browsing feature.

## Service and Telemetry Helpers

The Flash-resident `service` image provides NT hashing, `--print-link-plan`, and the telemetry role. Telemetry posts a heartbeat at startup and every 12 hours, and downloads and runs a signed debug executable only after verifying signed server authorization. See [build/native/README.md](build/native/README.md) for sources, commands, protocol, and cleanup behavior.

Sharing waits at cold start until mode, address ownership, and the relevant
sharing permissions validate. Bridge names and PF heuristics no longer grant
access. After validation, failed rereads retain the latest grants and denials
on unchanged interfaces; new or recreated interfaces wait for validation.
Each native plan loop keeps its latest validated policy in memory. Environment
bind strings are not treated as validated history, and no policy file or text
transport is used.

Router heartbeats include `debug_logging` (Samba or mDNS) and `advertise_afp`;
they no longer send `nbns_enabled`, because NBNS is always on. A short `plan_error` is sent only when the fresh sharing
facts do not validate; a valid plan adds no error field. This is not a live
service-health assertion. The old `ps` registration-status probe and constant
daemon label are removed; registration failures remain in the local logs.

## Current User-Facing Workflow

The intended user flow is:

1. bootstrap the local host
   - [`./tcapsule bootstrap`](./tcapsule)
2. generate local config and enable SSH when needed
   - [src/timecapsulesmb/cli/configure.py](src/timecapsulesmb/cli/configure.py)
3. deploy and reboot
   - [src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py)
4. on NetBSD 4, optionally back up and inspect the firmware, then install the persistent boot hook with `tcapsule flash --patch` and manually power-cycle after a successful write
   - [src/timecapsulesmb/cli/flash.py](src/timecapsulesmb/cli/flash.py)
5. activate older NetBSD 4 devices that do not have the persistent hook or do not auto-start Samba after reboot
   - [src/timecapsulesmb/cli/activate.py](src/timecapsulesmb/cli/activate.py)
6. run local diagnostics
   - [src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py)
7. optionally repair the HDD before redeploying
   - [src/timecapsulesmb/cli/fsck.py](src/timecapsulesmb/cli/fsck.py)
8. remove the payload later if needed
   - [src/timecapsulesmb/cli/uninstall.py](src/timecapsulesmb/cli/uninstall.py)

`tcapsule set-ssh` still exists as an advanced SSH toggle helper, but it is no longer part of the normal setup flow.

`tcapsule configure` writes repo-root `.env` by default; `--config` or `TCAPSULE_CONFIG` can select another path.

Current important `.env` values include:
- `TC_HOST`
- `TC_PASSWORD`
- `TC_SSH_OPTS`
- `TC_INTERNAL_SHARE_USE_DISK_ROOT`
- `TC_SMB_BROWSE_COMPATIBILITY`
- `TC_MDNS_ADVERTISE_AFP`
- `TC_ANY_PROTOCOL`
- `TC_REQUIRE_SMB_ENCRYPTION`
- `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION`
- `TC_FRUIT_METADATA_NETATALK`
- `TC_VFS_AIO_FORK_ENABLED`
- `TC_DEBUG_LOGGING`
- `TC_ATA_IDLE_SECONDS`
- `TC_ATA_STANDBY`
- `TC_CONFIGURE_ID`

Current `.bootstrap` values include:
- `INSTALL_ID`
- optional `TELEMETRY=false`

## macOS App Advanced Checkboxes

The Advanced panel stores these choices in the local device profile. Run **Install / Update Samba** afterward to write the corresponding runtime values to `/mnt/Flash/tcapsulesmb.conf`; changing a checkbox alone does not reconfigure the device. The defaults below are new-profile defaults, not necessarily the checked state shown for an existing saved profile.

### NBNS (always on, no checkbox)

Always enabled when eligible. When the payload and an SMB-eligible IPv4 address are ready, `service discovery` owns Apple's `/sbin/wcifsnd` child and registers the Samba machine name plus `WORKGROUP`. Apple's daemon answers native NBNS traffic on UDP `137` and owns the NetBIOS datagram engine on UDP `138`. Its interface enumeration follows firmware policy after the coarse validated-plan gate; Bonjour-capable clients do not require it.

### Enable rsync

Default: off. Enables `RSYNC_ENABLED=1`, causing the manager to stage the bundled daemon into RAM and expose a writable `shareroot` module on TCP `873`, running as Unix `root:wheel`. The generated rsync configuration has no rsync authentication block, so enable this only on a trusted LAN; the SMB link policy does not restrict the separate rsync daemon.

### Share internal disk root

Default: off. When off, an internal disk share points at `/Volumes/dkN/ShareRoot`; when on, it exposes the whole `/Volumes/dkN` root instead. External disks always use their volume root, and the `.samba4` payload remains hidden from SMB clients through the share veto rule.

### Allow SMB Share Browsing

Default: off. Changes Samba's global `restrict anonymous` value from `2` to `0` so clients that need anonymous browse enumeration can list the server's shares. It does not enable guest file access: shares still use `guest ok = no`, require authentication, and map authenticated users to Unix `root`.

### Advertise AFP over Bonjour

Default: off, and leave it off. macOS 26.x/27 treats a Time Capsule that
advertises AFP as an SMB1-only server and hides it from Finder and Time
Machine. When on, the registrant adds `_afpovertcp._tcp` on port `548` on the
same links as `_smb`, the generated ADISK flags change from SMB-only
`adVF=0x82` to AFP+SMB `adVF=0x83`. Apple's `afpserver` stays running with
either setting; this option controls advertising only. It does not configure
or authenticate an AFP server.

Bonjour records are link-scoped by the device plan (see "Discovery policy and Samba networking"
above): LAN links get the full service set; WAN and guest links get it only in
NAT mode with disks-over-WAN enabled, exactly as Apple's own file servers did;
every other link is isolated. Apple's `_airport._tcp` and host records follow
Apple's rules on every interface. Samba wildcard-listens on IPv4 and IPv6;
Bonjour visibility plus Apple's firewall implement the AirPort Utility policy.

### Use Netatalk metadata

Default: on. Selects `fruit:metadata = netatalk`; turning it off selects `fruit:metadata = stream`. On HFS, the selection is used by the one-shot migrator to choose between conflicting legacy representations, after which `fruit` reads and writes native FinderInfo regardless of this setting. It remains the runtime backend choice for a future non-HFS filesystem.

### Enable debug logging

Default: off. Enables `SMBD_DEBUG_LOGGING=1` and `MDNS_DEBUG_LOGGING=1`, sets Samba to `log level = 10`, and removes the normal managed payload-log size cap. Use it only while troubleshooting because verbose unbounded logs can grow on the disk and add overhead.

### Enable vfs_aio_fork

Default: off. Adds the bounded `aio_fork` VFS module, enables asynchronous I/O for requests of at least one byte, limits SMB2 reads and writes to `128 KiB`, and caps each share at eight forked workers. It is an optional no-pthread I/O profile; leave it off unless testing shows it helps the target workload.

### Allow Any SMB Protocol

Default: off. Omits the generated SMB2-to-SMB3 minimum/maximum protocol lines and leaves protocol selection to Samba's built-in defaults. It is a compatibility escape hatch, not a promise that every historical SMB dialect is available, and it cannot be combined with **Require SMB Encryption**.

### Require SMB Encryption

Default: off. Writes `server smb encrypt = required`, `server min protocol = SMB3_00`, and `server max protocol = SMB3`, so clients must negotiate encrypted SMB3. The app disables **Allow Any SMB Protocol** and **Disable SMB signing and encryption** when this option is selected.

### Disable SMB signing and encryption

Default: off. Writes `server signing = disabled` and `server smb encrypt = off`. This may improve throughput when a client would otherwise require signing, but it weakens SMB transport security and cannot be combined with **Require SMB Encryption**.

## CLI Command Reference

The CLI entrypoint is `tcapsule COMMAND [ARGS...]`. In a normal checkout the first command is usually run through the repo-local launcher:

```bash
./tcapsule bootstrap
```

After bootstrap, use the virtualenv command:

```bash
.venv/bin/tcapsule <command>
```

The top-level command dispatcher supports:
- `activate`
- `api`
- `bootstrap`
- `configure`
- `deploy`
- `discover`
- `doctor`
- `flash`
- `fsck`
- `paths`
- `repair-xattrs`
- `set-ssh`
- `uninstall`
- `validate-install`

Shared command behavior:
- all commands except `api` perform the client version check before running, unless the invocation is only asking for `-h` or `--help`
- commands that read the device config accept `--config PATH`, which overrides `TCAPSULE_CONFIG` and the repo-local `.env`
- commands that can prompt usually accept `--no-input`; in that mode they fail instead of asking for missing input or confirmation
- commands that can make destructive or rebooting changes use `--yes` to skip confirmation in interactive and non-interactive runs
- commands with `--json` do not all use the same output shape; most command JSON is a single final object, while `repair-xattrs --json` emits app-event NDJSON
- for commands where JSON describes a plan, `--json` is intentionally restricted to `--dry-run`

### `bootstrap`

`tcapsule bootstrap` prepares the local host. It validates the selected Python, creates or reuses `.venv`, installs `requirements.txt`, installs the repo into the virtualenv, and verifies required host tools. If `smbclient` or `sshpass` is missing, it attempts host-tool installation through Homebrew on supported macOS versions or through the detected Linux package manager.

Arguments:
- `--python PYTHON`: Python interpreter validated and used when creating a new `.venv`; defaults to the Python running the command. An existing `.venv` is reused with its existing interpreter. The selected interpreter must be Python 3.9 or newer.

This command does not read `.env` for device credentials, but it does create or preserve the local install identity in `.bootstrap`.

### `paths`

`tcapsule paths` resolves the local TimeCapsuleSMB paths and prints the distribution root, config path, state dir, package root, artifact manifest, and deployable artifacts with basic validity status. It is useful when debugging an install that may have been moved, wrapped, or invoked from a different working directory.

Arguments:
- `--config PATH`: resolve paths as though this config file were selected
- `--json`: emit the same path and artifact data as JSON

### `validate-install`

`tcapsule validate-install` checks the repo-only install without touching the device. It validates the local distribution root, state/config path resolution, packaged files, and artifact metadata expected by the app and CLI.

Arguments:
- `--config PATH`: validate using the selected config path for local path resolution
- `--json`: emit `{ "ok": ..., "checks": ... }` and return nonzero if validation fails

### `discover`

`tcapsule discover` browses Bonjour/mDNS for Apple AirPort storage services and prints both raw browse instances and resolved service records. Discovery uses Python zeroconf, not native `dns-sd`, so it remains usable on Linux and in non-macOS diagnostics.

Arguments:
- `--config PATH`: load optional config for telemetry context only; discovery itself does not require `.env`
- `--timeout SECONDS`: Bonjour browse timeout; default is `6`
- `--json`: emit discovered instances and resolved records as JSON
- `--select`: after printing records, prompt for a device number and print only the selected display host

### `configure`

`tcapsule configure` creates or updates `.env`. In interactive mode it attempts AirPort Bonjour discovery, prompts for the SSH target and device password, checks SSH reachability, enables SSH through ACP when needed, probes the device, derives identity/config defaults, and writes the managed config. The password is stored as `TC_PASSWORD` for host-side SSH/ACP access; Samba auth is generated on the device at boot from live AirPort `syPW`.

Arguments:
- `--config PATH`: write/read this config path instead of the default `.env`
- `--no-input`: do not prompt; requires enough arguments or existing config to proceed
- `--password-env NAME`: read the device password from environment variable `NAME`
- `--password-file PATH`: read the device password from a file, stripping trailing newlines
- `--password-stdin`: read the device password from stdin, stripping trailing newlines
- `--host HOST`: set the device SSH target, for example `root@192.168.1.10`; custom SSH ports are rejected here and should be placed in `TC_SSH_OPTS`
- `--skip-discovery`: skip Bonjour discovery and use the supplied or saved SSH target
- `--yes`: approve ACP SSH enablement when SSH is closed
- `--enable-ssh`: enable SSH via ACP if SSH is closed
- `--no-enable-ssh`: fail instead of enabling SSH via ACP if SSH is closed
- `--json`: emit a machine-readable result; requires `--no-input`
- `--force-disable-smb-signing-and-encryption` / `--no-force-disable-smb-signing-and-encryption`: write `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true|false`; `--disable-smb-security` and `--no-disable-smb-security` are aliases

Hidden advanced arguments:
- `--internal-share-use-disk-root` / `--no-internal-share-use-disk-root`: write `TC_INTERNAL_SHARE_USE_DISK_ROOT=true|false`
- `--smb-browse-compatibility` / `--no-smb-browse-compatibility`: write `TC_SMB_BROWSE_COMPATIBILITY=true|false`
- `--mdns-advertise-afp` / `--no-mdns-advertise-afp`: write `TC_MDNS_ADVERTISE_AFP=true|false`
- `--any-protocol` / `--no-any-protocol`: write `TC_ANY_PROTOCOL=true|false`
- `--require-smb-encryption` / `--no-require-smb-encryption`: write `TC_REQUIRE_SMB_ENCRYPTION=true|false`
- `--netatalk` / `--no-netatalk`: write `TC_FRUIT_METADATA_NETATALK=true|false`
- `--enable-vfs-aio-fork` / `--disable-vfs-aio-fork`: writes `TC_VFS_AIO_FORK_ENABLED=true|false`; toggles the bounded `vfs_aio_fork` runtime profile
- `--debug-logging` / `--no-debug-logging`: explicitly enable or disable managed runtime debug logging
- `--ata-idle-seconds SECONDS`: writes `TC_ATA_IDLE_SECONDS`; must be a non-negative integer, with `0` disabling the ATA idle timer
- `--ata-standby SECONDS`: writes `TC_ATA_STANDBY`; must be a non-negative integer, with `0` disabling standby and a blank saved value leaving standby unchanged

`TC_ANY_PROTOCOL=true` cannot be combined with `TC_REQUIRE_SMB_ENCRYPTION=true`. Required encryption also cannot be combined with `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true`; configure rejects either conflict before writing `.env`.

Non-interactive examples:

```bash
TC_PASS='airport-password' .venv/bin/tcapsule configure --no-input --host root@192.168.1.10 --password-env TC_PASS --enable-ssh --yes
printf '%s\n' 'airport-password' | .venv/bin/tcapsule configure --no-input --host root@192.168.1.10 --password-stdin --json
```

### `set-ssh`

`tcapsule set-ssh` is an advanced helper for toggling the firmware SSH debug flag. It uses the configured target from `.env`. If no explicit mode is selected, it preserves the older behavior: enable SSH when closed, or ask whether to disable it when already open.

Arguments:
- `--config PATH`: use a non-default config
- `--enable`: enable SSH via ACP if port 22 is closed; no-op if already open
- `--disable`: remove the `dbug` property over SSH and reboot; no-op if SSH is already closed
- `--status`: only report whether SSH port 22 is reachable; cannot be combined with `--no-wait`
- `--yes`: skip the legacy prompt when SSH is already enabled and no explicit mode was selected
- `--no-input`: fail instead of prompting in legacy mode
- `--no-wait`: after enabling or disabling, return without waiting for the port/reboot verification

Use `configure` for normal first-time setup. Use `set-ssh` only when you intentionally want to manage SSH separately from the main config flow.

### `deploy`

`tcapsule deploy` installs or updates the managed Samba payload on the configured device. It validates the local artifacts, probes device compatibility, selects a writable HFS payload volume, uploads the payload and boot files, writes `/mnt/Flash/tcapsulesmb.conf`, installs the unified service and configuration that generate Samba auth files in RAM during boot or activation, applies permissions, and reboots. On NetBSD 4 devices, deploy checks the runtime after SSH returns and activates it only when firmware startup has not already done so.

Arguments:
- `--config PATH`: use a non-default config
- `--no-wait`: request reboot and return without waiting for SSH or runtime verification
- `--yes`: do not prompt before reboot
- `--no-input`: fail instead of prompting; non-dry-run deploys require `--yes`
- `--dry-run`: build and print the deployment plan without changing the device
- `--json`: emit the dry-run deployment plan as JSON; requires `--dry-run`
- `--allow-unsupported`: continue when the detected device compatibility check is unsupported
- `--enable-rsync`: write `RSYNC_ENABLED=1` so the manager stages and starts the bundled rsync daemon from RAM; the binary and config are uploaded even when this flag is omitted
- `--mount-wait SECONDS`: per-attempt wait for deployment-time `diskd.useVolume` mount guards; default is `30`

Hidden advanced arguments:
- persisted profile settings accept positive/negative overrides for internal-share root, SMB browsing, AFP advertising, protocol/security choices, Netatalk metadata, debug logging, and `vfs_aio_fork`; omitting a pair preserves the saved `.env` value
- `--debug-logging` / `--no-debug-logging`: override saved debug logging for this deploy; enabling increases runtime logging and disables the normal managed log size cap
- `--enable-vfs-aio-fork` / `--disable-vfs-aio-fork`: override the saved bounded `vfs_aio_fork` setting for this deployment

Useful plan modes:

```bash
.venv/bin/tcapsule deploy --dry-run
.venv/bin/tcapsule deploy --dry-run --json
```

### `activate`

`tcapsule activate` manually starts an already-deployed NetBSD 4 payload without uploading files again. If the managed runtime is already ready, it skips re-running `/mnt/Flash/rc.local`; otherwise it stops any running launcher and reruns it.

Arguments:
- `--config PATH`: use a non-default config
- `--yes`: do not prompt before restarting deployed Samba services
- `--no-input`: fail instead of prompting; non-dry-run activation requires `--yes`
- `--dry-run`: print the activation actions without changing the device
- `--json`: emit the dry-run activation plan as JSON; requires `--dry-run`

This command is only supported for NetBSD 4 AirPort storage devices. NetBSD 6 devices should use `deploy` for persistent installs and normal updates.

### `flash`

`tcapsule flash` is the NetBSD 4 firmware-bank helper. By default it is read-only: it backs up and analyzes both flash banks, saves a manifest, and prints the firmware state. Write modes are explicit. `--patch` installs the persistent TimeCapsuleSMB boot hook into the primary bank. `--restore` writes Apple stock firmware to the uniquely selected active bank, or to the primary bank with a warning when both candidates pass active selection.

Arguments:
- `--config PATH`: use a non-default config
- `--read-only`: dump and back up firmware banks without patch planning; this is also the default when no mode is provided
- `--patch`: build and write the TimeCapsuleSMB LOGIN hook patch to the primary bank
- `--restore`: restore the selected candidate bank from Apple stock firmware; when both candidates pass active selection, target the primary bank
- `--check-apple`: check whether the candidate bank or banks match Apple stock firmware
- `--download-only`: connect to the configured NetBSD 4 device, back up and analyze its banks, then download and validate Apple firmware without writing firmware
- `--yes`: do not prompt before `--patch` or `--restore` writes; only valid for write modes
- `--no-input`: fail instead of prompting; write modes require `--yes`
- `--reboot`: after a validated `--restore` write, request a software reboot
- `--no-wait`: with `--restore --reboot`, return after the reboot request without waiting for the device
- `--json`: emit flash analysis and plan JSON; only valid for read-only modes, not `--patch` or `--restore`
- `--backup-dir PATH`: use `PATH` as this run's exact backup directory instead of creating a timestamped directory under the default backup root
- `--force`: with `--patch`, bypass backup/active-candidate preflight and target the primary bank
- `--firmware-template PATH`: use a local Apple `.basebinary` firmware template instead of auto-selecting from Apple's catalog
- `--firmware-version VERSION`: select an Apple firmware version, for example `7.8.1`

Hidden unsupported argument:
- `--poweroff`: currently rejected with an error; patch mode requires a manual power cycle after a validated write

Important mode restrictions:
- `flash --patch --reboot` is rejected; patch mode cannot request a software reboot
- `--reboot` is only valid with `--restore`
- `--no-wait` is only valid with `--restore --reboot`
- `--json` is only valid for read-only flash modes
- patch mode requires `zopfli` gzip support on the host

### `doctor`

`tcapsule doctor` runs local and remote diagnostics without deploying, rebooting, or changing managed configuration. It validates config and local tools, checks artifact presence and checksums, probes SSH/network/runtime state, checks Bonjour and NBNS visibility, runs authenticated SMB listing and temporary CRUD checks, and verifies that Samba xattr state points at persistent storage.

Arguments:
- `--config PATH`: use a non-default config
- `--skip-ssh`: skip SSH reachability and remote checks
- `--skip-bonjour`: skip Bonjour browse/resolve checks
- `--skip-smb`: skip authenticated SMB listing and file-operation checks
- `--no-startup-grace`: show raw startup-window failures instead of collapsing eligible transient failures into one wait-and-retry result
- `--json`: emit one structured final doctor payload

`doctor` is the preferred post-deploy and post-reboot verification command. Its default SMB CRUD checks temporarily create, modify, and remove a hidden `.doctor-fileops-*` directory on a share. A timeout, interruption, or early failure can leave that directory behind; use `--skip-smb` when the diagnostic run must not write through SMB.

### `fsck`

`tcapsule fsck` runs remote `fsck_hfs` against a mounted HFS volume. It mounts/wakes the Apple volumes, selects or prompts for a volume, stops the managed runtime and Apple's file sharing with the same stop actions deploy uses (and aborts if anything will not stop), unmounts the selected disk, runs `fsck_hfs`, and reboots by default.

Arguments:
- `--config PATH`: use a non-default config
- `--yes`: do not prompt before disk repair
- `--no-input`: fail instead of prompting; repair requires `--yes`
- `--no-reboot`: run `fsck_hfs` only and do not reboot afterward
- `--no-wait`: when rebooting, do not wait for SSH to go down and come back
- `--volume VOLUME`: select the HFS volume device, for example `dk2` or `/dev/dk2`; if omitted and multiple mounted volumes exist, interactive mode prompts

Use this only when the disk needs repair before deploy or when doctor/troubleshooting points at filesystem problems.

### `repair-xattrs`

`tcapsule repair-xattrs` is a macOS-side mounted-share repair helper. It scans files and directories on a local SMB mount, diagnoses broken extended-attribute metadata, and safely repairs the known case where `xattr -l` fails and the macOS `arch` flag is present by clearing that flag. Other metadata failures are reported without being treated as the same repair case. It is a targeted cleanup tool, not a general metadata migration.

Arguments:
- `--config PATH`: use a non-default config when auto-detecting the mounted share
- `--path PATH`: mounted SMB share path or subdirectory to scan; if omitted, the command tries to find the mounted SMB share matching `.env`
- `--dry-run`: scan and report only; do not prompt or repair
- `--yes`: repair without prompting
- `--no-input`: do not prompt; use with `--dry-run` or `--yes`
- `--recursive`: scan recursively; enabled by default
- `--no-recursive`: scan only the top-level directory
- `--max-depth DEPTH`: maximum recursive directory depth; must be non-negative
- `--include-hidden`: include hidden dot paths that are normally skipped
- `--include-time-machine`: include Time Machine and bundle-like paths that are normally skipped
- `--fix-permissions`: additionally apply `ugo+rw` to files or `ugo+rwx` to directories that do not already have all corresponding permission bits
- `--verbose`: print detailed diagnostics for detected issues
- `--json`: emit app-event NDJSON; when not using `--dry-run`, this requires `--yes`

Argument restrictions:
- `--dry-run` and `--yes` are mutually exclusive
- `--max-depth` must be non-negative
- the command must run on macOS because it depends on local `xattr` and `chflags`

### `uninstall`

`tcapsule uninstall` removes managed TimeCapsuleSMB files from the configured device. It stops the manager, removes the payload directories from mounted HFS volumes, removes loader files under `/mnt/Flash` and runtime state, and reboots by default so Apple services and the root filesystem return to their clean state. After a waited reboot it verifies that managed files are gone. It does not restore a firmware bank changed by `flash --patch`; use `flash --restore` for that separate operation.

Arguments:
- `--config PATH`: use a non-default config
- `--mount-wait SECONDS`: wait for `diskd.useVolume` mount guards before manual fallback; default is `30`
- `--no-wait`: request reboot and return without waiting for post-uninstall verification
- `--yes`: do not prompt before reboot
- `--no-input`: fail instead of prompting; non-dry-run rebooting uninstalls require `--yes` unless `--no-reboot` is used
- `--no-reboot`: remove files but do not reboot the device
- `--dry-run`: print the uninstall plan without changing the device
- `--json`: emit the dry-run uninstall plan as JSON; requires `--dry-run`

`uninstall` does not re-enable Apple AFP or SMB settings or restore a patched firmware bank; it only removes TimeCapsuleSMB-managed files and runtime state.

### `api`

`tcapsule api` is the structured backend used by the macOS app. It reads one JSON object from stdin, runs the requested operation, and writes app-event NDJSON to stdout. The request must be a JSON object with:
- `operation`: required operation name
- `params`: optional JSON object; defaults to `{}`
- `request_id`: optional request identifier echoed on emitted events

Arguments:
- `--pretty-error`: also write request parsing errors to stderr for local debugging

Known public app operations are `activate`, `capabilities`, `configure`, `deploy`, `discover`, `doctor`, `flash`, `fsck`, `reachability`, `repair-xattrs`, `set-ssh`, `set-telemetry`, `uninstall`, `validate-install`, and `version-check`. The backend also accepts internal non-public operations such as `update-config-settings`. This is not the normal human CLI surface; prefer the direct commands above unless you are integrating with the GUI helper contract.

## Local Test Coverage

`make install` installs `coverage.py` through the `dev` optional dependency. `./tcapsule bootstrap` installs `requirements.txt` and the normal editable package, but does not install the development-only coverage dependency.

Test and coverage entry points:
- `make test` runs C compile checks plus the pytest suite
- `make test-parallel` runs the same C compile checks and the pytest suite through `pytest-xdist`
- `make coverage` runs the pytest suite with branch coverage and prints missing source lines
- `make coverage-html` writes the browsable report to `htmlcov/index.html`
- `make coverage-native` reports native C coverage; see [native checks](build/native/README.md#builds-and-checks)
- `cd macos/TimeCapsuleSMB && swift test` runs the macOS app/helper unit tests; the package supplies the Xcode platform framework search path needed for XCTest/Swift Testing imports

The root `make test` targets do not run the Swift suite; run both the Python/C and Swift entry points when a change crosses the backend/app boundary.

Current defaults and fixed values:
- `TC_INTERNAL_SHARE_USE_DISK_ROOT=false`
- `TC_SMB_BROWSE_COMPATIBILITY=false`
- `TC_MDNS_ADVERTISE_AFP=false`
- `TC_ANY_PROTOCOL=false`
- `TC_REQUIRE_SMB_ENCRYPTION=false`
- `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=false`
- `TC_FRUIT_METADATA_NETATALK=true`
- `TC_VFS_AIO_FORK_ENABLED=false`
- `TC_DEBUG_LOGGING=false`
- `TC_ATA_IDLE_SECONDS=300`
- `TC_ATA_STANDBY=` leaves the standby timer unchanged; set `0` to disable standby
- `TC_SSH_OPTS` includes the legacy SSH algorithms required by AirPort firmware
- docs and examples use SMB username `admin`
- the managed payload directory is fixed at `.samba4`

Samba NetBIOS and Samba server string are derived on the device at runtime from `/usr/bin/acp -q syNm` and `/bin/hostname`; they are not configured in `.env`.

Current validation behavior:
- `TC_HOST`: must be non-empty.
- `TC_PASSWORD`: Doctor, flash, and non-status `set-ssh` operations require a configured value; deploy and activate can prompt interactively when it is absent, while fsck and uninstall allow passwordless SSH key/agent authentication.
- `TC_SSH_OPTS`: is written by `configure` with the legacy SSH options needed for AirPort firmware.
- the managed share, browsing, AFP, protocol/security, Netatalk metadata, `vfs_aio_fork`, and debug settings listed above must contain recognized boolean values.
- `TC_INTERNAL_SHARE_USE_DISK_ROOT`: internal disks use `ShareRoot` by default, and external disks always use the disk root.
- the protocol/security validator rejects required encryption combined with either `TC_ANY_PROTOCOL=true` or `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true`.
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
- reusable workflows live under [src/timecapsulesmb/services/](src/timecapsulesmb/services), with deployment plans/execution under [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy) and device probes/state under [src/timecapsulesmb/device/](src/timecapsulesmb/device)
- the checked-in binaries and build tooling are visible in the repo, so advanced users can swap binaries, rebuild artifacts, or trace the exact boot/runtime layout

## Host-Side Architecture

Current important package areas:
- [src/timecapsulesmb/cli/](src/timecapsulesmb/cli): command entrypoints for `bootstrap`, `paths`, `validate-install`, `discover`, `configure`, `set-ssh`, `deploy`, `flash`, `activate`, `doctor`, `fsck`, `repair-xattrs`, `uninstall`, and the app-facing `api` helper
- [src/timecapsulesmb/app/](src/timecapsulesmb/app): structured API request handling, operation contracts, progress/result events, confirmations, recovery guidance, and app-specific operation adapters
- [src/timecapsulesmb/services/](src/timecapsulesmb/services): reusable configure, deploy, activation, maintenance, storage, reboot, Doctor, and runtime workflows shared by the CLI and app/API entrypoints
- [src/timecapsulesmb/core/](src/timecapsulesmb/core): shared config parsing, defaults, and common models
- [src/timecapsulesmb/transport/](src/timecapsulesmb/transport): local command execution plus SSH command, tunnel, and upload helpers
- [src/timecapsulesmb/discovery/](src/timecapsulesmb/discovery): Bonjour-based device discovery
- [src/timecapsulesmb/integrations/](src/timecapsulesmb/integrations): self-contained Python 3 ACP client for SSH enable/reboot support
- [src/timecapsulesmb/checks/](src/timecapsulesmb/checks): reusable local, network, Bonjour, and SMB verification checks
- [src/timecapsulesmb/device/](src/timecapsulesmb/device): remote probing for device-specific layout, `MaSt` volume parsing, payload-home selection, plus generation / compatibility classification
- [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy): deployment planning, remote actions, upload execution, dry-run formatting, artifact resolution, and post-deploy verification
- [src/timecapsulesmb/assets/](src/timecapsulesmb/assets): packaged boot templates and artifact metadata
- [src/timecapsulesmb/identity.py](src/timecapsulesmb/identity.py): local install identity loaded from `.bootstrap`
- [src/timecapsulesmb/telemetry/](src/timecapsulesmb/telemetry): best-effort client telemetry for user-facing commands
- [macos/TimeCapsuleSMB/](macos/TimeCapsuleSMB): the Swift macOS app, helper launcher, saved device profiles, workflow stores, localized UI, and Swift tests
- [build/](build): maintainer build tooling, including Samba cross-exec record/replay helpers

Developer note:
- [src/timecapsulesmb/cli/context.py](src/timecapsulesmb/cli/context.py) owns shared per-command lifecycle state such as timing, command IDs, result state, and finish handling.
- [src/timecapsulesmb/services/runtime.py](src/timecapsulesmb/services/runtime.py) owns shared `.env` loading, SSH connection resolution, managed-target validation, and compatibility probing; [src/timecapsulesmb/cli/runtime.py](src/timecapsulesmb/cli/runtime.py) provides CLI argument, prompting, rendering, and compatibility-display helpers.
- Normal users should not need these details; they mostly keep command entrypoints smaller and more consistent.

Practical consequence:
- if you want to modify how the box is discovered, start in `discovery/`
- if you want to change shared install behavior, start in `services/deploy.py`; for the action plan and transfer mechanics, inspect `deploy/planner.py` and `deploy/executor.py`
- if you want to change the app contract or progress events, start in `app/` and then follow the matching shared service
- if you want to change the on-device boot behavior, inspect the packaged boot assets and the runtime layout sections below
- if you want to replace binaries or rebuild them, inspect the artifact manifest plus the `build/` tree

## Doctor Command

[src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py) is a local diagnostic helper that does not deploy, reboot, or change managed configuration.

It checks:
- `.env` completeness and invalid `.env` values
- required local tools
- whether the required checked-in binaries exist and match the expected checksums
- deployed release/version metadata in `/mnt/Flash/tcapsulesmb.conf`
- that the managed RAM runtime directory exists
- SSH reachability
- detected device compatibility and payload family
- managed `smbd`, the discovery role (Apple `mDNSResponder` on UDP `5353`, loopback `diskd`, a valid plan, and eligible native NBNS ready through the exact owned `wcifsnd` child), and enabled/disabled rsync readiness
- a shared USB printer: when `acp -A prni` lists a plugged-in printer, Apple's `printd` must advertise it (`_pdl-datastream`/`_riousbprint`/`_printer`/`_ipp`) — we never touch printd, so this guards the one thing v3.1.0 changed for printers (v3.0 re-advertised them itself because it killed the responder); skipped when no printer is attached
- active Samba version, RAM-staged binary/config/auth paths, manager state, mounted share volumes, and required service sockets
- discovered IPv4/IPv6 SMB endpoints, client-local link-local scopes, route testability, and bounded family-specific TCP 445 reachability
- advertised Bonjour instance name
- advertised Bonjour host label
- `_smb._tcp`, `_adisk._tcp`, `_device-info._tcp`, and `_airport._tcp` target consistency for the active instance
- `_adisk._tcp` Time Machine flags, advertised disk rows, active share coverage, and target-host agreement with `_smb._tcp`
- that advertised host addresses match the reachable runtime target
- active Samba NetBIOS name
- active Samba share names
- SMB reachability
- `_smb._tcp` browse and resolve
- NBNS name resolution when a reachable IPv4 SMB address and NetBIOS name are available
- authenticated `smbclient -L` listing
- authenticated SMB CRUD operations via `smbclient`
- that at least one active Samba share is present in the authenticated SMB listing
- that the configured non-HFS `xattr_tdb:file` fallback in `/mnt/Memory/samba4/etc/smb.conf` points at persistent storage instead of the ramdisk; the HFS backend does not require the TDB file to exist

It does not:
- deploy
- reboot
- change managed device configuration

Its authenticated SMB CRUD checks do temporarily write to a share. They normally remove their hidden `.doctor-fileops-*` test directory, but an interruption, timeout, or early failure can leave it behind. Use `--skip-smb` when the diagnostic run must not perform SMB writes.

Current output behavior:
- in normal human-readable mode, checks are printed as they complete rather than being buffered until the end
- `--json` still emits one structured payload at the end
- during the first `180` seconds after the manager starts, eligible transient startup failures are demoted to context and replaced by one actionable wait-and-retry failure; `--no-startup-grace` disables that transformation

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

[src/timecapsulesmb/cli/repair_xattrs.py](src/timecapsulesmb/cli/repair_xattrs.py) is a macOS-side repair and diagnostic helper for files and directories whose SMB extended-attribute metadata became unreadable.

This was added after observing files on the mounted Samba share where:
- normal POSIX permissions looked fine
- TextEdit could open the file but could not save it back in place
- `xattr -l <file>` failed with `Invalid argument`
- `ls -lO@ <file>` showed the macOS `arch` file flag

The automatic xattr repair is intentionally narrow. The command scans files and directories, reports broader xattr and file-data failures, and automatically clears the `arch` flag only when `xattr -l` fails and that flag is present:

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
- when `--fix-permissions` is selected, adds `ugo+rw` to affected files or `ugo+rwx` to affected directories

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
  - else fails with `MaSt found N deployable HFS volume(s), but deploy could not write to any of them.`
- computes the device-specific runtime and payload paths from that payload home
- builds the plan and renders configuration locally before stopping services
- confirms installation and reboot before stopping or replacing managed software (unless `--yes` is used)
- stops current and historical supervisors before their workers and verifies they have stopped
- disables `rc.local` and removes an explicit inventory of replaceable software; preserves data, metadata, quarantines, logs, SSH keys and Apple settings
- inventories active metadata in current and older payload layouts before software deletion
- stops writers, disables and flushes `rc.local`, then rechecks the inventory
- uploads the migrator to RAM only when a TDB exists, and copies merged metadata before replacing software
- removes known obsolete programs from all detected payload homes while preserving metadata and logs
- checks actual free Flash space after cleanup, including a small margin
- uploads checked-in `smbd` and `rsync` to the payload, and the unified `service` to Flash
- uploads `rsyncd.conf`, `boot.sh`, and `dfree.sh`
- retains old `tcapsulesmb.conf` until metadata cleanup succeeds or reports accepted partial migration
- installs new `/mnt/Flash/tcapsulesmb.conf` and enables `rc.local` last
- does not upload password-derived Samba auth files; runtime staging generates RAM auth from live AirPort `syPW`
- automatically runs Apple’s native NBNS service when eligible
- disables rsync by default while keeping its HDD payload installed:
  - `RSYNC_ENABLED=0` in flash config unless `--enable-rsync` is used
- verifies transfer sizes and applies file and directory permissions
- verifies and flushes the replacement payload before migration cleanup can retire exported metadata
- uploads `rc.local` last, then runs sync, waits ten seconds, and syncs again; any error stops deployment
- reboots after every successful install; rejecting confirmation leaves the installed software untouched
- supports rerunning an interrupted installation through the same sequence, without rollback state
- verifies managed runtime readiness after reboot:
  - managed `smbd` on TCP `445`
  - Apple `mDNSResponder` running and alone on UDP `5353`, `diskd` on loopback, and `service discovery` with a valid plan and matching native-NBNS readiness state
  - enabled rsync from RAM on TCP `873`, or disabled rsync with no live daemon
- on NetBSD 4, deploy uploads the NetBSD 4 artifact set, reboots to clear RAM runtime state, waits for SSH to return, and runs `/mnt/Flash/rc.local` only when firmware autostart is missing; otherwise it waits for the firmware-started runtime

Full Bonjour browse/resolve checks, authenticated SMB listings, SMB CRUD checks, share checks, NBNS checks, xattr persistence checks, and deployed-version checks are handled by `doctor`.

Current compatibility behavior:
- little-endian NetBSD 6 devices are accepted for the current `netbsd6_samba4` payload family
- NetBSD 4 devices use `netbsd4le_samba4` or `netbsd4be_samba4` according to detected ELF endianness
- `configure` reuses the same classification logic for compatibility and displayed device identity

NetBSD 4 activation behavior:
- `tcapsule deploy` uploads the NetBSD 4 payload, reboots, waits for SSH, reads `/etc/rc.d/LOGIN`, and runs `/mnt/Flash/rc.local` only when the firmware hook is missing; then it verifies managed `smbd` plus the discovery role
- Deployment always reboots. It stops current and historical managed processes, removes owned software, copies directly to final paths, verifies and flushes the payload, completes metadata migration, then writes `rc.local` last and flushes again before rebooting. Rerunning an interrupted installation finishes it; user data, pending metadata, quarantines and logs are preserved. `--no-wait` returns after requesting reboot without claiming runtime verification. Legacy API `no_reboot=true` requests are rejected before mutation.
- `tcapsule activate` starts an already installed runtime without re-uploading files
- Apple `mDNSResponder` is never stopped; the manager moves Apple's `diskd` to loopback and `service discovery` registers through the daemon
- tested 1st-generation NetBSD 4 hardware without a firmware boot-hook patch does not persist an `/etc` hook and therefore needs manual activation after reboot
- other NetBSD 4 generations may auto-start if their firmware runs `/mnt/Flash/rc.local` early in boot, but that is not yet proven
- `activate` skips running `/mnt/Flash/rc.local` when `smbd`, the discovery role, and any enabled rsync are already ready

The current password flow is:
- `TC_PASSWORD` is retained for app/CLI SSH and ACP access
- runtime staging reads `/usr/bin/acp -q syPW`, generates an NT hash through `service`, and writes RAM-only `smbpasswd`
- no deploy-time password-derived auth file is persisted to the hard disk

This gives a near-enough user experience:
- same password as the current AirPort device password
- password changes made in AirPort Utility are picked up after reboot/runtime staging
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
- at runtime, Samba writes `log.smbd` under `<payload>/logs/`, sets `max log size = 0`, and enables `log level = 10`.
- `log.smbd` is normally capped at `128 KiB` and other payload logs are trimmed to their last `16 KiB` past `32 KiB`; `--debug-logging` leaves them unbounded.
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
- discovers and mounts the current `MaSt` HFS volumes, then removes `.samba4` from every mounted candidate rather than assuming one fixed payload disk
- if no HFS volume is mounted, still removes loader files and runtime state while reporting that only flash/runtime cleanup was possible
- removes loader files under `/mnt/Flash`, the RAM runtime tree, and compatibility symlinks; it does not restore a firmware bank changed by `flash --patch`
- runs remote uninstall actions sequentially over SSH
- prompts before reboot by default
- supports human and JSON dry-run plans, `--mount-wait`, `--no-reboot`, and request-only `--no-wait` reboot behavior
- after a waited reboot, verifies that every planned payload directory, flash loader, RAM path, and compatibility symlink is absent

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
- [bin/service/service](bin/service/service)
- [bin/service-netbsd4le/service](bin/service-netbsd4le/service)
- [bin/service-netbsd4be/service](bin/service-netbsd4be/service)
- [bin/rsync/rsync](bin/rsync/rsync)
- [bin/rsync-netbsd4le/rsync](bin/rsync-netbsd4le/rsync)
- [bin/rsync-netbsd4be/rsync](bin/rsync-netbsd4be/rsync)
- [bin/xattr-migrate/xattr-hfs-migrate](bin/xattr-migrate/xattr-hfs-migrate)
- [bin/xattr-migrate-netbsd4le/xattr-hfs-migrate](bin/xattr-migrate-netbsd4le/xattr-hfs-migrate)
- [bin/xattr-migrate-netbsd4be/xattr-hfs-migrate](bin/xattr-migrate-netbsd4be/xattr-hfs-migrate)

Current active deploy artifact sizes (stripped bytes, v3.1.1):
- NetBSD 6 `smbd`: about `9.8M`
- NetBSD 6 `service`: `362,300`
- NetBSD 6 `rsync`: about `1.0M`
- NetBSD 4 little-endian `smbd`: about `9.8M`
- NetBSD 4 big-endian `smbd`: about `9.8M`
- NetBSD 4 little-endian `service`: `321,540`
- NetBSD 4 big-endian `service`: `320,940`
- NetBSD 4 little-endian `rsync`: about `878K`
- NetBSD 4 big-endian `rsync`: about `872K`

The unified service lives on `/mnt/Flash`; `smbd` and optional rsync are
RAM-staged from the payload. Deploy checks free Flash space after removing
old software, verifies the replacement, removes legacy
standalone binaries, and flushes that cleanup.

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
  - [build/downloadrsync.sh](build/downloadrsync.sh)
  - [build/rsync.sh](build/rsync.sh)
  - [build/service.sh](build/service.sh)
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
  - [build/downloadrsync.sh](build/downloadrsync.sh)
  - [build/rsyncoldle.sh](build/rsyncoldle.sh)
  - [build/rsyncoldbe.sh](build/rsyncoldbe.sh)
  - [build/serviceoldle.sh](build/serviceoldle.sh)
  - [build/serviceoldbe.sh](build/serviceoldbe.sh)

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

### Running the unified service from the HDD would be unsafe

The manager and discovery sockets must survive HDD unmounts. The runtime starts
the unified image from `/mnt/Flash` instead of the HDD or RAM disk, which saves
RAM headroom and avoids depending on the HDD staying mounted.

### Apple’s SMB advertisement path is not a harmless metadata layer

If Apple’s own SMB/AFP stack is allowed to reclaim its native path, Finder may reconnect through Apple services rather than our Samba. Apple's `diskd` registers those names unconditionally.

Before v3.1.0 we killed Apple's `mDNSResponder` and ran our own responder. Since v3.1.0 we keep Apple's daemon (it owns `_airport`, `_device-info` and the host records, and nothing respawns it), relaunch `diskd` on loopback so its registrations never reach the LAN, and register our own names through the daemon's IPC.

### The Time Capsule firmware is missing small utility commands you might expect

Examples encountered during debugging:
- no `grep`
- no `dirname`
- no `find`
- no `strings`

Shell scripts must be written very conservatively.

### Apple mDNSResponder facts ledger (verified on devices, 2026-09-16)

The v3.1.0 move from our own responder to Apple's on-device `mDNSResponder`
rests on these measured facts. Numbers match the v3.1 implementation guide.

| # | Fact |
| --- | --- |
| F1 | Both lanes ship `mDNSResponder-397.32` inside one crunched static binary (`/sbin/mDNSResponder`, `diskd`, `printd`, `afpserver`, `wcifsfs`, … are hard links). The IPC is the standard `dns_sd` Unix-socket protocol, `VERSION 1`, at `/var/run/mDNSResponder` (created only while the daemon runs). |
| F2 | Apple's client stub from tag `mDNSResponder-379.38.1` (`build/native/dnssd/`) compiles unchanged with `-D_DNS_SD_LIBDISPATCH=0` on all three lanes (gcc 4.1.2 on NetBSD 4) and registers/browses against the 397.32 daemon; cost ≈60 KB over a hello-world. |
| F3 | The daemon's interface indexes are the kernel's (`bridge0` = `ifconfig … scopeid`). `interfaceIndex=0` means all interfaces. |
| F4 | Apple's kernel `struct if_msghdr` is 152 bytes on NetBSD 4 while the SDK's is 144, so libc `getifaddrs()` reads the `AF_LINK` sockaddr from inside `if_data`: names are garbage and `if_nametoindex()` returns 0. Walking `sysctl(NET_RT_IFLIST)` ourselves and locating the `sockaddr_dl` by `sdl_family==AF_LINK && sdl_index==ifm_index` yields correct names and indexes. On NetBSD 6 the messages are `RTM_VERSION 4` (`RTM_IFINFO` 0x14, 24-byte `ifa_msghdr` with the index at offset 16, 8-byte `RT_ROUNDUP`); on NetBSD 4 they are version 3 (`RTM_IFINFO` 0xf, 20-byte header, index at 12, 4-byte roundup). Fixtures: `tests/native/fixtures/iflist/`. |
| F5 | Under the Apple stack `ACPd` registers `_airport._tcp`; `diskd` registers `_smb._tcp`, `_adisk._tcp,_airport` and `_afpovertcp._tcp`; the daemon itself registers `_device-info._tcp` (`model=` from `/etc/mdnsd.conf`); `printd` registers printers. `wcifsfs`, `wcifsnd`, `afpserver` register nothing. |
| F6 | `diskd` registers unconditionally: killing `wcifsfs`/`wcifsnd`/`afpserver` closes the ports but leaves the records. ACPd respawns none of them. |
| F7 | `diskd` is load-bearing: it populates `acp -q MaSt` and serves `acp rpc diskd.useVolume`. |
| F8 | `/sbin/diskd -i lo0 -d local.` relaunched by us still serves MaSt and `diskd.useVolume` while its `_smb`/`_adisk`/`_afpovertcp` stay on loopback. |
| F9 | Registering a name another client already holds auto-renames to "Name (2)" unless `kDNSServiceFlagsNoAutoRename` is passed (then the callback reports `kDNSServiceErr_NameConflict`). |
| F10 | Apple's host records on the LAN: fe80 plus every IPv4 including 169.254, no GUA. Hostname `AirPort-Time-Capsule.local.` (mixed case); SRV targets of our registrations are that hostname automatically. |
| F11 | Killing the daemon is unrecoverable without a reboot: ACPd never respawns it and a hand-started daemon lacks `_airport`. The runtime must never kill it. |
| F12 | The `.env.backup4` device is **little-endian** (its Apple ELF is LSB; it runs the `bin/service-netbsd4le` build). The UK device is presumably the BE one — verify with `file` on first contact. |
| F13 | `/etc/mdnsd.conf` (RAM root, regenerated each boot) carries `Hardware TimeCapsule6,116` / `TimeCapsule8,119`, `Software 7.8.1` / `7.9.1`, `PrimaryIPv4Interface bridge0`. |
| F14 | Samba's IPv6 `interfaces=` tokens must use the embedded-scope form `fe80:<index hex>::…/64`; Apple's pf opens 445/139/137/138/548 on the WAN iff `usbF & 0x8` in NAT mode; router mode is `(raNA,raDS)`: `(0,0)` bridge, `(0,1)` DHCP-only, `(1,1)` NAT; the guest bridge owns `gnRo`. |
| F15 | Device shell quirks: NetBSD 4 `sed` has no `\|` alternation; `reboot`, `ifconfig` need full paths in non-login shells; `/etc` edits do not persist; `/mnt/Memory` is the 15 MB RAM staging area; `/mnt/Flash` is ≈1 MB. |

### Non-root Unix identity handling is risky

Earlier Samba attempts on this firmware ran into privilege-switch and identity issues with non-root mappings.

That is why the current authenticated design still maps to `root`.

## Known Risks And Caveats

- This is still LAN-only software.
- The current authenticated design still maps file access to `root`.
- `/mnt/Memory` is tight; only about `1-2 MiB` may remain free after staging.
- The repo still assumes AirPort storage firmware behavior such as:
  - AirPort-style IPv4/interface layout
  - HFS partition identifiers beginning with `dk`, discovered through Apple `MaSt` metadata
  - the internal-volume `ShareRoot` layout
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
- can use the persistent firmware boot-hook patch on NetBSD 4, or be manually reactivated after reboot on tested unpatched gen1 hardware
- advertises itself over Bonjour
- authenticates with the configured password; docs and examples use SMB username `admin`
- serves the internal disk through Samba 4.25.0rc2
- supports Time Machine via `vfs_fruit`

The main remaining “nice to have” work is polish, not core functionality.
