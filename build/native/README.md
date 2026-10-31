# Native device helpers

`service.sources` links one fully static device executable, with independent
`manager`, `discovery`, and `telemetry` processes and diagnostic entrypoints.
Common code is linked once. No role uses pthreads, and host regressions compile
the same unified source list.

| Entrypoint | Responsibility |
| --- | --- |
| `service manager` | Event-driven supervision, Apple volume activation, guarded RAM staging, Samba configuration and child lifetimes. |
| `service discovery` | Bonjour registrations through Apple's mDNSResponder and one owned native wcifsnd child. |
| `service telemetry --daemon` | Heartbeat collection/POST, scheduling and signed debug execution. |
| `service --print-*` | Existing identity, hashing, MaSt and network diagnostics. |

Build the device image with `build/service.sh`, `build/serviceoldle.sh`, or
`build/serviceoldbe.sh` using the existing NetBSD SDK lanes. Boot executes the
Flash copy. Samba and optional rsync execute from RAM because Apple may unmount
the HDD. Discovery and telemetry collect their own network plans; the manager
is network-blind and passes successfully applied disk shares to discovery as
argv.

Bonjour registrations use `name=NULL` and flags `0`: Apple owns the default
instance name and conflict renaming across SMB/ADisk. This follows the live
stock `diskd` test on 2026-09-19, where an SMB-only collision renamed both
services together without changing `syNm` or the hostname. Successful callback
names are accepted as returned; they are not treated as conflicts against ACP.

