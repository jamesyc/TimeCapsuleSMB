# TimeCapsuleSMB detail reference

This file covers what runs on the Time Capsule, what each setting and command does, and what to do when Apple's firmware fails. [README.md](README.md) has the quick start. Firmware internals for maintainers are in [build/native/README.md](build/native/README.md#firmware-facts).

## What works today

TimeCapsuleSMB works end to end on the AirPort Time Capsule:
- static Samba 4.25.0, built from NetBSD 7 sources for NetBSD 6-era AirPort storage devices and from NetBSD 4 sources for older ones
- one static `service` image with manager, discovery, telemetry, and diagnostic roles
- startup from `/mnt/Flash/rc.local` at boot
- a native manager for `smbd`, discovery, telemetry, and optional rsync
- SMB on port `445`
- Finder info, extended attributes and resource forks stored natively on HFS, where Apple's AFP server sees them too
- a deploy-time migrator for legacy `xattr.tdb` records and `._` AppleDouble files
- Bonjour records for `_smb._tcp`, `_adisk._tcp`, and `_afpovertcp._tcp` when AFP advertising is on; Apple's `_device-info._tcp`, `_airport._tcp` and USB printer records are left alone
- SMB logins with the AirPort device password (`/usr/bin/acp -q syPW`); the docs use the username `admin`
- no guest access
- device compatibility detection during deploy
- `tcapsule activate` to start the runtime by hand on NetBSD 4
- `tcapsule fsck` for disk repair
- `tcapsule wipe-internal-disk` to format the internal disk, including disks AirPort Utility's Erase Disk cannot format
- `tcapsule uninstall` to remove the managed files; `tcapsule flash --restore` restores a patched firmware bank

Tested so far:
- NetBSD 6 works end to end and starts on its own after a reboot.
- 1st-generation NetBSD 4 hardware without the firmware boot-hook patch works, with `tcapsule activate` after each reboot.
- Other NetBSD 4 generations without the patch may start on their own if their firmware runs `/mnt/Flash/rc.local` early in boot. This is not confirmed.

What clients see:
- `_smb._tcp`, and `_adisk._tcp` for Time Machine
- `_airport._tcp` for AirPort Utility, published by Apple's ACPd and never touched by us
- NBNS name answers from Apple's `wcifsnd` when the network allows it
- a Bonjour name managed by Apple's mDNSResponder, which also handles conflict renames; the Samba server string comes from the AirPort name (`syNm`)
- a Samba NetBIOS name from `/bin/hostname`, falling back to `syNm`
- one share per Apple `MaSt` volume, at `smb://<advertised-host>.local/<share name>`, with names cleaned up and made unique

Logins:
- any SMB username works with the AirPort device password; the docs use `admin`
- at boot, the runtime writes a `root` Samba account hash to RAM and maps every incoming username to Unix `root`
- files are accessed as `root`, because non-root identities failed to switch privileges on this firmware
- a password changed in AirPort Utility takes effect at the next boot or activation

## Device profile

Target families:

- NetBSD 6.x `evbarm`: 5th-generation Time Capsules and AirPort storage devices of the same era
- NetBSD 4.x `evbarm`: older little-endian AirPort storage devices
- NetBSD 4.x `armeb`: older big-endian AirPort storage devices
- AirPort Extremes with USB storage use the same deploy and runtime, with less testing than Time Capsules

Every generation shares these limits:
- the root filesystem is tiny
- flash is tiny
- `/mnt/Memory` holds about `16 MiB`
- the runtime has to fit in RAM, and its lock and cache databases grow while clients are connected

Mount points:
- `/` on `/dev/md0a`
- `/mnt/Flash` on `/dev/flash2a`
- `/mnt/Memory` on `tmpfs`
- the internal HDD, usually `/dev/dk2` or `/dev/dk3`, mounted by Apple at `/Volumes/dk2` or `/Volumes/dk3`

Sizes measured on a device:
- `/`: about `15.5 MiB`, about `4.7 MiB` free
- `/mnt/Flash`: about `1 MiB`, about `933 KiB` free
- `/mnt/Memory`: `16 MiB`, with little left once Samba is staged
- `/Volumes/dk2`: the 2 TB data disk

Most design decisions in this repo follow from these limits.

Deploy picks the payload by NetBSD major version and ELF byte order. The reported architecture and AirPort model only narrow the device names it shows.
- little-endian NetBSD 6.x: `netbsd6_samba4`, for 5th-generation Time Capsules and AirPort devices of that era
- little-endian NetBSD 4.x: `netbsd4le_samba4`, for 3rd and 4th-generation Time Capsules and 3rd to 5th-generation AirPort Extremes
- big-endian NetBSD 4.x: `netbsd4be_samba4`, for 1st and 2nd-generation Time Capsules and AirPort Extremes
  - tested 1st-generation hardware without the firmware boot-hook patch needs `activate` after each reboot
  - other generations may start on their own if their firmware runs `/mnt/Flash/rc.local`; this is not confirmed

## Why the architecture looks like this

### Flash is too small

The flash filesystem cannot hold Samba.

### Root is too small

The root filesystem cannot hold it either.

### RAM is too small to keep anything

`/mnt/Memory` holds about `16 MiB`, and the staged Samba runtime takes most of it. Programs can run from it, but nothing there survives a reboot.

### The HDD is large but can disappear

The device can mount the internal HDD itself and read and write it freely. Apple may unmount or sleep the disk later, though, so `smbd` cannot run from `/Volumes/dk2`.

### Where everything lives

- on the HDD, kept across reboots:
  - `/Volumes/dkX/.samba4/smbd`
  - `/Volumes/dkX/.samba4/rsync`
  - `/Volumes/dkX/.samba4/rsyncd.conf`
  - `/Volumes/dkX/.samba4/private/`
  - `/Volumes/dkX/.samba4/private/xattr.tdb`
  - `/Volumes/dkX/.samba4/cache`
  - `/Volumes/dkX/.samba4/logs/`