`common/` is the shared device-facts collector (v3.1.0): `iflist.c` walks
`sysctl(NET_RT_IFLIST)` itself (Apple's kernel `if_msghdr` differs from the
SDK's, so `getifaddrs()` returns garbage names), `acp.c` runs `acp -q`
children with timeouts and a non-blocking mode, `config.c` decodes the flash
config literally, and `topology.c`/`policy.c`/`identity.c`/`plan.c` turn
those facts into a `struct device_plan` (link roles, service masks and
identity) with the retained-policy rules of the redesign plan.
`loop.c` is the daemons' select loop (PF_ROUTE debounce + 30 s poll).
Host test builds (`TC_NATIVE_TEST`) accept `--facts-file` snapshots. Device
builds omit the fixture parser and accept only live facts; top-level
`service --print-link-plan` remains available for live diagnostics.
There is no shared plan daemon or cache/status file; each process owns its own
last validated plan. Fixtures from both device lanes live under
`tests/native/fixtures/iflist/`.

ACP capture uses one process/timeout engine with independent multiline and
whitespace-trimming options and caller-owned buffers. Scalar facts retain their
256-byte first-line, trimmed behavior; raw password and MaSt reads preserve
whitespace. `service --print-device-nt-hash` reads `syPW` and hashes it without
passing plaintext through the shell or temporary files. Its raw capture is
limited to 8 KiB; the existing hash-input limit remains 4096 bytes.
`service --print-samba-identity` returns a versioned four-line response with
NetBIOS name, server string and observed fruit model. Legacy name/model overrides
are rejected; deploy no longer forwards them. The manager stages Samba, and so
discovery, under the NetBIOS name of the hostname it maps in `/etc/hosts`, not
the one its settings read derived: the first read at boot runs before ACPd sets
the hostname and falls back to `syNm`.

smbd and rsync are separate units (`service/manager.c` `struct unit`), each with
its RAM image, staging job and publish, and both go through one pump
(`pump_unit`) and one launch gate; `struct unit_kind` holds what differs.
Samba's run maps the hostname, writes its three files and copies smbd; rsync's
run writes rsyncd.conf and copies rsync. Neither waits on, stops or copies the
other, and a payload is valid with Samba's files alone. A run that copies a
binary anew and fails stops the program still serving the old copy. Deploy
writes `checksums` into the payload after the binaries, one `NAME XXXXXXXX`
CRC-32 line each, so a payload on another disk is checked against its own. Staging checks the bytes it reads against them, and a
read error (EIO) counts as damage too; each launch checks the RAM image, so a
copy damaged in RAM, or gone, is copied afresh instead of crashing at every
relaunch, and each program starts only from a copy its unit verified.
`samba/image.c` holds the rules for each image: after damage, on the disk or in
RAM, the next copy waits 5, 15, then 60 s and copies go on every 60 s until the
program serves from one; a retry first checks the disk copy in place, so damage
that has not changed is neither copied into RAM nor logged again, and each wait
is logged once. Disk damage stays reported until a copy's bytes match. That
wait paces copies only; each unit has its own retry for failed runs: Samba's
after 5 s, rsync's after 5, 15, then 60 s, which a publish, another payload or
rsync turned back on starts over. Both units have the same verdicts, which
waiting does not clear: Samba's in the title as `payload=missing|damaged|unreadable`
(missing from every disk, smbd damaged, or a disk that could not read it),
rsync's as `rsync=missing|damaged|unreadable` (rsync or rsyncd.conf missing
from the payload, rsync damaged, or unreadable). Unreadable is a read that
failed with EIO, of the binary or its `checksums` file, or that took longer than
`TC_PAYLOAD_READ_BUDGET_MS` (90 s while bytes remain; a healthy disk returns
smbd in seconds; two reads and the run's other steps fit the 240 s staging
deadline, and the verdict lands within Activate's and doctor's startup windows):
the disk is failing, so a new disk fixes it, not a reinstall. The manager keeps
checking the disk copy, after 5 and 15 s, then 60 s doubling each time up to a
day (`TC_IMAGE_UNREADABLE_MAX_MS`), so a failing disk the user is copying files
off is not kept busy, and clears the verdict once the disk returns it whole.
Each copy's time is logged (`stage: copied smbd in N ms`), and a copy that
took 30 s or more (`TC_SLOW_COPY_MS`) goes out once per manager and program as
a `slow_copy:<name>:<seconds>` heartbeat, a once-report like the verdicts', which
shows how close healthy disks come to the budget. Doctor, deploy and Activate report each with its fix
(`DISK_PAYLOAD_PROBLEMS` in `device/probe.py`, which the host manager tests and
the app's contract test hold to the title); rsync's leave Samba serving. They
stop waiting at the other verdicts, but wait out their window through an
unreadable one, which the manager's next checks may clear. Each
verdict also goes out once per manager as a `payload:<verdict>` or
`rsync:<verdict>` heartbeat; a verdict that changes is reported again. They wait for rsync as for Samba:
until it is ready, a verdict says why it is off, or their window ends, which
reports rsync alone (`rsync_not_running`, which sends the user to Checkup) when
Samba serves. A run's findings about the payload count only from a disk that
stayed the payload's until the run ended: a disk going away reads as files
missing or other bytes. smbd missing from a payload is storage's to report
(`payload=missing`), since Samba's files are what makes one. Activate never
restarts a Samba that serves; it waits for rsync instead. A publish while smbd is still starting
reaches it as a SIGHUP once it is ready. rsync's RAM files go once rsync is
disabled or its payload is gone, including files an earlier manager left. A
symlink at the RAM path is a test build linked in by hand: it must be readable
but need not match. A payload without a `checksums` file is not checked.

`service --print-mast [--timeout-seconds N]` performs a bounded `acp -A MaSt`
read without starting a daemon. The manager uses the same collector asynchronously,
with a native parser, five-second topology confirmation and ten-second recovery
poll. Apple EVFILT_DEVICE notifications trigger earlier storage observations
even if a brief detach/replug leaves inventory unchanged.

`service --print-file-locks FILE...` prints each `fcntl()` byte-range lock
other processes hold on the files (`F_GETLK` from offset 0, resuming after each
lock it reports), as `FILE pid PID read|write START+LENGTH`. A read lock on byte
4 of a `.tdb` file ends in ` (TDB open marker)`: every process that has the TDB
open holds one, and only one holder is listed. Both kernels report a `flock()`
lock too, as `pid -1 write 0+0`: it belongs to an open file, not a process.
Doctor runs it on Samba's RAM TDBs when an smbd waits in `lockf`, to name the
holder.

`service --print-syscall-registers PID...` prints the user registers each
process entered its current system call with, as `PID wchan=WCHAN spsr=… r0=…
r1=… r2=… r3=… pc=…` in hex. It reads the `struct trapframe` at the top of the
process's 8 KB u-area through `/dev/kmem`, at the address `KERN_PROC2` reports,
so it neither stops the process nor reads its memory. It reads the wait channel
before and after the frame and prints `PID: changed during the read` instead
when the two differ: the frame of a process that woke holds another call's
arguments. The NetBSD 6 lane's frame has a pad word after `spsr`
(`TC_TRAPFRAME_FILL`); NetBSD 4's has none, and since the frame ends at the top
of the u-area either way, only `spsr` depends on it. Doctor runs it for each
smbd that has slept in `lockf` for 10 seconds or more: r0-r3 are the call's
arguments, so `r1` tells `flock()` from `fcntl(F_SETLKW)` and `r0` gives the
descriptor.

Cold start grants no sharing services until critical facts validate. Failed
rereads retain permissions only on unchanged interfaces. Each discovery or
telemetry process owns its history directly in C; no process serializes or
transfers retained policy.

Module headers declare cross-module functions; every other function is
`static`. This matters on NetBSD 4, where the linker deliberately does not use
section garbage collection because it can discard required ELF notes, so an
unused external function would stay in the binary. Do not include
implementation `.c` files.

The manager owns foreground Samba in a separate process group and waits for the
whole group before replacing executables or clearing locks. Anonymous stdin
pipes carry parent lifetime to managed roles. Setup work runs in bounded child
jobs so slow disks do not block supervision. Singleton ownership uses a lock on
the existing service executable, without a new PID or status file.

The manager also recovers buffer-cache stalls (NetBSD kern/60584, unfixed in
Apple's NetBSD 4 and 6 kernels; `service/bufstall.c`). `getnewbuf()` can sleep
forever when `buf_lotsfree()` refuses a fresh buffer and every cached one is
busy. The sleepers cannot be killed, and each new connection or job adds one
until `fork()` reaches `kern.maxproc` and the device stops
answering SSH and SMB. Nothing on the recovery path forks or starts a process:
on each loop pass, at most once a second, the manager reads every process's
wait message with `sysctl(KERN_PROC2)` into a static buffer. When a process
has waited in `getnewbu`, `needbuf` or `buf_mall` (8-byte truncated names) for
5 s, the manager sets `vm.bufmem_lowater` to `vm.bufmem_hiwater - 16`, the
highest value the kernel accepts, and reads `/dev` 128 times itself. FFS reads
directories with `bread()`, and each buffer release wakes the sleepers (all of
them on NetBSD 4, one per release on NetBSD 6), which then get fresh buffers.
It repeats the wake every second while a process stays stalled (40 ms on
NetBSD 6, 94 ms on NetBSD 4) and logs only after acting, because NetBSD 4's
`/mnt/Memory` is MFS, whose writes can themselves wait for a buffer. It also
runs while the manager stops, when stuck Samba workers would block the drain.

Apple's value (`hiwater >> 3`, as `buf_setwm()` sets it) comes back 10 s after
the last stalled process, so the raise never keeps the pagedaemon from
draining the cache for long. A process that stays stalled 60 s into a raise
(whenever it started waiting) means the raise is not helping: Apple's value is
restored and the raise is tried again 60 s later. A cache already at its
high-water mark is logged once and rechecked every 60 s. A higher low-water
mark found at start, or a restore the kernel refused, is restored on the next
sample. When an episode ends, one `telemetry --report` heartbeat (at most one
successful report an hour, killed if it outlasts 180 s or the manager stops) reports
`bufstall:<outcome>:<longest wait in s>`, with outcome `resolved`, `stuck`,
`capped` or `failed`; episodes that end before a report can start are merged
into their worst outcome and longest wait. The in-flight report is retained
until delivery; failed starts, failed POSTs and timeouts retry after 60 s.
Episodes ending during a report stay pending; a failed report merges back into
them. These snapshots live only in the manager, so a restart loses pending
reports. A lost HTTP response can cause a duplicate on retry. A permanent raise (PR 353) was
rejected: any low-water mark above `vm.bufmem` stops the pagedaemon from
draining the cache.

Known gaps: a cache at its high-water mark needs a temporary `hiwater` raise,
which is not done; real kernel-memory exhaustion is not fixed (the wake only
lets waiters retry, and the manager's own wake could sleep in `buf_malloc` on
NetBSD 4); on NetBSD 4 the manager's own log writes to MFS could block it;
NetBSD 6 kernel threads (pagedaemon) waiting in `needbuf` belong to process 0
and are not seen; a stall that never clears is never reported, and its stuck
Samba workers still keep the manager from exiting.

The manager also reports two states nothing in user space can clear; it
never restarts the device for them. A process asleep uninterruptibly for 2
minutes without running is named in the title (`stuck=PID:COMM:WAIT:SECONDS`)
and, after 5 minutes, in a `stuck:<comm>:<wait>:<seconds>` heartbeat
(`stuck-cleared:...` when a reported one ends). When a disk MaSt lists has
not mounted for 10 minutes and no other holds the Samba files, so Samba is
off (diskd can hang on a disk that stopped answering, which a restart
clears), the title carries `unmounted=<start>` on the `started=` clock and one
`unmounted:internal` or `unmounted:usb` heartbeat goes out per manager (the
same once-per-manager reports as the payload verdicts': each reason retried
until delivered, then never sent again; the manager remembers 16 of them
(`ONCE_REPORTS`), and when that list is full it logs once and sends no new
one rather than resending one it cannot record). Time
diskd spends in `fsck_hfs` or `newfs_hfs` does not count towards the 10
minutes; a disk that fails while another serves the Samba files is left to
doctor's share checks. Doctor reads both words from the title; `unmounted=`
is its whole verdict. A restart of smbd with an emptied lock directory
(`/mnt/Locks` past 4 MiB, DETAIL.md) sends a `locks_reset:<KiB>` heartbeat.

Apple's mDNSResponder and afpserver remain alive; the AFP preference controls
advertising only. Loopback diskd retains filesystem ownership. Discovery alone
owns its wcifsnd child, started as `/usr/libexec/wcifsnd` because boot.sh's
`/sbin/wcifsnd` guard ignores ACPd's starts. The manager removes any other
wcifsnd, and ACPd's wcifsfs, by PID within about a second of its per-second
process-table read, without replacing a running discovery generation. See
[runtime tests](../../tests/native/README.md).

Telemetry creates only `/mnt/Memory/debug` and `/mnt/Memory/debug.sig`. It locks
the existing `/mnt/Memory` directory to exclude concurrent cycles, including
manual runs, without a lock file or job directory. That directory must be
root-owned, with sticky permissions if writable by other users.

## Telemetry protocol

Schema 2 reports `debug_logging` (Samba or mDNS) and `advertise_afp`, using
null for unreadable settings. It omits `nbns_enabled`: native NBNS is always on,
and the server treats that field as optional. `plan_error` contains a
short critical-facts failure reason and is omitted on success. There is no
`ps` probe, constant daemon label, or live registration-state upload.
This probe has no retained history and does not assert that services stopped.

`service telemetry --daemon` sends a boot heartbeat and then one every 12 hours.
A heartbeat that is not delivered (usually DNS not ready yet right after boot)
retries after 60 seconds, doubling up to 12 hours, and keeps the `boot` reason
until one is delivered.
`--once [reason]` performs one cycle; `--print-payload [reason]` only prints.
`--report [reason]` posts only, without downloading or executing debug jobs.
It neither locks nor cleans the debug workspace, so a running signed job does
not block it. Exit zero acknowledges a successful POST; opt-out, cancellation
and transport failure return nonzero. The bounded HTTP response is discarded,
including any debug request. Managed reports keep their collectors in their
own process group so timeout or shutdown can stop the whole report safely.
`--cleanup` removes stale debug files without collecting or posting telemetry.
An existing cycle or inherited debug lock causes `--once` and `--cleanup` to
exit 75. Cleanup errors return 1 and prevent a new cycle from starting.

The POST to `/v1/router-heartbeats` uses schema version 2 and preserves the
original device fields. It adds `target_lane` (`6`, `4le`, `4be`) and `debug_nonce` (16 random
bytes as 32 lowercase hex characters). Empty or legacy successful responses
without DEBUG end the cycle. New responses contain a top-level JSON boolean:

```json
{"DEBUG": false}
```

For true, `DEBUG_SIGNATURE` must contain a hex Ed25519 signature of:

```text
tc-debug-v1\n<lowercase SHA-512 hex of the exact POST body bytes>\n
```

The line breaks above denote LF bytes. The body includes the fresh nonce and
device identity, so an HTTP intermediary cannot replay a decision or substitute
a different router ID. Use the same deployed public key as binary verification.
Malformed/duplicate fields, wrong types, invalid authorization or transport
failure never cause execution. Unknown JSON fields are validated and ignored.

The client downloads `/downloads/bin/heartbeat<lane>?debug=true` and
`/downloads/bin/heartbeat<lane>.sig?debug=true`. The server maps these to separate
`debug<lane>` files; this preserves the existing NetBSD 4 HTTP proxy exception.
Old requests without the query still download the old heartbeat. New direct
`/downloads/bin/debug<lane>` routes are also supported by the application.

Debug's detached Ed25519 signature covers the complete executable bytes. The
executable is bounded to 1 MiB while receiving, verified before being written
executable, and run with only the cycle reason as an argument. Response bodies
are bounded to 4096 bytes and signatures to 512. Curl config files are disabled;
fixed endpoints are used without following redirects. No server-provided shell
command or arbitrary URL is executed.

Downloads are written with exclusive creation and mode 0600. The signature file
is deleted immediately after verification, including failed verification. Only
a verified executable is made executable. Each completed or failed cycle
removes its files with checked `unlink` calls; unexpected directories are
reported to stderr and syslog and preserved, and symlinks are removed without
following them.
Telemetry never recursively deletes `/mnt/Memory`.

TERM cancels preparation and stops future scheduling. An already-started debug
process is allowed to finish and is reaped by telemetry. Debug inherits the
kernel lock across exec, so even a killed telemetry parent does not make an
active debug job look stale. `TC_DEBUG_LOCK_FD` identifies the descriptor: debug
must keep it open, and pass it to any detached worker, until all work is done.
Curl does not inherit this descriptor.

After releasing its own reference, telemetry reacquires the lock before
cleanup. If a surviving worker still owns it, cleanup is deferred. Recovery
also runs before each cycle and every 30 seconds while the daemon is idle,
without PID files or age-based guesses. A killed process cannot clean up
immediately; the next unlocked recovery removes its leftovers. Power loss
discards these RAM files.

Reset and uninstall stop telemetry scheduling with TERM. Uninstall checks
`--cleanup` before deleting the runtime and defers if debug remains active.
Migration removes the old `/mnt/Memory/tc-telemetry` tree recursively only after
legacy telemetry/debug/heartbeat processes have exited and no nested mounts
remain. New telemetry never recreates that tree.

Server rollout retains legacy heartbeat routes and responses. New capability
fields select the DEBUG response. `TELEMETRY_ROUTER_DEBUG_IDS` is an explicit
comma-separated list of router IDs, empty by default. Install matching debug
executables in `TELEMETRY_ROUTER_DEBUG_DIR` (default `/data/router-debug`) and
publish matching signatures when replacing them. While selected, a router may
run debug on every cycle; this boolean contract does not claim exactly-once jobs.

## Builds and checks

Run `./build/service.sh`, `./build/serviceoldle.sh`, and
`./build/serviceoldbe.sh` in the existing NetBSD VM as root, or use
`make -C build service-all`. These commands build only the unified service and
never download or rebuild a toolchain.

After copying stripped outputs back, wait five seconds and refresh the artifact
manifest hashes. All three service artifacts must be static ARM ELF executables
with the correct byte order and NetBSD note.

From the repository root:

```sh
./build/native/host-check.sh
.venv/bin/pytest
.venv/bin/pytest -n 4 --dist loadfile
TC_NATIVE_SANITIZERS=1 UBSAN_OPTIONS=halt_on_error=1 .venv/bin/pytest tests/native tests/test_deploy_modules.py
make coverage-native
```

The native suite includes checked-in packet and interface regression cases,
pure parser/scheduler tests, a local HTTP server, real curl and a harmless signed
test executable. Test-only builds use a separate key and local endpoints.
Production endpoints/keys are not used by integration tests. Test children have
deadlines and isolated workspaces. Coverage uses LLVM tools and retains its HTML
report under the temporary output directory printed at completion.

## Firmware facts

Measured on the devices. The runtime's design rests on them; code comments
cite them by number. ([DETAIL.md](../../DETAIL.md) has the user-facing side.)

### mDNSResponder, diskd and the network stack

| # | Fact |
| --- | --- |
| F1 | Both lanes ship `mDNSResponder-397.32` inside one crunched static binary (`/sbin/mDNSResponder`, `diskd`, `printd`, `afpserver`, `wcifsfs`, … are hard links). The IPC is the standard `dns_sd` Unix-socket protocol, `VERSION 1`, at `/var/run/mDNSResponder` (created only while the daemon runs). |
| F2 | Apple's client stub from tag `mDNSResponder-379.38.1` (`dnssd/`) compiles unchanged with `-D_DNS_SD_LIBDISPATCH=0` on all three lanes (gcc 4.1.2 on NetBSD 4) and registers/browses against the 397.32 daemon; cost ≈60 KB over a hello-world. |
| F3 | The daemon's interface indexes are the kernel's (`bridge0` = `ifconfig … scopeid`). `interfaceIndex=0` means all interfaces. |
| F4 | Apple's kernel `struct if_msghdr` is 152 bytes on NetBSD 4 while the SDK's is 144, so libc `getifaddrs()` reads the `AF_LINK` sockaddr from inside `if_data`: names are garbage and `if_nametoindex()` returns 0. Walking `sysctl(NET_RT_IFLIST)` ourselves and locating the `sockaddr_dl` by `sdl_family==AF_LINK && sdl_index==ifm_index` yields correct names and indexes. On NetBSD 6 the messages are `RTM_VERSION 4` (`RTM_IFINFO` 0x14, 24-byte `ifa_msghdr` with the index at offset 16, 8-byte `RT_ROUNDUP`); on NetBSD 4 they are version 3 (`RTM_IFINFO` 0xf, 20-byte header, index at 12, 4-byte roundup). Fixtures: `tests/native/fixtures/iflist/`. |
| F5 | Under the Apple stack `ACPd` registers `_airport._tcp`; `diskd` registers `_smb._tcp`, `_adisk._tcp,_airport` and `_afpovertcp._tcp`; the daemon itself registers `_device-info._tcp` (`model=` from `/etc/mdnsd.conf`); `printd` registers printers. `wcifsfs`, `wcifsnd`, `afpserver` register nothing. |
| F6 | `diskd` registers unconditionally: killing `wcifsfs`/`wcifsnd`/`afpserver` closes the ports but leaves the records. ACPd respawns none of them. |
| F7 | `diskd` is load-bearing: it populates `acp -q MaSt` and serves `acp rpc diskd.useVolume`. |
| F8 | `/sbin/diskd -i lo0 -d local.` relaunched by us still serves MaSt and `diskd.useVolume` while its `_smb`/`_adisk`/`_afpovertcp` stay on loopback. |
| F9 | Registering a name another client already holds auto-renames to "Name (2)" unless `kDNSServiceFlagsNoAutoRename` is passed (then the callback reports `kDNSServiceErr_NameConflict`). A stock `diskd` renames SMB and ADisk together after an SMB-only conflict, leaving `syNm` and the hostname alone. |
| F10 | Apple's host records on the LAN: fe80 plus every IPv4 including 169.254, no GUA. Hostname `AirPort-Time-Capsule.local.` (mixed case); SRV targets of our registrations are that hostname automatically. |
| F11 | Killing the daemon is unrecoverable without a reboot: ACPd never respawns it and a hand-started daemon lacks `_airport`. The runtime must never kill it. |
| F13 | `/etc/mdnsd.conf` (RAM root, regenerated each boot) carries `Hardware TimeCapsule6,116` / `TimeCapsule8,119`, `Software 7.8.1` / `7.9.1`, `PrimaryIPv4Interface bridge0`. |
| F14 | Samba's IPv6 `interfaces=` tokens must use the embedded-scope form `fe80:<index hex>::…/64`; Apple's pf opens 445/139/137/138/548 on the WAN iff `usbF & 0x8` in NAT mode; router mode is `(raNA,raDS)`: `(0,0)` bridge, `(0,1)` DHCP-only, `(1,1)` NAT; the guest bridge owns `gnRo`. |
| F15 | Device shell quirks: NetBSD 4 `sed` has no `\|` alternation; `reboot`, `ifconfig` need full paths in non-login shells; `/etc` edits do not persist; `/mnt/Memory` is the 15 MB RAM staging area; `/mnt/Flash` is ≈1 MB. |
| F16 | ACPd keeps one table of `diskd.*` RPC names. A second diskd's duplicate registration is rejected, but when it exits ACPd deletes the names it tried, which are the first diskd's, in registration order (`getHomeVolumeInfo`, `getVolumeCounts`, `useVolume`, ...). `acp rpc` exits 22 for every failure; stderr carries the code (-6727 for a missing name). Measured on NetBSD 4 LE. |

### Apple ACPd

- **Restart hang.** ACPd can accept an `acRB` reboot request, stop
  mDNSResponder, diskd, printd, afpserver, iCloudd, link_local and rtsold, stop
  answering port 5009, and never run `/sbin/shutdown`. The kernel keeps running
  with the same PIDs; sshd, ACPd's child stopped late in that path, survives,
  so SSH still works. The device stays off the network until unplugged. The
  predictor is a child process also named `ACPd`, asleep in nanosleep (`ps`
  shows `nanoslp`; NetBSD 4 truncates it to `nanoslee`); a healthy device runs
  one ACPd whose parent is init. A NetBSD 4 variant also takes SSH and the
  network down. The runtime detects a restart that never happened with the RAM
  marker `/mnt/Memory/acp-reboot` (`device/acpd.py`), which only a real restart
  empties.
- **Real restart timing.** Ports 22 and 5009 close within the same second; the
  old sshd does not outlive ACP. The new boot answers ACP 54 s (NetBSD 6) to
  64 s (NetBSD 4) later, and SSH 2-3 s after ACP. NTP steps the clock by weeks
  just after boot, so no restart check may trust the device clock.
- **Failed gets.** `acp` prints `### get 'X' failed: <<UNKNOWN FORMAT
  CONVERSION CODE %m>>` to stderr and exits 0 for any failed get, unknown
  properties included. Only a failing `acp -q syUT` (`syUT` always exists)
  blames ACPd.
- **IPC vs network listener.** The device's own `/usr/bin/acp` reaches ACPd
  over a Unix socket (`/var/run/ACPIPC` on NetBSD 6, `/tmp/ACPIPC` on
  NetBSD 4), never TCP 5009, so a local answer says nothing about the listener
  a Mac uses. The listener can stop answering for hours while sharing and the
  local `acp` work. The cause is not established: a full file table looks the
  same from outside (both kernels' `accept()` takes a file before dequeuing a
  connection), but the local `acp` and SSH, which need files too, worked in the
  same minute.
- **ACPd quits.** On NetBSD 4 TC6,116 and TC6,113 units, ACPd has exited when
  file sharing started about 25 s after a restart (the same start also
  worked; cause unknown). Its surviving children (sshd, mDNSResponder, snmpd,
  printd, sntpd, dhclient) reparent to init; diskd, wcifsnd, wcifsfs and
  afpserver go. NetBSD 4 never starts ACPd again. A Samba already serving keeps
  serving, since it is the manager's child. ACPd is among the last rows `ps`
  prints, so only a complete listing can show it gone.
- **Disk stalls look like ACPd.** ACPd waits on diskd, which waits on the disk,
  so a disk that stops answering (processes asleep in `biowait`, `biolock` or
  `genput`, or `diskd`/`mount_hfs`/`fsck_hfs`/`newfs_hfs` stuck on any channel)
  stops MaSt, `syUT` and the TCP listener too. ACPd's reads take up to 45 s,
  long enough for a stalled `mount_hfs` to cross a 120 s threshold between two
  process listings.

### Disks and HFS

- `gpt` exits 0 after `gpt: error: bogus map` and after `no room for the backup
  header` (a legacy MBR partition reaching the last sector), and diskd's erase
  stops only on a failing `gpt create`, `add` or `label`, so Erase Disk then
  formats wedges that exist only in memory. NetBSD 6's `gpt` names the internal
  disk `rwd0c`.
- diskd's erase formats `dk0`-`dk2` by name; wedge names are unique across
  disks, so a USB disk holding those names (from APconfig/APswap/APdata GPT
  labels) gets formatted instead.
- Apple's HFS stores at most 3,802 bytes in one attribute: Darwin 8's
  inline-only `hfs_setxattr` allows (attributes B-tree node size - 20) / 2 -
  284, rounded down to even, for its 8 KiB nodes, and fails larger values with
  `E2BIG`. It is the only writer of attribute records, AFP included.
- The HFS driver's journal can panic (`jnl: start_tr: active_tr is NULL`,
  recorded in `/mnt/Flash/dmesg.panic`) under bursts of link conversions, and
  replay after that unclean restart can overwrite recently reused blocks. Each
  native symlink conversion therefore ends with `sync()`.
- `fsck_hfs` holds its output until exit on a pipe; on a terminal (`ssh -tt`)
  each `** Checking …` line arrives as the step starts. It has crashed with
  SIGBUS (status 138) repeatably on one volume.