- on flash, a small boot hook:
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/boot.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/service`
  - `/mnt/Flash/tcapsulesmb.conf`
- on the RAM disk, rebuilt at each boot:
  - `/mnt/Memory/samba4`
  - `/mnt/Memory/debug` and `/mnt/Memory/debug.sig`, for a signed debug program when one is sent
  - `/mnt/Locks`

The disk holds the files, programs run from RAM, and flash holds only the small files that must always be mounted.

Names:
- `.samba4` is the payload directory on the HDD, and its name is fixed
- the RAM runtime is always at `/mnt/Memory/samba4`
- share names are not configured; the runtime builds them from Apple's `MaSt` volume names

## Why Samba 4.8, then 4.25.0

Samba 4.8 was the first version that worked as a Time Machine target on this hardware. The current release ships Samba 4.25.0.

### Samba 3

Samba 3.x proved the device could serve files, and its binary was a small 6 MB. But directory listing failed on NetBSD 6, so `ls` did not work in the share. Samba 3.x was also the first version with SMB2, and that support was incomplete and buggy.

### Samba 4.0

I tried 4.0 because its SMB2 support should have been better than 3.x, but it had the same directory listing bug. It was much harder to compile than 3.x and much easier than 4.2-4.8. That made it a good stepping stone, because compiling 4.8 from scratch at first drove me crazy.

### Samba 4.2

Samba 4.2 built, but it crashed on the device with a `talloc` / `loadparm` use-after-free on the first client session.

The NetBSD 10 toolchain also produced directory API calls that the NetBSD 6 box did not support.

### Samba 4.3

Samba 4.3 ran as an authenticated network share without bugs, but it could not be a Time Machine target. It proved the architecture and deployment model; 4.8 was the first version whose shares worked with Time Machine.

### Samba 4.8

Samba 4.8 was the first stable target, with a working Time Machine stack through `vfs_fruit`.

### Samba 4.25.0

Samba 4.25.0 is the shipped version. It keeps the static-module build and uses the newer `samba4x` build lanes.

The shipped build includes:
- `catia`
- `fruit`
- `streams_xattr`
- `acl_xattr`
- `xattr_tdb`
- `aio_fork`, on by default, with at most two children per client connection and share; `TC_VFS_AIO_FORK_DISABLED=true` leaves it out
- `fruit:time machine = yes`

## NetBSD 6 build path

The Time Capsule runs NetBSD 6, so the first builds used NetBSD 6 sources. They failed: the NetBSD 6 tree cannot produce earmv4 output. I presume Apple used its own toolchain.

### NetBSD 10 build path

My VM ran NetBSD 10. Static NetBSD 10 binaries ran on the device and worked for Samba 3.x, but later tests showed that important directory APIs failed on the Time Capsule, so NetBSD 10 could not build a full Samba.

### NetBSD 7 build path

The first working build used:
- the NetBSD 7 source tree
- a static `earmv4` build
- Samba 4.8.x

It builds reproducibly, runs on the Time Capsule, serves files, and supports Time Machine through `vfs_fruit`.

The shipped Samba 4.25.0 uses the same NetBSD 7 / NetBSD 4 SDK split. The build scripts are under [build/](build); see [What the build pipeline produces](#what-the-build-pipeline-produces).

## Mac metadata on HFS

Finder info, tags, other Mac extended attributes, and resource forks are stored on the HFS disk the same way Apple's AFP server stores them, so a file's metadata is the same over SMB, AFP and SSH. The Samba module stack is:

```text
catia → fruit → streams_xattr → acl_xattr → xattr_tdb
```

On HFS, `fruit` maps `AFP_AfpInfo` to the native FinderInfo and `AFP_Resource` to the real resource fork (`file/..namedfork/rsrc`). `xattr_tdb` writes other streams as native HFS xattrs and never opens `xattr.tdb`. Apple's firmware stores at most 3,802 bytes in one HFS attribute, and a larger Apple xattr is refused. FAT32 volumes are not supported.

### Migrating older metadata

Releases before native storage kept Mac metadata in `.samba4/private/xattr.tdb` and in `._filename` AppleDouble files. Deploy migrates both. Boot and disk hotplug never do.

- Deploy stops every writer, Apple's `afpserver` included, and copies the metadata into native HFS storage. After it installs the new software, it verifies each value, removes the migrated `._` files and retires the old database.
- An interrupted deploy can be rerun: the old files are only read until their values are verified. When databases disagree about an attribute, the newest file wins.
- A disk that is not attached keeps its rows in the database. Attach it and deploy again to migrate them.
- `xattr.tdb.migration-progress.json`, beside the database, lists the finished volumes so later deploys skip them. If it is lost, the next deploy scans those volumes again.
- Rows for files that are gone from a fully scanned disk are kept: the database is renamed `xattr.tdb.orphaned.N`. Only Uninstall and Wipe Internal Disk delete these files.
- A value too large for HFS stays where it was, in a set-aside database or in its `._` file, which Samba hides. macOS sandbox containers carry such values (8-41 KB `com.apple.data-container-personality`). Deploy lists them when it finishes.
- The logs are `.samba4/logs/xattr-migration-copy.log` and `xattr-migration-cleanup.log`. A damaged `._` file or database stops the deploy before anything is changed.

## How we use Apple's mDNSResponder

The device's own `mDNSResponder` is the only mDNS responder on the device, and TimeCapsuleSMB never kills it: ACPd does not restart it, so only a reboot would bring it back. Our `service discovery` role registers our records through the daemon's `dns_sd` socket.

Apple's `diskd` registers `_smb._tcp`, `_adisk._tcp` and `_afpovertcp._tcp` for Apple's own file servers, which would send Finder to Apple's SMB and AFP. `diskd` also provides the disk list (`acp -q MaSt`) and mounts volumes (`diskd.useVolume`), so it has to keep running. The manager starts it as `/sbin/diskd -i lo0 -d local.`, which keeps both of those working and limits its registrations to loopback.

`boot.sh` replaces `/sbin/diskd` with a guard that lets only that loopback start through. A second diskd started by ACPd would break the first one's RPC names until the next reboot. `/sbin/wcifsnd` gets a guard too, so ACPd cannot take the NBNS ports from the `wcifsnd` that discovery runs. Uninstall leaves both guards in place until the next reboot brings back the stock daemons.

| Process | Owner | Role |
| --- | --- | --- |
| `/sbin/mDNSResponder -d` | Apple (child of ACPd) | the only responder: host `A`/`AAAA` per interface, `_airport` (via ACPd), `_device-info`, printers (via `printd`), and everything we register. Never killed. |
| `ACPd` | Apple | registers `_airport._tcp` and follows the AirPort Utility WAN switches; serves `acp -q`/`acp rpc` |
| `/usr/libexec/diskd -i lo0 -d local.` | Apple binary, started by the manager | disk list (`MaSt`), `diskd.useVolume`, spin-down; its `_smb`/`_adisk`/`_afpovertcp` stay on loopback |
| `printd` | Apple | printer discovery and `_riousbprint`/`_pdl-datastream` registration |
| `wcifsfs` | Apple | Apple's SMB server, always stopped so Samba owns SMB |
| `/usr/libexec/wcifsnd` | Apple binary, child of `service discovery` | NBNS on UDP `137`/`138`; runs only while the network allows NBNS |
| `afpserver` | Apple | kept running whatever the AFP advertising setting |
| `service discovery` | ours, `/mnt/Flash/service` | registers Bonjour records through mDNSResponder and runs the `wcifsnd` child |

Who publishes what:

| Service | Source | Interfaces |
| --- | --- | --- |
| `_smb._tcp` (port 445, empty TXT) | ours | every link allowed file sharing |
| `_adisk._tcp,_airport` (port 9, `sys=waMA=…,adVF=0x1010` + one `dkN=adVF=…,adVN=…,adVU=…` per disk) | ours | same as `_smb` |
| `_afpovertcp._tcp` (port 548) | ours, only with `MDNS_ADVERTISE_AFP=1` | same as `_smb` |
| `_airport._tcp` | Apple (ACPd) | Apple's rules |
| `_device-info._tcp` (`model=TimeCapsule6,116` …) | Apple | Apple's |
| host `A`/`AAAA` | Apple | fe80 + every IPv4 incl. 169.254, no GUA |
| printers | Apple (printd) | Apple's |

The hostname is Apple's (`AirPort-Time-Capsule.local`, from `syDN`, else `syNm`, else `Base-Station-<MAC>`), and our records point at it.

### Where file sharing is visible

Discovery uses the rules Apple's own file servers used. The LAN gets SMB and ADisk, plus AFP when enabled. WAN and guest links get them only in NAT mode with "Share disks over WAN" turned on in AirPort Utility. The guest network never gets file sharing, and any other link gets nothing. At startup nothing is shared until those settings have been read; if a later read fails, the last good result stays. `service --print-link-plan` prints the current plan.

Samba listens on every IPv4 and IPv6 address, including scoped IPv6 link-local. Bonjour controls where `_smb` is visible, and Apple's firewall controls whether WAN and guest clients can reach it.

## Bonjour discovery boundaries

`timecapsulesmb.discovery.bonjour.discover_snapshot_merged_detailed()` returns one normalized record per service instance, so a device with three services gives three records. Each service type has its own job:
- `_airport._tcp.local.` identifies the Apple device, and configure lists devices from it alone
- `_smb._tcp.local.` identifies the Samba service, and doctor's Bonjour checks use it; doctor also reads `waMA` from the `_airport`/`_adisk` TXT to find the device's own host
- `_device-info._tcp.local.` can have the same name, hostname and IP as `_smb._tcp.local.`, and stays a separate raw record

Do not merge `_airport`, `_smb` and `_device-info` records inside `bonjour.discover_snapshot_merged_detailed()`. A merged record has one name and hostname with several meanings, and configure and doctor then print duplicate-looking or misleading lines. Callers filter the raw results by the service prefix they need: `_airport` for configure, `_smb` for doctor.

## Bonjour names

Apple's rules, read from the firmware:

- The device's Bonjour name is its AirPort name (`syNm`) when that fits in 63 bytes. A longer name gets a name Apple builds from the model and MAC. `_airport`, `_smb` and `_adisk` share that one name and are renamed together.
- When another device on the network already uses the name, Apple renames this one "Name (2)" and its host `Name-2`. Only ASCII letters ignore case: "Home" and "home" conflict, "Été" and "été" do not.
- An AirPort does not save that rename. At its next restart or network change it goes back to its original name, so two AirPorts with the same name can swap names, and Time Machine can reach the wrong one. A Mac does save its renames. Give each AirPort its own name in AirPort Utility.

Doctor warns when another device advertises this device's name. A renamed name that nobody else uses is reported for information only.

## Saved device identity

Each saved device is identified by its AirPort MAC address (`TC_AIRPORT_MAC`). Its name and address can change. Every command first checks that the device at the saved address (`TC_HOST`) has that MAC. If the device has moved, the command finds it by MAC over Bonjour and uses the new address for that run; the app offers to save it, and the CLI says to run `tcapsule configure`. A different AirPort at the saved address is refused (`device_identity_mismatch`), so a neighbor's AirPort with the same password is never reconfigured. Adding a device the app already has stops with `device_already_saved`, and the app continues with the saved profile.

## Registered mDNS records

- `boot.sh` prepares directories, installs the diskd and wcifsnd guards and the locks filesystem, then runs `service manager`, which starts Apple's loopback `diskd`
- the manager starts `/mnt/Flash/service discovery` with the Samba name and one ADisk row per share; discovery reads everything else from ACP, the interface table and the flash config
- discovery registers again when the network changes, and deregisters everything at shutdown so the daemon sends goodbye packets
- with no Samba payload on any disk, none of our records are registered; Apple's `_airport` and `_device-info` stay up either way
- a name conflict or registration error is retried with backoff; we never start the daemon ourselves

## Boot flow

`rc.local` starts `boot.sh` in the background so Apple's startup can continue. `boot.sh` prepares RAM directories, the diskd and wcifsnd guards, and a 6 MiB locks filesystem (`/mnt/Locks`: tmpfs on NetBSD 6, MFS on NetBSD 4). It leaves Apple's kernel settings alone and then runs `/mnt/Flash/service manager`.

### Native manager

The [manager](build/native/service/manager.c) handles everything that repeats:

- It watches for disk and network changes, and reads Apple's disk list (`MaSt`) and the mount table before using a volume.
- It mounts volumes through Apple's `diskd.useVolume`, keeps Apple's `mDNSResponder` and `afpserver` running, and runs `diskd` on loopback.
- It uses a valid internal payload first, then an external one.
- It copies `smbd`, the login files and the Samba config into RAM, checks `smbd` against the payload's checksums, and runs `smbd` as its child. A configuration change reloads Samba; a new `smbd` restarts it.
- It starts `service discovery` and `service telemetry --daemon` from the Flash image, and restarts any child that exits, with backoff.
- It stops Apple's SMB server (`wcifsfs`) and any `wcifsnd` that discovery did not start.
- It stops Samba and rsync when no valid payload is left.

Internal shares point at `ShareRoot` unless the disk-root option is on; external shares point at the volume root.

A full lock directory makes `smbd` fail to open files and can stop a backup partway. When the files in `/mnt/Locks` reach 4 MiB, the manager logs it, restarts `smbd` with the directory emptied, and sends a `locks_reset` heartbeat. Clients reconnect as they would after a dropped connection.

The manager's process title shows what it found:
- `payload=missing`: no disk holds the Samba files, as after an erase. Reinstall.
- `payload=damaged`: `smbd` on the disk does not match its checksum. Reinstall.
- `payload=unreadable`: the disk could not read it. The disk is failing.
- `unmounted=`: a disk has not mounted for 10 minutes. Restart the device, then run Disk Repair.
- `stuck=`: a process has been asleep in the kernel for 2 minutes or more.

Doctor reads these.

Logs:

- RAM: `var/rc.local.log`, `var/runtime.log`, `var/telemetry.log` and `var/rsync.log` under `/mnt/Memory/samba4`. Each keeps only its most recent lines.
- Discovery: `<payload>/logs/discovery.log`, or a RAM copy while no payload is available.
- Samba: `<payload>/logs/log.smbd` and `smbd-console.log`.

## Optional rsync daemon

Deploy always copies the rsync binary and its configuration to the payload (`/Volumes/dkX/.samba4/rsync` and `rsyncd.conf`). It runs only when turned on: Enable rsync in the app, or `tcapsule deploy --enable-rsync`, saved as `RSYNC_ENABLED=1` in `/mnt/Flash/tcapsulesmb.conf`. It is off by default.

When it is on, the manager copies rsync to RAM, checks it against the payload's checksums, and runs it as a daemon on TCP `873`. The daemon has one writable module, `shareroot`, at the payload volume's `ShareRoot`. It runs as `root` and asks for no password, so turn it on only on a network you trust. Its log is `/mnt/Memory/samba4/var/rsync.log`.

rsync runs separately from Samba. A failed rsync never stops file sharing; it is retried after 5, 15, then 60 s. Doctor reports `rsync=missing`, `rsync=damaged` (reinstall) or `rsync=unreadable` (a failing disk). Turning rsync off in a later deploy stops the daemon and leaves its files on the disk.

## SMB runtime layout

After a successful boot, `/mnt/Memory/samba4` holds:
- `sbin/smbd`
- `sbin/rsync`, when rsync is on
- `etc/smb.conf`
- `etc/rsyncd.conf`, when rsync is on
- `var/`
- `private/`

The login files are written at boot and exist only in RAM:
- `/mnt/Memory/samba4/private/smbpasswd`
- `/mnt/Memory/samba4/private/username.map`

Persistent Samba data such as `xattr.tdb` stays in `/Volumes/dkX/.samba4/private/`. NetBSD 4 keeps the Samba cache on the HDD (`/Volumes/dkX/.samba4/cache`) because its RAM disk is too small; NetBSD 6 keeps it in `/mnt/Memory/samba4/var`.

Generated `smb.conf` settings:
- `netbios name = <name from the device hostname>`
- `server string = <name from Apple syNm>`
- `security = user`
- `min protocol = SMB2` and `max protocol = SMB3` by default; the protocol and signing options change these
- `guest ok = no`
- `valid users = root`
- `force user = root`
- `force group = wheel`
- `reset on zero vc = yes`
- one share per `MaSt` volume
  - internal disk: `path = /Volumes/dkN/ShareRoot`
  - external disk: `path = /Volumes/dkN`
- `pid directory = /mnt/Memory/samba4/var`
- `lock directory = /mnt/Locks`
- `state directory = /mnt/Memory/samba4/var`
- `cache directory = /mnt/Memory/samba4/var` on NetBSD 6, `/Volumes/dkX/.samba4/cache` on NetBSD 4
- `private dir = /mnt/Memory/samba4/private`
- `log file = /Volumes/dkX/.samba4/logs/log.smbd`
- `max log size = 128`
- `deadtime = 720`
- `max smbd processes = 12`: room for eight backup clients plus their reconnects. Twelve backups at once have not been tested.
- `socket options` with larger receive buffers than the firmware default (and larger send buffers on NetBSD 4), so a Mac can keep sending Time Machine data while `smbd` waits for the disk
- `server smb3 signing algorithms` and `server smb3 encryption algorithms` ordered by cost on these CPUs. On NetBSD 6, signing a 128 MiB read takes 9.4 CPU seconds with HMAC-SHA256 and 38.4 with Samba's default AES-GMAC; encrypting it takes 20.9 with AES-128-CCM and 45.6 with AES-128-GCM. Unsigned traffic, macOS's default, is not affected.
- `vfs objects = catia fruit streams_xattr acl_xattr xattr_tdb`, plus `aio_fork` (at most two helpers per connection and share) unless `TC_VFS_AIO_FORK_DISABLED=true`
- `fruit:resource = file`; HFS shares use the native resource fork regardless
- `fruit:veto_appledouble = yes`
- `fruit:metadata = netatalk` by default, or `stream` when Netatalk metadata is turned off
- `fruit:time machine = yes`
- `mangled names = no`: names are listed as stored, so a Mac can open files named after DOS devices such as `AUX` or `con.txt`. A Mac cannot open a name that AFP or SSH stored with a trailing dot or space, though it is listed.
- `acl_xattr:ignore system acls = yes`
- `xattr_tdb:file = /Volumes/dkX/.samba4/private/xattr.tdb`; HFS shares do not use it
- `veto files = /.samba4/` on every share, which hides the payload when a share exposes the disk root

Before each `smbd` start the manager raises three kernel limits that are too low for several Macs backing up at once: the socket buffer ceiling (`kern.sbmax`), the open-file table (`kern.maxfiles`, 1024 to 4096; one sparse bundle keeps a hundred or more band files open), and the process table (`kern.maxproc`, set to the highest value each kernel runs safely: 90 on NetBSD 6, 103 on little-endian and 99 on big-endian NetBSD 4). Doctor records their values.

Login mapping:
- the RAM `smbpasswd` has one `root` entry, made from the AirPort password (`syPW`)
- the RAM `username.map` contains `!root = root` and `root = *`, so every SMB username maps to Unix `root`

The runtime config at `/mnt/Memory/samba4/etc/smb.conf` is rebuilt at every boot. `/mnt/Memory` is a RAM disk, so edits there, such as a one-off `log level = ...`, are lost at the next reboot, along with the RAM logs.

## Symbolic links

Symlinks are stored as native POSIX links, the same as Apple's AFP server and SSH create, so every protocol sees the same link:

- macOS clients see links as links and create them as usual. XSym link files from older releases keep working.
- Windows `mklink` works. `mklink /D` (a directory symlink) is refused with "Access is denied"; directory links made by other clients can be followed and removed.
- Linux clients can create links with any `symlink=` mount option, or with `mfsymlinks`. FIFOs, sockets and device nodes are refused (`EOPNOTSUPP`).
- `touch -h` does not change a link's times on HFS, the same as over AFP.

The devices' HFS driver can panic its journal when many links are created quickly, so each conversion to a native link ends with `sync()`.

## Discovery controller

The discovery controller is the `service discovery` role of `bin/service/service`, built from [build/native/discovery/](build/native/discovery/), [build/native/common/](build/native/common/) and Apple's unchanged `dns_sd` client code in [build/native/dnssd/](build/native/dnssd/). Each payload family has its own static binary, installed and run from `/mnt/Flash`.

At runtime it:
- registers `_smb._tcp`, `_adisk._tcp` and, with `MDNS_ADVERTISE_AFP=1`, `_afpovertcp._tcp` on every link allowed file sharing
- announces each `_adisk` record again 5 and 20 minutes after registering it, so a Mac that missed the first announcements replaces an old TXT such as Apple's, which advertises AFP. Deploy and activate also tell the Mac that ran them to refresh its cached copy.
- accepts Apple's conflict renames such as "Name (2)" as they are
- starts Apple's `wcifsnd` when the network allows NBNS, and registers the machine name and `WORKGROUP` with it once per child
- shows `nbns=disabled|waiting|starting|ready|none`, the payload state and the Samba name in its process title, which doctor reads
- logs registrations and network changes to `discovery.log`

`service --print-link-plan` and `service --print-mast` print the live plan and disk list.

## Native NBNS scope

Apple's firmware `wcifsnd`, run by `service discovery`, provides NBNS. It cannot be turned off. Old `NBNS_ENABLED` flash values and saved app preferences are ignored, and the removed `--no-nbns` CLI flag is rejected.

Discovery starts `wcifsnd` only when an SMB link has an IPv4 address Apple's daemon answers on: anything except `0.0.0.0`, and a 169.254 address only when the interface has no other IPv4 address. Without a DHCP server, the firmware leaves `0.0.0.0` beside the 169.254 address, so the device answers NBNS nowhere. The title then says `nbns=none`, and doctor reports NBNS as off and skips its NBNS query. A DHCP lease starts it. NBNS gives name registration and conflict handling. It does not give SMB1, NetBIOS sessions, or every legacy Windows browsing feature.

`uninstall` stops discovery and its `wcifsnd`. Apple's own NBNS comes back with the next reboot.

## Service and telemetry helpers

The `service` image on flash also computes NT password hashes, prints `--print-*` diagnostics, and runs telemetry. Telemetry sends a heartbeat at startup and every 12 hours. It downloads and runs a debug program only after checking the server's signed authorization. [build/native/README.md](build/native/README.md) has the sources, commands, protocol and cleanup rules.

## Setup workflow

1. Prepare this computer:
   - [`./tcapsule bootstrap`](./tcapsule)
2. Write the local config, and turn SSH on if needed:
   - [src/timecapsulesmb/cli/configure.py](src/timecapsulesmb/cli/configure.py)
3. Deploy, which reboots the device:
   - [src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py)
4. On NetBSD 4, optionally back up and inspect the firmware, then install the boot hook with `tcapsule flash --patch` and power-cycle the device after a successful write:
   - [src/timecapsulesmb/cli/flash.py](src/timecapsulesmb/cli/flash.py)
5. On NetBSD 4, run activate when the boot hook is missing or Samba does not start after a reboot:
   - [src/timecapsulesmb/cli/activate.py](src/timecapsulesmb/cli/activate.py)
6. Check the result:
   - [src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py)
7. Repair the HDD before redeploying, if needed:
   - [src/timecapsulesmb/cli/fsck.py](src/timecapsulesmb/cli/fsck.py)
8. Remove it later, if needed:
   - [src/timecapsulesmb/cli/uninstall.py](src/timecapsulesmb/cli/uninstall.py)

`tcapsule set-ssh` turns SSH on or off by itself. Normal setup does not need it.

`tcapsule configure` writes `.env` in the repo root unless `--config` or `TCAPSULE_CONFIG` names another path.

`.env` values:
- `TC_HOST`
- `TC_PASSWORD`
- `TC_SSH_OPTS`
- `TC_AIRPORT_MAC`
- `TC_INTERNAL_SHARE_USE_DISK_ROOT`
- `TC_SMB_BROWSE_COMPATIBILITY`
- `TC_MDNS_ADVERTISE_AFP`
- `TC_ANY_PROTOCOL`
- `TC_REQUIRE_SMB_ENCRYPTION`
- `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION`
- `TC_FRUIT_METADATA_NETATALK`
- `TC_VFS_AIO_FORK_DISABLED`
- `TC_DEBUG_LOGGING`
- `TC_ATA_IDLE_SECONDS`
- `TC_ATA_STANDBY`
- `TC_CONFIGURE_ID`

`.bootstrap` values:
- `INSTALL_ID`
- `TELEMETRY=false`, when telemetry is off

## macOS app Advanced checkboxes

The Advanced panel saves these choices in the device profile on the Mac. Run Install / Update Samba afterward to write them to `/mnt/Flash/tcapsulesmb.conf`; changing a checkbox does not change the device by itself. The defaults below are for a new profile.

### NBNS (always on, no checkbox)

On whenever the network allows it (see [Native NBNS scope](#native-nbns-scope)). Apple's `wcifsnd` answers NetBIOS name queries for the Samba machine name and `WORKGROUP` on UDP `137`/`138`. Clients that use Bonjour do not need it.

### Enable rsync

Off by default. Starts the bundled rsync daemon with a writable `shareroot` module on TCP `873` that runs as `root` and asks for no password. Turn it on only on a LAN you trust; see [Optional rsync daemon](#optional-rsync-daemon).

### Share internal disk root

Off by default. When off, an internal disk's share is `/Volumes/dkN/ShareRoot`. When on, the share is the whole `/Volumes/dkN`. External disks always share their volume root, and the `.samba4` payload stays hidden from SMB clients either way.

### Allow SMB Share Browsing

Off by default. Changes Samba's `restrict anonymous` from `2` to `0`, so clients that browse anonymously can list the shares. Guests still cannot open files: every share requires a login.

### Advertise AFP over Bonjour

Off by default, and leave it off. macOS 26 and later treats a Time Capsule that advertises AFP as an SMB1-only server and hides it from Finder and Time Machine. When on, discovery adds `_afpovertcp._tcp` on port `548`, and the ADisk flags change from SMB-only `adVF=0x82` to AFP+SMB `adVF=0x83`. Apple's `afpserver` runs with either setting; the checkbox changes only what is advertised.

### Use Netatalk metadata

On by default. Selects `fruit:metadata = netatalk`; off selects `fruit:metadata = stream`. On HFS it only decides which legacy copy wins during migration. After that, Finder info is native either way.

### Enable debug logging

Off by default. Sets Samba to `log level = 10` and removes the usual log size limits. Use it only while troubleshooting: the logs grow on the disk without limit and slow the device down.

### Enable vfs_aio_fork

On by default. Lets each client connection use up to two helper processes per share for disk I/O, which these kernels need because they have no threads. Unchecking it saves `TC_VFS_AIO_FORK_DISABLED=true`.

### Allow Any SMB Protocol

Off by default. Allows SMB1 and NTLMv1 logins (`server min protocol = NT1`, `ntlm auth = ntlmv1-permitted`), as Apple's firmware did, for old clients such as Windows XP, scanners and media players. SMB1 Unix extensions stay off. SMB1 and NTLMv1 are insecure, so leave this off unless an old client needs it. It cannot be combined with Require SMB Encryption.

### Require SMB Encryption

Off by default. Requires encrypted SMB3 (`server smb encrypt = required`, `server min protocol = SMB3_00`). Selecting it disables Allow Any SMB Protocol and Disable SMB signing and encryption.

### Disable SMB signing and encryption

Off by default. Writes `server signing = disabled` and `server smb encrypt = off`. Throughput may improve when a client would otherwise sign, but traffic is no longer protected. It cannot be combined with Require SMB Encryption.

## App updates

The macOS app updates itself with Sparkle from the latest GitHub release's `appcast.xml`. It checks once a day by default (Settings, "Check for updates automatically"), and Check for Updates... in the app menu checks at once. Prereleases and drafts are never offered. The app and the CLI stop when their version is below `min_supported_version` in `version.json`, and the CLI prints one line when a newer release exists.

The device page shows the version installed on the device and offers Install / Update Samba when it is older than the app. This check runs in the background and only with a saved password that works, so the page never asks for anything.

## CLI command reference

Run the CLI as `tcapsule COMMAND [ARGS...]`. In a fresh checkout, start with the repo's launcher:

```bash
./tcapsule bootstrap
```

After bootstrap, use the virtualenv command:

```bash
.venv/bin/tcapsule <command>
```

Commands:
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
- `set-ssh`
- `uninstall`
- `validate-install`
- `wipe-internal-disk`

Shared behavior:
- every command but `api` stops when `version.json` says this version is no longer supported, and prints one line when a newer release exists
- commands that read the device config accept `--config PATH`, which overrides `TCAPSULE_CONFIG` and the repo's `.env`
- commands that can prompt usually accept `--no-input`, which makes them fail instead of asking
- commands that change the device or reboot it accept `--yes` to skip the confirmation
- `--json` works only with `--dry-run` on commands whose JSON output is a plan
- every command checks that the device at the saved address is the saved AirPort (see [Saved device identity](#saved-device-identity))

### `bootstrap`

`tcapsule bootstrap` prepares this computer. It checks the selected Python, creates or reuses `.venv`, installs `requirements.txt` and the repo into it, and checks the host tools it needs. A missing `ssh` or `smbclient` is installed through Homebrew on supported macOS versions, or through the Linux package manager. OpenSSH 8.4 or newer is required. On Intel Macs it skips `smbclient`, which Homebrew no longer builds for Intel: deploy does not need it, but doctor's SMB checks do. On Fedora, RHEL and their derivatives, SSH gets an OpenSSL config that allows the SHA-1 signature the Time Capsule's host key uses.

Arguments:
- `--python PYTHON`: the Python used to create a new `.venv`, by default the one running the command. It must be Python 3.9 or newer. An existing `.venv` keeps its interpreter.

Bootstrap does not read device credentials from `.env`. It creates or keeps the install ID in `.bootstrap`.

### `paths`

`tcapsule paths` prints the local TimeCapsuleSMB paths: the distribution root, config path, state directory, package root, artifact manifest, and each deployable artifact with a basic check. Use it when an install has been moved, wrapped, or run from another directory.

Arguments:
- `--config PATH`: resolve paths as if this config file were selected
- `--json`: print the same data as JSON

### `validate-install`

`tcapsule validate-install` checks the local install without touching the device: the distribution root, the state and config paths, the packaged files, and the artifact metadata the app and CLI expect.

Arguments:
- `--config PATH`: resolve local paths with this config path
- `--json`: print `{ "ok": ..., "checks": ... }`, and exit nonzero if a check fails

### `discover`

`tcapsule discover` browses Bonjour for Apple AirPort storage services and prints both the raw browse results and the resolved records. It uses native `dns-sd` when installed and Python zeroconf otherwise, so it also works on Linux.

Arguments:
- `--config PATH`: load a config for telemetry context only; discovery does not need `.env`
- `--timeout SECONDS`: browse timeout, `6` by default
- `--json`: print the results as JSON
- `--select`: after printing, ask for a device number and print only that device's host

### `configure`

`tcapsule configure` creates or updates `.env`. In interactive mode it looks for AirPorts over Bonjour, asks for the SSH target and device password, checks SSH, turns SSH on through ACP when needed, probes the device, fills in defaults (including the AirPort MAC), and writes the config. The password is saved as `TC_PASSWORD` for this computer's SSH and ACP access. Samba gets its password from the AirPort (`syPW`) at boot.

Arguments:
- `--config PATH`: read and write this config instead of `.env`
- `--no-input`: do not prompt; the arguments or existing config must supply everything
- `--password-env NAME`: read the device password from environment variable `NAME`
- `--password-file PATH`: read the device password from a file, without trailing newlines
- `--password-stdin`: read the device password from stdin, without trailing newlines
- `--host HOST`: the device SSH target, for example `root@192.168.1.10`. A custom SSH port is rejected here; put it in `TC_SSH_OPTS`.
- `--skip-discovery`: skip Bonjour and use the given or saved SSH target
- `--yes`: approve turning SSH on through ACP when it is off
- `--enable-ssh`: turn SSH on through ACP when it is off
- `--no-enable-ssh`: fail when SSH is off
- `--json`: print a machine-readable result; requires `--no-input`
- `--force-disable-smb-signing-and-encryption` / `--no-force-disable-smb-signing-and-encryption`: write `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true|false`; `--disable-smb-security` and `--no-disable-smb-security` are aliases

Hidden advanced arguments:
- `--internal-share-use-disk-root` / `--no-internal-share-use-disk-root`: write `TC_INTERNAL_SHARE_USE_DISK_ROOT=true|false`
- `--smb-browse-compatibility` / `--no-smb-browse-compatibility`: write `TC_SMB_BROWSE_COMPATIBILITY=true|false`
- `--mdns-advertise-afp` / `--no-mdns-advertise-afp`: write `TC_MDNS_ADVERTISE_AFP=true|false`
- `--any-protocol` / `--no-any-protocol`: write `TC_ANY_PROTOCOL=true|false`
- `--require-smb-encryption` / `--no-require-smb-encryption`: write `TC_REQUIRE_SMB_ENCRYPTION=true|false`
- `--netatalk` / `--no-netatalk`: write `TC_FRUIT_METADATA_NETATALK=true|false`
- `--enable-vfs-aio-fork` / `--disable-vfs-aio-fork`: write `TC_VFS_AIO_FORK_DISABLED=false|true`
- `--debug-logging` / `--no-debug-logging`: turn debug logging on or off
- `--ata-idle-seconds SECONDS`: write `TC_ATA_IDLE_SECONDS`, a whole number; `0` turns the ATA idle timer off
- `--ata-standby SECONDS`: write `TC_ATA_STANDBY`, a whole number; `0` turns standby off, and a blank saved value leaves standby as it is

`TC_ANY_PROTOCOL=true` and `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true` each conflict with `TC_REQUIRE_SMB_ENCRYPTION=true`. Configure refuses either combination before writing `.env`.

Non-interactive examples:

```bash
TC_PASS='airport-password' .venv/bin/tcapsule configure --no-input --host root@192.168.1.10 --password-env TC_PASS --enable-ssh --yes
printf '%s\n' 'airport-password' | .venv/bin/tcapsule configure --no-input --host root@192.168.1.10 --password-stdin --json
```

### `set-ssh`

`tcapsule set-ssh` turns the firmware's SSH flag on or off for the device in `.env`. With no mode given, it turns SSH on when it is off, or asks whether to turn it off when it is on.

Arguments:
- `--config PATH`: use another config
- `--enable`: turn SSH on through ACP if port 22 is closed; nothing happens if it is open
- `--disable`: remove the `dbug` property over SSH and reboot; nothing happens if SSH is already off. It refuses while a volume exists only until the next restart (see [Volumes that exist only in memory](#volumes-that-exist-only-in-memory)).
- `--status`: only report whether port 22 answers; cannot be combined with `--no-wait`
- `--yes`: skip the question when SSH is already on and no mode was given
- `--no-input`: fail instead of asking when no mode was given
- `--no-wait`: return right after the change, without waiting for the port or the reboot

Turning SSH on restarts the device. If SSH does not open, the error says which case it was. With `ssh_enable_timeout`, the device did not come back within 10 minutes: unplug it, wait 10 seconds, plug it in, and try again after 2 minutes. With `ssh_closed_after_restart`, it came back with SSH still off: try again.

Use `configure` for first-time setup, and `set-ssh` to turn SSH on or off by itself.

### `deploy`

`tcapsule deploy` installs or updates TimeCapsuleSMB on the device. It checks the local artifacts, checks the device is compatible, picks a writable HFS volume for the payload, migrates old metadata, uploads the payload and boot files, writes `/mnt/Flash/tcapsulesmb.conf`, and reboots. On NetBSD 4 it checks the runtime once SSH is back and starts it only if the firmware did not. See [Deploy details](#deploy-details).

Arguments:
- `--config PATH`: use another config
- `--no-wait`: request the reboot and return without waiting for SSH or checking the runtime
- `--yes`: do not ask before rebooting
- `--no-input`: fail instead of asking; a real deploy then needs `--yes`
- `--dry-run`: print the plan without changing the device
- `--json`: print the dry-run plan as JSON; requires `--dry-run`
- `--allow-unsupported`: continue when the device is not a supported model
- `--enable-rsync`: write `RSYNC_ENABLED=1` so the manager starts rsync; the rsync files are uploaded either way
- `--mount-wait SECONDS`: how long each volume mount may take, `30` by default

Hidden advanced arguments:
- each saved profile setting (internal-share root, SMB browsing, AFP advertising, protocol and security, Netatalk metadata, debug logging, `vfs_aio_fork`) has an on/off override pair; without one, the saved `.env` value is used

Plan modes:

```bash
.venv/bin/tcapsule deploy --dry-run
.venv/bin/tcapsule deploy --dry-run --json
```

### `activate`

`tcapsule activate` starts an installed NetBSD 4 runtime without uploading anything. If the runtime is already up, it leaves it running; otherwise it stops any running launcher and runs `/mnt/Flash/rc.local` again. It refuses installs older than v3.3.0 (`runtime_outdated`) and asks for a deploy first.

Arguments:
- `--config PATH`: use another config
- `--yes`: do not ask before restarting the Samba services
- `--no-input`: fail instead of asking; a real activation then needs `--yes`
- `--dry-run`: print the steps without changing the device
- `--json`: print the dry-run plan as JSON; requires `--dry-run`

`activate` works only on NetBSD 4 AirPort devices. NetBSD 6 devices use `deploy` for installs and updates.

### `flash`

`tcapsule flash` manages the NetBSD 4 firmware banks. By default it only reads: it backs up and analyzes both flash banks, saves a manifest, and prints the firmware state. Writing needs an explicit mode. `--patch` writes the TimeCapsuleSMB boot hook into the primary bank. `--restore` writes Apple's stock firmware to the active bank, or to the primary bank with a warning when both banks look active.

Without `--force`, `--patch` refuses unless both banks are valid backups, so a write that stops halfway always leaves a bank to start from. When only the secondary bank is invalid and the device runs from a valid primary, `--restore` rewrites the secondary with the newest Apple firmware for the model. This works only on models whose flash drivers were checked for it (syAP 106, 109, 113 and 116), and needs no reboot. Back up again before patching.

Arguments:
- `--config PATH`: use another config
- `--read-only`: back up and analyze the banks without planning a patch; this is the default
- `--patch`: write the TimeCapsuleSMB LOGIN hook into the primary bank
- `--restore`: write Apple's stock firmware to the active bank, to the primary when both look active, or to the secondary when only it is invalid
- `--check-apple`: check whether the bank or banks match Apple's stock firmware
- `--download-only`: back up and analyze the banks, then download and check Apple's firmware without writing it
- `--yes`: do not ask before a `--patch` or `--restore` write; valid only with those
- `--no-input`: fail instead of asking; writes then need `--yes`
- `--reboot`: after a verified `--restore` write to the active bank, request a software reboot
- `--no-wait`: with `--restore --reboot`, return after requesting the reboot
- `--json`: print the analysis and plan as JSON; read-only modes only
- `--backup-dir PATH`: back up into exactly `PATH` instead of a new timestamped directory
- `--force`: with `--patch`, skip the backup and active-bank checks and write the primary bank
- `--firmware-template PATH`: use a local Apple `.basebinary` file instead of picking one from Apple's catalog
- `--firmware-version VERSION`: pick an Apple firmware version, for example `7.8.1`

Hidden argument:
- `--poweroff`: rejected; after a patch, power-cycle the device by hand

Mode rules:
- `--patch --reboot` is rejected; a patch cannot request a software reboot
- `--reboot` works only with `--restore`, and not when restore rewrites the secondary bank
- `--no-wait` works only with `--restore --reboot`
- `--json` works only in read-only modes
- `--patch` needs `zopfli` gzip support on this computer

### `doctor`

`tcapsule doctor` checks this computer and the device without deploying, rebooting or changing the configuration. [Doctor command](#doctor-command) lists the checks.

The results come in two sections, each with its own status. **This computer** covers its tools, `nsmb.conf`, the SSH login, and its view of the device over the network. **Device** covers what runs on the device, checked over SSH, including logged-in SMB checks through an SSH tunnel. A failure in either section fails doctor.

Arguments:
- `--config PATH`: use another config
- `--skip-ssh`: skip SSH and every check on the device
- `--skip-bonjour`: skip the Bonjour checks
- `--skip-smb`: skip the SMB listing and file checks
- `--json`: print one JSON result at the end

Run `doctor` after a deploy or a reboot. Its SMB checks create, change and remove a hidden `.doctor-fileops-*` directory on a share. A timeout, interruption or early failure can leave that directory behind; use `--skip-smb` when doctor must not write through SMB.

### `fsck`

`tcapsule fsck` runs `fsck_hfs` on the device against one HFS volume. It mounts and wakes Apple's volumes, picks a volume or asks for one, stops TimeCapsuleSMB and Apple's file sharing (and gives up if anything will not stop), unmounts the disk, runs `fsck_hfs`, and reboots. Unless `--no-reboot` is given, it refuses to start while a volume exists only in memory, because the reboot would take it (see [Volumes that exist only in memory](#volumes-that-exist-only-in-memory)).

- A repair can take an hour or more on a large disk, and then checks the volume again. Progress shows each `fsck_hfs` step as it starts.
- If the SSH session drops, `fsck_hfs` keeps running on the device and file sharing stays off until it ends; the command reports `fsck_session_dropped`. A second repair is refused while one is running.
- If `fsck_hfs` crashes (for example with SIGBUS, exit status 138), running it again on the device will stop at the same place. Connect the disk to a Mac (an internal disk through a SATA-to-USB adapter) and run First Aid in Disk Utility.

Arguments:
- `--config PATH`: use another config
- `--yes`: do not ask before the repair
- `--no-input`: fail instead of asking; a repair then needs `--yes`
- `--no-reboot`: run `fsck_hfs` and do not reboot afterward
- `--no-wait`: do not wait for the device and SSH to come back after the reboot
- `--volume VOLUME`: the HFS volume, for example `dk2` or `/dev/dk2`; with several mounted volumes and no `--volume`, it asks

Run it when doctor or a failure points at the filesystem, or before a deploy to a damaged disk.

### `wipe-internal-disk`

`tcapsule wipe-internal-disk` erases the device's internal disk and formats it as AirPort Utility's Erase Disk does, including disks Erase Disk cannot format. Apple's partitioning tool gives up on some partition tables (one made for a bigger disk, a broken one, or a Windows-style MBR) without reporting an error, and Erase Disk then formats volumes that exist only in memory and vanish at the next boot. Wipe Internal Disk first zeroes 10 MiB at each end of the disk, which clears any such table, then asks Apple's disk service for the same Quick erase AirPort Utility uses.

Steps:
- It reads the disk list and partition tables without mounting or changing anything.
- It refuses an internal disk that reports under 15 GB with no partitions (a drive misreporting its size), a disk the firmware has lost, and any disk while Apple's disk service is busy. It also refuses while another disk has Apple's APconfig, APswap or APdata partitions, because Apple's erase would format that disk instead. Unplug such a USB disk first. It never wipes a USB disk.
- It asks, with No as the default, naming the disk and what is on it, and warns when the disk shows signs of failing.
- It stops file sharing, zeroes both ends of the disk, checks the writes, and asks for the Quick erase. The new disk gets APconfig, APswap and APdata partitions, and the volume keeps its old name when it can, else "Data".
- It waits up to 30 minutes for the format, reboots, checks the new volume, and starts file sharing again. If the wiped disk held the Samba files, run `tcapsule deploy` next.

Arguments:
- `--config PATH`: use another config
- `--dry-run`: read the disk and print the plan, including the wipe offsets, without changing anything
- `--yes`: do not ask before wiping
- `--no-input`: fail instead of asking; a wipe then needs `--yes`

#### Volumes that exist only in memory

When Apple's `gpt` cannot replace a disk's partition table, AirPort Utility's Erase Disk still formats and mounts the data volume but writes no table. The volume works and Time Machine can back up to it, but the next restart takes it and everything on it. This happens with a table `gpt` rejects (`gpt: error: bogus map`), and with a Windows-style MBR whose partition reaches the last sector.

Doctor finds these volumes from this boot's erase log and the disk's current table, and fails with `disk_volumes_in_memory` (internal disk) or `usb_disk_volumes_in_memory`. Install, Disk Repair, Uninstall and Disable SSH refuse to run while one exists, because each ends by restarting the device.

Copy off anything you need from the volume first. For the internal disk, run Wipe Internal Disk. For a USB disk, disconnect it, erase it on a Mac, and connect it again. Volumes erased before TimeCapsuleSMB was installed cannot be detected.

### `uninstall`

`tcapsule uninstall` removes TimeCapsuleSMB's files from the device. It stops the manager, removes the payload directories from the mounted HFS volumes, removes the loader files under `/mnt/Flash` and the runtime state, runs `sync`, and reboots so Apple's services and the root filesystem start clean. After a waited reboot it checks the files are gone. Unless `--no-reboot` or `--dry-run` is given, it stops before mounting anything while a volume exists only in memory. It does not restore a firmware bank changed by `flash --patch`; `flash --restore` does that.

Arguments:
- `--config PATH`: use another config
- `--mount-wait SECONDS`: how long each volume mount may take, `30` by default
- `--no-wait`: request the reboot and return without checking the result
- `--yes`: do not ask before rebooting
- `--no-input`: fail instead of asking; an uninstall that reboots then needs `--yes`
- `--no-reboot`: remove the files without rebooting
- `--dry-run`: print the plan without changing the device
- `--json`: print the dry-run plan as JSON; requires `--dry-run`

`uninstall` does not turn Apple's AFP or SMB settings back on.

### `api`

`tcapsule api` is the backend for the macOS app. It reads one JSON object from stdin, runs the operation, and writes app events as NDJSON to stdout. The object has:
- `operation`: the operation name (required)
- `params`: a JSON object, `{}` by default
- `request_id`: an identifier copied onto each event

Arguments:
- `--pretty-error`: also print request parsing errors to stderr

Use the commands above; `api` is for the app.

## Local test coverage

`make install` installs `coverage.py` through the `dev` extra. `./tcapsule bootstrap` installs only `requirements.txt` and the editable package.

Test and coverage commands:
- `make test`: C compile checks and the pytest suite
- `make test-parallel`: the same, with pytest running in parallel through `pytest-xdist`
- `make coverage`: pytest with branch coverage, printing the missing lines
- `make coverage-html`: the same report as HTML in `htmlcov/index.html`
- `make coverage-native`: native C coverage; see [native checks](build/native/README.md#builds-and-checks)
- `cd macos/TimeCapsuleSMB && swift test`: the macOS app and helper unit tests

`make test` does not run the Swift tests. Run both when a change touches both the backend and the app.

Defaults and fixed values:
- `TC_INTERNAL_SHARE_USE_DISK_ROOT=false`
- `TC_SMB_BROWSE_COMPATIBILITY=false`
- `TC_MDNS_ADVERTISE_AFP=false`
- `TC_ANY_PROTOCOL=false`
- `TC_REQUIRE_SMB_ENCRYPTION=false`
- `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=false`
- `TC_FRUIT_METADATA_NETATALK=true`
- `TC_VFS_AIO_FORK_DISABLED=false`
- `TC_DEBUG_LOGGING=false`
- `TC_ATA_IDLE_SECONDS=300`
- `TC_ATA_STANDBY=`, which leaves the standby timer as it is; `0` turns standby off
- `TC_SSH_OPTS` includes the old SSH algorithms AirPort firmware needs
- the docs use the SMB username `admin`
- the payload directory is always `.samba4`

The Samba NetBIOS name and server string come from `/usr/bin/acp -q syNm` and `/bin/hostname` on the device at runtime. They are not set in `.env`.

Validation:
- `TC_HOST` must not be empty.
- `TC_PASSWORD` is required by doctor, flash and `set-ssh` (except `--status`). Deploy and activate ask for it when it is missing. fsck and uninstall can log in with an SSH key or agent instead.
- `TC_SSH_OPTS` is written by `configure` with the old SSH options AirPort firmware needs.
- The share, browsing, AFP, protocol and security, Netatalk metadata, `vfs_aio_fork` and debug settings above must be `true` or `false`.
- Required encryption cannot be combined with `TC_ANY_PROTOCOL=true` or `TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=true`.
- `TC_ATA_IDLE_SECONDS` is an optional whole number, `300` by default; `0` turns the ATA idle timer off with `atactl setidle 0`.
- `TC_ATA_STANDBY` is an optional whole number; blank leaves standby as it is, and `0` turns it off with `atactl setstandby 0`.
- `TC_CONFIGURE_ID` is a local revision ID and is not checked.

How `configure` works:
- It starts by looking for the Time Capsule over mDNS.
- If SSH answers, it checks the SSH target and password and probes the device.
- If SSH is off, it turns SSH on with its built-in Python ACP client, reboots the device through ACP, waits for SSH, and then probes the device.
- A wrong password over ACP asks for the password again. Any other ACP failure stops configure with that error.
- On NetBSD 4, it uses the byte order and the device's `acp` identity to name the exact generation when it can.
- It checks the `.env` values before writing them, and `deploy`, `activate` and `doctor` stop early on invalid values.

The binaries and build tooling are in the repo, so you can swap binaries, rebuild them, or trace the boot and runtime layout yourself.

## Host-side architecture

Package areas:
- [src/timecapsulesmb/cli/](src/timecapsulesmb/cli): command entrypoints and the app's `api` helper
- [src/timecapsulesmb/app/](src/timecapsulesmb/app): the app API: request handling, operation contracts, progress and result events, confirmations, and recovery guidance
- [src/timecapsulesmb/services/](src/timecapsulesmb/services): configure, deploy, activation, maintenance, storage, reboot, doctor and runtime workflows shared by the CLI and the app
- [src/timecapsulesmb/core/](src/timecapsulesmb/core): config parsing, defaults, and common models
- [src/timecapsulesmb/transport/](src/timecapsulesmb/transport): local commands, and SSH commands, tunnels and uploads
- [src/timecapsulesmb/discovery/](src/timecapsulesmb/discovery): Bonjour device discovery
- [src/timecapsulesmb/integrations/](src/timecapsulesmb/integrations): a self-contained Python ACP client for turning SSH on and rebooting
- [src/timecapsulesmb/checks/](src/timecapsulesmb/checks): local, network, Bonjour and SMB checks
- [src/timecapsulesmb/device/](src/timecapsulesmb/device): device probes, `MaSt` parsing, payload volume choice, and generation and compatibility detection
- [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy): deploy planning, remote actions, uploads, dry-run output, artifact lookup, and post-deploy checks
- [src/timecapsulesmb/assets/](src/timecapsulesmb/assets): boot templates and artifact metadata
- [src/timecapsulesmb/telemetry/](src/timecapsulesmb/telemetry): client telemetry for the commands
- [macos/TimeCapsuleSMB/](macos/TimeCapsuleSMB): the Swift macOS app, its helper launcher, saved device profiles, workflows, translations, and Swift tests
- [build/](build): build tooling

Where to start:
- device discovery: `discovery/`
- install behavior: `services/deploy.py`, then `deploy/planner.py` and `deploy/executor.py` for the plan and the transfers
- the app contract and progress events: `app/`, then the matching service
- boot behavior on the device: the boot templates in `assets/` and [build/native/](build/native/)
- replacing or rebuilding binaries: the artifact manifest and `build/`

## Doctor command

[src/timecapsulesmb/cli/doctor.py](src/timecapsulesmb/cli/doctor.py) checks:
- `.env` values, the local tools, and the checksums of the checked-in binaries
- SSH, device compatibility and payload family, and the installed release in `/mnt/Flash/tcapsulesmb.conf`
- the manager's verdicts (`payload=missing`, `damaged`, `unreadable`, `unmounted=`; see [Native manager](#native-manager)); any of them is doctor's whole result
- this boot's kernel disk errors, reported as the cause (`disk_failing`) when Samba is off
- each disk's SMART status and volume sizes as AirPort Utility shows them; a failing SMART status is a warning
- processes stuck in the kernel, and a disk that has stopped responding (`disk_not_responding`)
- Apple ACPd's health (see [Firmware problems](#firmware-problems))
- volumes that exist only in memory (`disk_volumes_in_memory`)
- `smbd`, the discovery role, Apple's `mDNSResponder`, the loopback `diskd` and its guards, NBNS, and rsync when it is on
- an `smbd` that has waited 2 minutes or more for a file lock, which only a restart clears
- that Apple's `printd` still advertises a shared USB printer
- the RAM copies of the binary, config and login files, the mounted share volumes, and the service sockets
- the IPv4 and IPv6 SMB addresses and TCP 445 reachability
- the device's own Bonjour records (found by its MAC), their names and hosts, the `_adisk` Time Machine flags and disk rows, and another device using the same name
- NBNS name lookup, when the device has an address NBNS answers on
- a logged-in `smbclient -L` listing, at least one share in it, and temporary SMB file operations
- that the non-HFS `xattr_tdb:file` setting points at the disk, not the RAM disk

Results print as each check finishes; `--json` prints one result at the end. For the first 180 seconds after the manager starts, doctor waits, checking every 10 seconds, while Samba, mDNS and NBNS start.

```bash
.venv/bin/tcapsule doctor
.venv/bin/tcapsule doctor --json
.venv/bin/tcapsule doctor --skip-ssh
.venv/bin/tcapsule doctor --skip-bonjour
.venv/bin/tcapsule doctor --skip-smb
```

Run it after setup, after a deploy, and after a reboot.

## Deploy details

[src/timecapsulesmb/cli/deploy.py](src/timecapsulesmb/cli/deploy.py) calls shared modules in [src/timecapsulesmb/deploy/](src/timecapsulesmb/deploy) and [src/timecapsulesmb/device/](src/timecapsulesmb/device). In order, deploy:

- loads `.env` and checks it before touching the device
- checks the binaries against the artifact manifest
- checks the device is compatible, before uploading anything
- refuses a device whose ACPd is unhealthy or whose disk is not responding (see [Firmware problems](#firmware-problems))
- reads Apple's `MaSt` disk list from the device
- picks one writable HFS volume for the payload:
  - the first writable internal (`builtin=true`) HFS volume
  - else the first writable external HFS volume
  - else it fails with `Deploy could not write to any deployable HFS volume.` (`deploy_disk_not_writable`)
- stops before changing anything while a volume exists only in memory (see [Volumes that exist only in memory](#volumes-that-exist-only-in-memory))
- builds the plan and the configuration on this computer before stopping anything
- asks before stopping or replacing anything, unless `--yes` is given; answering no leaves the device as it was
- stops the current and older TimeCapsuleSMB processes and Apple's file servers, disables `rc.local`, and runs `sync`
- migrates old metadata (see [Migrating older metadata](#migrating-older-metadata))
- removes known obsolete programs, keeping data, metadata, quarantined databases, logs, SSH keys and Apple's settings
- checks the free space on flash, with a small margin
- uploads `smbd`, `rsync` and `rsyncd.conf` to the payload, and the `service` image, `boot.sh` and `dfree.sh` to flash
- checks the uploaded sizes and sets file and directory permissions, then runs `sync`
- uploads no password files; the device writes them to RAM at boot from the AirPort password (`syPW`)
- writes the new `/mnt/Flash/tcapsulesmb.conf` and `rc.local` last, then runs `sync` again
- reboots
- after the reboot, checks that:
  - `smbd` listens on TCP `445`
  - Apple's `mDNSResponder` runs and is alone on UDP `5353`, `diskd` runs on loopback, and `service discovery` has a valid plan and the expected NBNS state
  - rsync listens on TCP `873` when on, and no rsync daemon runs when off
- on NetBSD 4, once SSH is back, runs `/mnt/Flash/rc.local` only when the firmware boot hook is missing, and otherwise waits for the runtime the firmware started

Running deploy again after an interruption finishes the install; there is no rollback. User data, unmigrated metadata, quarantined databases and logs are kept. A connection lost during the install reports `install_interrupted`, which asks for another run. `--no-wait` returns after requesting the reboot and does not check the runtime.

Doctor does the full Bonjour, SMB listing, SMB file, share, NBNS, metadata and installed-version checks.

On NetBSD 4:
- tested 1st-generation hardware without the firmware boot-hook patch keeps no `/etc` hook, so it needs `tcapsule activate` after each reboot
- other NetBSD 4 generations may start on their own if their firmware runs `/mnt/Flash/rc.local` early in boot; this is not proven
- `activate` does not rerun `/mnt/Flash/rc.local` when `smbd`, discovery and rsync (if on) are already up

```bash
.venv/bin/tcapsule deploy --dry-run
.venv/bin/tcapsule deploy --dry-run --json
.venv/bin/tcapsule activate --dry-run
.venv/bin/tcapsule activate
```

Dry runs print the exact steps without changing the device.

Debug logging:
- `tcapsule deploy --debug-logging` writes `SMBD_DEBUG_LOGGING=1` and `MDNS_DEBUG_LOGGING=1` to the flash config, and sets Samba to `log level = 10` and `max log size = 0`.
- `log.smbd` is normally capped at `128 KiB`, and the other payload logs are cut to their last `16 KiB` once they pass `32 KiB`. With `--debug-logging` they grow without limit.
- The flag is left out of the command help because it is only for debugging.

## Client telemetry

Every `tcapsule` command, and every app operation through `tcapsule api`, sends a `<command>_started` and a `<command>_finished` event (for example `deploy_started` and `deploy_finished`).

Identity:
- `.bootstrap` holds a stable `INSTALL_ID` for this install
- `.env` holds a `TC_CONFIGURE_ID` that changes with each configure

Sending:
- events go to the HTTPS telemetry endpoint
- started events are sent in the background
- finished events are sent before the command exits, so they are not lost
- `TELEMETRY=false` in `.bootstrap` turns telemetry off

## Uninstall

Uninstall:
- stops the manager first, so it cannot restart `smbd` during removal
- mounts the current `MaSt` HFS volumes and removes `.samba4` from each
- with no HFS volume mounted, still removes the loader files and runtime state, and reports that only flash and RAM were cleaned
- removes the loader files under `/mnt/Flash`, the RAM runtime, and compatibility symlinks, but not a firmware bank changed by `flash --patch`
- asks before rebooting
- after a waited reboot, checks that every payload directory, flash loader, RAM path and compatibility symlink it planned to remove is gone

## Artifact resolution

The deployable binaries are in [bin/](bin). Their paths are not hardcoded in the host code; three files own them:
- [src/timecapsulesmb/assets/artifact-manifest.json](src/timecapsulesmb/assets/artifact-manifest.json)
- [src/timecapsulesmb/deploy/artifact_resolver.py](src/timecapsulesmb/deploy/artifact_resolver.py)
- [src/timecapsulesmb/deploy/artifacts.py](src/timecapsulesmb/deploy/artifacts.py)

Deploy and doctor look artifacts up by name, and checksums and paths are checked in one place.

## What the build pipeline produces

The build pipeline under [build/](build) is for maintainers.

Outputs:
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

The `service` image runs from `/mnt/Flash`; `smbd` and rsync are copied to RAM from the payload.

Building needs a NetBSD VM, a root-owned cross-build tree under `/root`, and `su` for the build steps. NetBSD 6-era devices build from NetBSD 7, and NetBSD 4-era devices from NetBSD 4. NetBSD 10 was useful only for early experiments.

Build scripts:
- NetBSD 7 SDK: [build/download.sh](build/download.sh), [build/bootstrap.sh](build/bootstrap.sh)
- NetBSD 4 SDKs: [build/downloadoldle.sh](build/downloadoldle.sh), [build/bootstrapoldle.sh](build/bootstrapoldle.sh), [build/downloadoldbe.sh](build/downloadoldbe.sh), [build/bootstrapoldbe.sh](build/bootstrapoldbe.sh)
- Samba 4.25.0: [build/downloadsamba4x.sh](build/downloadsamba4x.sh) and [build/samba4x.sh](build/samba4x.sh); [build/downloadsamba4xoldle.sh](build/downloadsamba4xoldle.sh), [build/samba4xoldle.sh](build/samba4xoldle.sh), [build/downloadsamba4xoldbe.sh](build/downloadsamba4xoldbe.sh), [build/samba4xoldbe.sh](build/samba4xoldbe.sh); or [build/samba4xall.sh](build/samba4xall.sh) for all three
- rsync: [build/downloadrsync.sh](build/downloadrsync.sh), [build/rsync.sh](build/rsync.sh), [build/rsyncoldle.sh](build/rsyncoldle.sh), [build/rsyncoldbe.sh](build/rsyncoldbe.sh)
- `service`: [build/service.sh](build/service.sh), [build/serviceoldle.sh](build/serviceoldle.sh), [build/serviceoldbe.sh](build/serviceoldbe.sh)

The plain scripts build the NetBSD 7 lane; `*oldle.sh` and `*oldbe.sh` build the NetBSD 4 little-endian and big-endian lanes. The VM-side [build/Makefile](build/Makefile) has per-family and all-lane targets.

Output directories:
- NetBSD 7 SDK: `/root/tc-earmv4-netbsd7`
- NetBSD 4 little-endian SDK: `/root/tc-earmv4-netbsd4`
- NetBSD 4 big-endian SDK: `/root/tc-armeb-netbsd4`
- NetBSD 7 runtime outputs: `/root/tc-netbsd7`
- NetBSD 4 little-endian runtime outputs: `/root/tc-netbsd4le`
- NetBSD 4 big-endian runtime outputs: `/root/tc-netbsd4be`

## Important historical findings

### The internal disk can be mounted locally

The Time Capsule can mount `/dev/dk2` itself with `mount_hfs`; a Mac does not have to start Apple's file sharing first. The rest of the project depends on this.

### Running `smbd` from the HDD is a bad idea

Apple may unmount or sleep the HDD at any time, so `smbd` runs from RAM.

### Running the `service` image from the HDD would be unsafe

The manager and discovery have to keep running when the HDD is unmounted. The runtime runs the `service` image from `/mnt/Flash`, which also saves RAM.

### Apple's SMB advertisements send Finder to Apple's servers

If Apple's own SMB and AFP stack gets its advertisements back, Finder may connect to Apple's servers and bypass our Samba. Apple's `diskd` registers those names whatever the settings say.

Releases before v3.1.0 killed Apple's `mDNSResponder` and ran their own responder. Since v3.1.0 we keep Apple's daemon (it owns `_airport`, `_device-info` and the host records, and nothing restarts it), run `diskd` on loopback so its registrations never reach the LAN, and register our own names through the daemon.

### The firmware lacks common small commands

The device has no `grep`, `dirname`, `find` or `strings`, among others. Shell scripts that run on it must make do without them.

### Non-root Unix identities fail

Earlier Samba builds on this firmware failed to switch privileges when users mapped to non-root identities, so every login maps to `root`.

## Firmware problems

These failures come from Apple's firmware. TimeCapsuleSMB detects them and says what to do, but none can be fixed over the network. [build/native/README.md](build/native/README.md#apple-acpd) has the measured details.

### Apple ACPd can hang during its own restart

ACPd is Apple's AirPort management service. On some devices it accepts a restart request, shuts down its own services, and never restarts the system. SSH may still answer, but AirPort Utility, file sharing and Bonjour are gone until the device is unplugged. The warning sign is a second ACPd process stuck under the first.

Before every restart and every flash write, TimeCapsuleSMB checks ACPd. It refuses with `acpd_broken` ("Nothing was changed") on the warning sign, on an ACPd that does not answer, and after a restart that never happened. When a restart it requested does not happen, it reports that about two minutes after the request. Doctor reports the same problems. TimeCapsuleSMB never retries the restart itself.

Unplug the device, wait 10 seconds, plug it back in, wait 2 minutes, and try again.

### Apple ACPd can quit after file sharing starts

On some NetBSD 4 devices, ACPd exits soon after file sharing starts following a restart, and NetBSD 4 never starts it again. A Samba that was already serving keeps serving, but the disk list, AirPort Utility, updates and restarts stop working. If Samba had not started, file sharing stays off. Install, Activate and Disk Repair report `acpd_quit`, and doctor says whether sharing still works.

Unplug the device and plug it back in, then run Checkup. On NetBSD 4, run Activate if Checkup says Samba is not running. When this follows an install, the update is already on the device and does not need installing again.

### A disk can stop responding behind ACPd

When a disk stops answering, the processes waiting for it hang in the kernel. ACPd waits on Apple's disk service, so it stops answering as well, and the failure looks like ACPd's. TimeCapsuleSMB checks for a stuck disk first and reports `disk_not_responding` ("A disk on the device has stopped responding").

Unplug the device, wait 10 seconds, plug it back in, wait 2 minutes, and try again. If the problem comes back, the disk is failing. Copy its files off (a USB disk connects to a Mac as it is, an internal disk through a SATA-to-USB adapter) and replace it.

## Known risks and caveats

- Use TimeCapsuleSMB only on a local network.
- Every login accesses files as `root`.
- `/mnt/Memory` is tight: about `1-2 MiB` may be free after staging.
- The code assumes AirPort storage firmware behavior, including:
  - AirPort-style IPv4 and interface layout
  - HFS partitions named `dk…`, found through Apple's `MaSt`
  - the `ShareRoot` layout on internal volumes
- Apple's firmware may still mount disks late or change their state in unusual cases.
- Apple's ACPd can hang during its own restart and leave the device off the network until it is unplugged (see [Firmware problems](#firmware-problems)).

## Verification commands

Checks to run from a Mac.

Browse SMB advertisements:

```bash
dns-sd -B _smb._tcp local.
```

Resolve the SMB service:

```bash
dns-sd -L "<advertised-instance-name>" _smb._tcp local.
```

List the shares with a login:

```bash
smbutil view //admin:<password>@<configured-or-advertised-host>
```

Mount a share:

```bash
mount_smbfs //admin:<password>@<configured-or-advertised-host>/<share-name> /tmp/tc-auth-mount
```

The listing should show `IPC$` and at least one share named after a `MaSt` volume.

A guest login should fail with an authentication error:

```bash
smbutil view //guest:@<configured-or-advertised-host>
```

## Files worth reading

- [README.md](README.md): overview and quick start
- [FAQ.md](FAQ.md): common questions and troubleshooting
- [build/native/README.md](build/native/README.md): the on-device service and firmware facts

## Summary

TimeCapsuleSMB:
- builds reproducibly
- deploys from checked-in binaries
- survives a reboot on NetBSD 6
- on NetBSD 4, starts at boot with the firmware boot-hook patch, or with `activate` after each reboot on tested unpatched 1st-generation hardware
- advertises itself over Bonjour
- logs in with the AirPort password; the docs use the SMB username `admin`
- serves the internal disk through Samba 4.25.0
- works as a Time Machine destination through `vfs_fruit`

The core features work. What remains is polish.
