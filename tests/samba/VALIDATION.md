# Doctor lists processes stuck in the kernel before touching the disk (2026-10-06)

A user's TimeCapsule8,119 on v3.2.0-3 passed doctor after deploying, then
14.5 h later failed it: no Bonjour record visible from the Mac, and
`smbclient -L` timing out at 20 s three times (one earlier attempt took
14.7 s), while SSH and every RAM read still worked. The same device showed the
same pattern on 2026-09-18, 15 h after a v3.0.0-1 deploy, then with SSH
`echo` timing out too. Doctor recorded nothing that showed what the device's
processes were waiting on, and its own SMB retries could each have left one
more smbd child blocked.

Doctor now runs `ps` right after the SSH login check and before any check
that reads the data disk. ps reads the process table through sysctl, so it
still answers while processes are blocked on the disk. A process in an
uninterruptible sleep (`D`, not a kernel thread `K`) whose sleep time (`sl`)
is at least 60 s is reported as a FAIL naming it, its wait channel and the
time. When smbd is one of them, the authenticated SMB checks are skipped,
since each new connection forks an smbd child that blocks the same way and
cannot be killed (the kernel allows 84 processes). When ps itself does not
answer in 15 s, doctor warns, tries SMB once without retries, and reads only
the RAM logs. The full listing goes into the debug fields of a failing run.
Startup grace does not mask the FAIL.

Checked on the devices and in the SDK sources on the VM:

- `ps` state comes from `kinfo_proc2.p_stat` and `p_flag`: `D` is
  `LSSLEEP` without `L_SINTR` (bin/ps/print.c, both trees). `sl` is
  `p_slptime`, the representative LWP's `l_slptime`: incremented once a
  second for any sleeping LWP (NetBSD 4 `schedcpu`, NetBSD 6/7
  `sched_lwp_stats`) and reset at every sleep and wake-up. ps only caps the
  display at 127. NetBSD 6's `[system]` LWPs and NetBSD 4's kernel threads
  sit in `DK` with `sl` climbing to 127 (`mod_unld`, `amc6821c`, `sccomp`),
  so `K` must be excluded.
- A process making progress does not reach 60 s: a busy smbd child on
  NetBSD 6 (76 min CPU), sampled once a second for 60 s, was `R` 47 times,
  `D` 8 times (`biowait`, `ahcicmd`, `sl` 0), `S` 5 times; `dd` writing and
  then reading 2 GiB on NetBSD 4's data disk was almost always `R`. Its
  `inblk`/`oublk` stayed at 0-33 for 2 GiB through HFS, so block counters
  cannot show progress.
- Limit: NetBSD 6 waits for a free buffer with `cv_timedwait(..., hz / 4)`
  (vfs_bio.c `needbuf`, getnewbuf), so a process in the buffer-cache stall
  wakes four times a second and its `sl` stays near 0. NetBSD 4's
  `getnewbuf`/`buf_malloc`/`biowait` sleeps have no timeout. Apple's
  multi-LWP daemons (ACPd 29 LWPs, afpserver 21, printerd 7, mDNSResponder
  2-3) show only a representative LWP; every LWP of theirs was interruptible
  (`ps -axs`). The manager can follow a process across samples and per LWP; doctor's single
  snapshot cannot.
- `ps` over SSH: 0.55-0.75 s on NetBSD 6, 0.66-0.82 s on NetBSD 4, about
  0.1-0.2 s over an SSH `true`. The parser read both devices' real listings
  (40 and 55 rows, one blank row) and reported nothing stuck.
- `tcapsule doctor` passed on NetBSD 6 and NetBSD 4 with this change.

# Samba and discovery start under the hostname's NetBIOS name (2026-10-06)

Doctor on v3.2.0-2/-3 failed only "NBNS query for '<name>' timed out" when it
ran 26-29 s after the manager started, and passed when rerun about 35 s later:
a user's TimeCapsule6,116 right after a flash power cycle, and three times on
our own NetBSD 6 and NetBSD 4 devices. Their discovery logs show why. The
manager's first settings read runs before ACPd sets the hostname, so
`identity_derive()` fell back to a NetBIOS name from syNm (`JAMESSAIRPORTTI`),
and Samba and discovery started under it. The next read, `SETTINGS_MS` (30 s)
later, saw the hostname and derived `JAMESS-AIRPORT-`; the manager restaged
and reloaded Samba and restarted discovery, replacing the wcifsnd child, so
for several seconds no name answered and Bonjour re-registered (12:57:31 ->
12:58:01, 14:19:07 -> 14:19:37, 22:39:48 -> 22:40:18). It happens on every
boot where the two names differ, even only in case: another user's log shows
`TMC` (syNm) and `tmc` (hostname), manager start 19:12:09 and the discovery
restart 19:12:39. Doctor's startup grace did not cover it because native NBNS
reported ready, and a timeout after ready is deliberately not maskable.

Staging now sets the NetBIOS name from the hostname it maps
(`normalize_netbios_name(m->hostname)`, the same derivation `identity_derive()`
tries first), keeping the settings read's name only if the hostname yields
none, which no ACPd hostname does. The settings worker derives its identity
from the manager's hostname rather than its own `gethostname()`, so the read
after the hostname is set produces the same name and changes nothing.
`--print-samba-identity` passes `tc_hostname_read()`. A rename restages at
once with both the new mapping and the new name even while settings reads
fail; an earlier version of this fix waited for a fresh settings read, which
left a rename unmapped (and Samba logins stalled, issue #54) while ACP reads
failed. Host tests previously derived the name from the Mac's own hostname
(`gethostname()` ignored `TC_TEST_HOSTNAME`); they now use the rig's.

`tests/native/test_manager.py` records the NetBIOS name each smbd start and
reload read. A cold boot whose first read ran without a hostname starts smbd
and discovery once, under `kevins-airport-` rather than `KevinsAirPortTi`,
and the next read reloads nothing; a rename reloads once under the new name
and restarts discovery once, also while every settings read fails; a failed
name read keeps the server string and follows a rename. Removing the
staging-time name fails four of these; letting the settings read ignore the
manager's hostname fails the cold-boot one. `pytest -n auto` passed;
`build/native/host-check.sh` passed under gcc 13.3 (Ubuntu 24.04).

Lane builds produced NetBSD 6 `72a3a69c...` (376076 bytes, was 376020),
NetBSD 4 LE `7cf35083...` (333372, was 333296) and NetBSD 4 BE `bd1d751e...`
(332792, was 332716); `disable_data_faultahead` on every lane and the fork
repair on NetBSD 6 passed. A comment-only edit to `pump_stage` afterwards
rebuilt to the same three hashes. Device validation (one discovery start and one
wcifsnd registration after boot, doctor passing NBNS about 30 s after the
manager starts) is still to be done.

# The device hostname always fits /etc/hosts; its guard is gone (2026-10-05)

The manager refused to map a hostname outside 1-255 of `A-Z a-z 0-9 . _ -`
(`tc_hostname_plain()`), logged it and started Samba unmapped, and doctor
reported it as `hostname_invalid`. Nothing on the devices can produce such a
name:

- Every ACPd checked (products 106 and 116 at 7.5.2-7.8.1, 119 and 120 at
  7.7.3-7.9.1; see "Doctor reads syDN") sets the kernel hostname by
  converting `syDN`, else `syNm`, to `[A-Za-z0-9-]`, at most 63 bytes, else
  formatting `Base-Station-%02x%02x%02x`, then lowercasing it.
- The only other setter is `/sbin/dhclient-script`, which ACPd's dhclient
  runs in bridge mode (`/sbin/dhclient -q -d` on NetBSD 4). It sets the
  hostname from a DHCP `host-name` only when the current one is empty or the
  previous lease's, and first strips everything outside `[-.a-zA-Z0-9]`; ACPd
  sends its own name as `host-name`, and the NetBSD 4 lease echoed
  `airport-time-capsule`. `/etc/rc.d/network` has no `hostname` in `rc.conf`
  and no `/etc/myname` on either device.

Removed: `tc_hostname_plain()` and its uses in the manager's staging and in
`tc_hosts_update()` (which no longer returns `EINVAL`), the
`DeviceHostnameProbeResult.plain` rule that mirrored it, the `hostname_invalid`
doctor check, and the tests of those paths. Waiting for an unset hostname,
mapping it, removing our stale lines after a rename and doctor's waiting and
unmapped checks stay.

Doctor repeats the manager's reading of `/etc/hosts`, and the two had
drifted: doctor's stale-line pattern still allowed only `[A-Za-z0-9._-]` and
counted characters, its mapping check split words on any Unicode whitespace
with no 1023-byte cut, and its probe parser split lines at CR and other
separators. Doctor now reads a line as `our_line()` and `maps()` do, in
bytes: our form is `127.0.0.1<TAB><n> <n>.local` with `<n>` up to the first
space and at most 255 bytes; a mapping is any word after the address, before
`#`, separated by space, tab or CR, within the first 1023 bytes; lines end
only at LF; doctor no longer trims the hostname, which the manager uses as it
is. Doctor decodes the probe's output as UTF-8 with replacement, so only valid
UTF-8 lines are read byte for byte. `tests/native/test_hosts.py` runs the real `tc_hosts_update()` and
doctor's rules on the same 33 lines (tab and UTF-8 names, 255/256-byte and
254/256-UTF-8-byte names, CR, vertical tab, form feed, NBSP, the 1023-byte
cut) and states each answer; against the previous doctor rules 9 of them
disagreed.

Lane builds (12 s for all three; `build/service.sh` compiles every source in
one `gcc` call with no cached objects, so each build is from scratch) produced NetBSD 6 `f0d2e69c...`
(376020 bytes, was 376324), NetBSD 4 LE `d2c1ea7c...` (333296, was 333608) and
NetBSD 4 BE `bee2bd9d...` (332716, was 333028); both build checks passed
(`disable_data_faultahead` on every lane, fork repair on NetBSD 6). The same
VM rebuilt main's unchanged source to the committed hashes byte for byte. Comment-only edits
to `hosts.c` and `service.h` afterwards rebuilt to the same three hashes.
Deploy and doctor passed on NetBSD 6 and NetBSD 4 LE (NetBSD 6's first doctor,
about 70 s after boot, failed only its NBNS query and passed a minute
later). On NetBSD 4, `syDN` set to `Dn Test.Name’s` and a reboot gave
`hostname found: dn-test-names` and `mapped dn-test-names`; `hostname
dn-live-rename` while running gave `hostname changed`, `removed the stale
mapping for dn-test-names` and one line for the new name, with smbd still
running; clearing `syDN` and rebooting restored `airport-time-capsule`.
`pytest -n auto` passed (3619 tests), and doctor passed again on both devices
with the final code.

# Doctor reads syDN before syNm for Apple's Bonjour host (2026-10-05)

Telemetry from a TimeCapsule6,113 (NetBSD 4 LE) on v3.2.0-3 failed doctor
only on `_smb._tcp target host label 'Time-Capsule-dd5301' does not match
runtime mDNS host label 'time-capsule-nm'`: `syNm` was "Time Capsule NM",
while the kernel hostname and Apple's Bonjour host were
`time-capsule-dd5301`. ACPd reads `syDN` before `syNm`, and the probe skipped
`syDN`.

Every ACPd checked (products 106 and 116 at 7.5.2-7.8.1, 119 and 120 at
7.7.3-7.9.1) names the host the same way. The Bonjour host routine (LE 7.8.1 `0x42e110`, NetBSD 6 7.9.1 `0x4eebb8`) converts `syDN` and
uses it when the converted label is not empty, else converts `syNm`, else
formats `Base-Station-` with the last three `raMA` bytes, else `waMA`. The
kernel hostname routine (`0x243ec4`, `0x270db8`) reads the same keys in the
same order but tests `syDN` before converting it, fills its fallback from
unchecked stack bytes, and lowercases the result. Both call the same
conversion (`0x442194`, `0x501578`). Binaries checked: products 106 (BE) and
116 (LE) at 7.5.2, 7.6, 7.6.1, 7.6.3, 7.6.4, 7.6.7, 7.6.8, 7.6.9 and 7.8.1,
and products 119 and 120 at 7.7.3, 7.7.7, 7.7.8, 7.7.9 and 7.9.1, taken from
Apple's firmware with the repo's basebinary keys and rebuilt from the FFS
image through ACPd's indirect blocks; the three that also came off devices
(116 7.8.1, 106 7.8.1, 119 7.9.1) matched byte for byte. The conversion is the
same instruction sequence in every NetBSD 4 build and in every NetBSD 6
build; the NetBSD 6 one, compiled differently, was checked by hand. `syDN`
is a type 2 (raw string) property, like `syNm`.

The probe now reads `syDN` (`acp -q syDN 2>/dev/null`) and derives the host
label from it first, and doctor's debug context reports it as
`system_dns_name` (ACPd calls the property `SYSDNSNAME`). Device
evidence, NetBSD 4 LE, each value set with `acp syDN=...`, then Flash
flushed and `acp acRB=00000000`:

| `syDN` | kernel hostname | Bonjour host | main's doctor | this doctor |
| --- | --- | --- | --- | --- |
| unset | `airport-time-capsule` | `AirPort-Time-Capsule` | pass | pass |
| `Dn Test.Name’s` | `dn-test-names` | `Dn-Test-Names` | host label FAIL (IPv4, IPv6) | pass |
| `!!!` | `base-station-edffbf` | `AirPort-Time-Capsule` | not run | pass |
| empty (cleared) | `airport-time-capsule` | `AirPort-Time-Capsule` | not run | pass |

The instance name stayed `AirPort Time Capsule` in every row, and the manager
mapped each kernel hostname in `/etc/hosts`. An unset `syDN` prints
`### get 'syDN' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>` on stderr and
exits 0 on both devices; a cleared one prints an empty line. The `!!!` row
is why the kernel hostname cannot stand in for the Bonjour host. NetBSD 6
(`syDN` unset) passed doctor with no FAIL or WARN, and `pytest -n auto` passed
(3605 tests). The probe tests run the real probe shell with a stand-in `acp`
that prints failures on stderr, as the devices do.

# Manager loop pass as a build setting (2026-10-04)

The manager's loop woke at least once a second through a literal 1000 ms;
buffer-stall sampling runs on those passes, so the host rig's stall tests
could not sample faster than once a second. `TC_MANAGER_PASS_MS` (default
1000) now names it. Clean lane builds with the change produced the committed
service binaries byte for byte (NetBSD 6 `e6f51006...`, NetBSD 4 LE
`8c90462b...`, NetBSD 4 BE `88147dae...`), so nothing on the devices changes.
The host rig samples every 250 ms with shorter stall timings, and
`tests/native/test_manager.py` takes about a third less time; sixteen runs
of it beside full parallel suites passed.

# ACP collector reaps each child when its output closes (2026-10-04)

`acp_collect_pump()` set `eof` when `read()` returned 0 and left the reap to
its next call. After EOF the collector offers no descriptor
(`acp_collect_fd()` returns -1), so nothing woke the caller before its
100 ms poll: every `acp -q` key cost at least 100 ms more than the child
took. The pump that reads EOF now reaps the child and finishes the key. A
child can close its output a moment before it becomes reapable, so for the
first 100 ms after EOF the collector polls every 5 ms, then every 100 ms as
before (a child that closes stdout and keeps running is still bounded by
its per-key timeout).

On the host a telemetry `--once` cycle (16 keys) went from 1.85 s to 0.10 s.
On the NetBSD 6 device, `service --print-link-plan` (the 10 keys discovery
and telemetry collect) took 1.60-1.74 s over SSH with the deployed binary
and 0.53-0.82 s with this build; the two printed the same plan. On the
NetBSD 4 LE device the same command took 1.80-1.92 s with main's binary and
0.69-0.73 s with this one, again with the same plan. Deploy and doctor passed
on both devices (86 checks each; a doctor run 29 s after the NetBSD 6 boot
timed out its NBNS query once, and passed when rerun after startup).
`test_acp_capture.py` covers both cases: an already-exited child's key
finishes on the pump that reads EOF (main's collector left it pending for
the next poll), and a child that closed its output but runs gets a 5 ms
poll, then a 100 ms one.

Clean lane builds of the unchanged tree first reproduced the committed
service hashes, so only this change moved them.

| Lane | Stripped bytes | SHA256 |
| --- | --- | --- |
| NetBSD 6 | 376228 | `e6f5100619e2c3f9cb46cef573b23843ba6dafca358bdcdc623ec64457e7bd09` |
| NetBSD 4 LE | 333548 | `8c90462bf79fce2ca69cfd0d8cd5cdbc4af3487f2333bec8c393f83d47d919bf` |
| NetBSD 4 BE | 332968 | `88147dae183ea42c5faea61dcd135abd8b68a8772db53c9cf97f9db51dbee43a` |

# Bonjour service label preservation (2026-10-03)

The v3.2.0-1 telemetry case registered `AirPort Time\u00a0Capsule ` through
Apple's default-name callback, but host discovery stripped its trailing space
before `dns-sd -L`. Browse parsing now consumes Apple's minimum-width metadata
columns in bytes and preserves the remaining label. Lookup and diagnostic
parsing split physical output records without treating Unicode separators as
newlines. A label containing `...STARTING...` remains service data.
Apple's `printtimestamp()` writes `%2d:%02d:%02d.%03d` and two spaces, so
before 10:00 a line starts with a space; browse, lookup and error parsing share
one timestamp pattern that accepts both, and every ASCII space after it is
framing (fullnames escape ASCII spaces as `\032`).

Doctor expects `syNm` exactly as Apple advertises it. The runtime naming probe
no longer strips it (the device shell's command substitution already drops
only the trailing newline), and doctor selects only that exact label or
Apple's conflict rename of it (`"Capsule "` becomes `"Capsule  (2)"`: mDNSCore
appends ` (2)` without trimming). A trimmed spelling is another service. The
ACP-derived whitespace-hint matching from the first version of this branch is
gone. Observation/candidate identities and macOS profile storage keep the
exact label; macOS lists show it trimmed, or the host when it is blank. The
service's C identity code still trims its display name (plan output, Samba
server string fallback); it registers nothing, so it is unchanged and its
parity test now covers only the shared 63-byte cut and control-character
mapping.

Doctor's expected Bonjour host now follows ACPd instead of `/bin/hostname`.
ACPd converts `syNm` to a host label (LE ACPd `0x442194`): ASCII letters and
digits keep their case, `'` and UTF-8 `’` are dropped, an inner `-` is kept,
any other byte becomes one `-`, no leading or trailing `-`, at most 63 bytes.
When that is empty, the Bonjour host (`0x42e1a0`) is `Base-Station-` and the
last three `raMA` bytes, else `waMA`. The kernel hostname routine (`0x243ec4`,
`sethostname`) makes the same conversion, but its fallback ignores the `raMA`
read result and formats an uninitialized stack buffer: `base-station-edffbf`
after three reboots, bytes that look like an ARM stack address. The probe
reads `raMA` and `waMA` (an `acp -q` error line is not a MAC; on both devices
`acp -q` prints a failure on stderr, which the probe drops) and ports the
conversion; `/bin/hostname` is the last resort. `syDN`, which ACPd reads
first, is unset on both test devices, so the probe skipped it (it reads it
since 2026-10-05; see "Doctor reads syDN"). The
`/etc/hosts` mapping keeps the kernel hostname, which smbd resolves.

macOS SMB URLs escape a label's dots and backslashes as DNS does. With
`smbutil view -N`, `Time.Capsule` failed as `smb://Time.Capsule._smb._tcp.local`
and `Time%2ECapsule` ("No route to host") but reached the server as
`Time%5C.Capsule`; `Time\Capsule` failed as `Time%5CCapsule` and connected as
`Time%5C%5CCapsule`; `%20%20%20` reached the three-space name unescaped.

Device evidence (NetBSD 4 LE, 2026-10-03/04). `/usr/bin/acp` is ACPd: an
argument `code=value` takes the 4-byte code and everything after `=` verbatim;
ACPd's property table types `syNm` as 2, a raw `strlen`/`memcpy` copy (at most
256 bytes), in both the LE and BE 7.8.1 binaries. With the user's permission,
`acp syNm=...` set `"   "`, `Time.Capsule`, `Time\Capsule` and `"   "` again,
each followed by `acp acRB=00000000`; Bonjour labels changed only after the
reboot, and every value was stored and advertised byte for byte
(`\032\032\032`, `Time\.Capsule` and `Time\\Capsule` in `dns-sd -L`). The
Bonjour hosts were `Base-Station-619b7d` and `Time-Capsule` (kernel hostname
`time-capsule`). The device was then set back to its original
`AirPort Time Capsule`.

- Doctor against the three-space name: `main` dropped the label (its parser
  stripped it to empty) and failed four Bonjour checks; the first version of
  this branch failed IPv6 because its expected name fell back to the host
  label; with the exact name only the host-label check failed
  (`Base-Station-619b7d` against `base-station-edffbf`). With the ACPd host
  derivation doctor passed with no FAIL or WARN on NetBSD 4 (three spaces,
  then `AirPort Time Capsule`) and on NetBSD 6 (`James's AirPort Time
  Capsule`, `jamess-airport-time-capsule`). `Time.Capsule` also passed.
- The dedicated Python label suite runs 183 cases from 27 tests. It exercises
  real adapters through exact-name subprocess/zeroconf fixtures, independent
  Apple output literals, escaped fullnames, fragmented UTF-8, withdrawals,
  distinct identities, both IP families, ADisk validation, exact-label doctor
  selection, conflict renames and the blank-name host check, which fails with
  the hostname-based label.
- Naming probe tests run the real probe shell against a stand-in `acp` and
  check leading/trailing spaces, NBSPs, U+2028, whitespace-only names, the
  `raMA` fallback and an `acp` error line; conversion tests cover every ACPd
  branch. The C parity test adds an inner NBSP.
- Existing in-flight replacement, scoped removal and grace-period withdrawal
  tests also run with the telemetry label. The helper cancellation fixture
  uses whitespace-bearing labels and verifies that all owned children exit.
- Nine new Swift tests cover raw UTF-8 preservation, coding and registry
  save/reload, display edits and names, service/name-based SMB URLs (including
  the device-verified escaped forms, which fail 58 assertions on the previous
  policy) and distinct labels. Four new paired provider fixtures pass through
  the existing Swift contract tests, including leading whitespace, a Unicode
  separator and missing MACs.
- Zeroconf retains its upstream ASCII-control rejection (including tabs).
  Native parsing preserves observed bytes; the zeroconf test verifies an
  unresolved outcome and closes both transports. Supporting invalid control
  labels through dependency bypasses would add code without helping this valid
  NBSP/trailing-space case.
- Review fixes: a `dns-sd -B` capture from macOS with `TZ=UTC` (hour 1) gave
  three parse errors and no instances on the first version of this branch;
  `-L` fullnames kept a leading space at every hour and the timestamp before
  10:00; `Error code -65570` was missed before 10:00 (also on `07a78fe0`).
  Fake dns-sd fixtures now print Apple's framing with a single-digit hour.
- Final full parallel local pytest on `origin/main`: **3,479 passed**, 183.25
  seconds. Python 3.14 emitted 44 existing `forkpty()` deprecation warnings in
  CLI tests.
- Final full Swift suite: **665 passed**, 19.40 seconds. Ruff and
  `git diff --check` passed.
- Host code only: no NetBSD artifacts changed.

# Runtime log of diskd.useVolume errors (2026-10-03)

The manager's disk claim discarded acp's stderr, so runtime.log only showed
`command /usr/bin/acp failed ... (exit=22)`; acp exits 22 for every failed
RPC. The claim now runs acp through `/bin/sh` with stderr captured and stdout
dropped, and logs `storage: claim ROOT failed: <acp's line>`.

- Native host tests: 783 passed. The storage test's fake acp prints a plist on
  stdout and acp's failure line on stderr: the log has that line exactly once
  and no plist; a silent failure logs `acp printed no error`.
- VM: all three service lanes rebuilt from this tree (NetBSD 6 376,060 bytes,
  NetBSD 4 LE 333,372, NetBSD 4 BE 332,792); manifest hashes updated; the
  manifest and deploy tests passed (183).
- NetBSD 6 deploy: the boot claim of `/Volumes/dk2` succeeded at attempt 1 with
  no failure line.
- NetBSD 4 LE deploy, then three collisions with the real diskd binary and a
  manager restart (rc.local): each claim attempt logged
  `storage: claim /Volumes/dk2 failed: ### RPC function "diskd.useVolume" failed: -6727`.
  After a reboot the claim succeeded at attempt 1.
- Only the claim path changed, so no Samba, doctor or smbtorture runs.

# diskd RPC registry guard and deploy past lost names (2026-10-03)

Field telemetry (v3.1.0 to v3.2.0) showed deploys failing at
`select_payload_home` with `use_volume_rcs=22,22` on all three device families.
`acp rpc` exits 22 for every failed RPC; its stderr (discarded) carried -6727.
ACPd keeps one table of `diskd.*` RPC names. ACPd starts Apple's diskd at boot
and whenever it applies a sharing setting; that second diskd's duplicate
registration is rejected, and when it exits ACPd deletes the names it tried,
which were the manager diskd's, in registration order. boot.sh now replaces
`/sbin/diskd` (RAM root) with a guard that runs the real binary, hard-linked as
`/usr/libexec/diskd`, only for `-i lo0`. The shared mount-guard script keeps
acp's error code and accepts a mounted volume whose claim failed with -6727.
Doctor reports `diskd.getVolumeCounts` routing as INFO.

- Mechanism (NetBSD 4 LE, before the fix): after boot `getVolumeCounts` and
  `useVolume` answered; each extra stock diskd removed the next name; the
  second extra one left `useVolume` failing with -6727 twice while
  `/Volumes/dk2` stayed mounted, the field signature. A reboot restored it.
- Guard prototype in rc.local, three boots each way on NetBSD 4: with the guard
  ACPd's boot start became a `(sh)` zombie and the names stayed whole (one forced
  collision removed only the first name); ACPd.log showed no new messages.
  afpserver's boot "No HFS+ volumes found" appears with and without the guard,
  on NetBSD 4 and 6, so it is unrelated.
- Device shell semantics (NetBSD 4 and 6, scratch dir, no ACP): `ln -f` rerun,
  `mv -f`, `$(cmd 2>&1 >/dev/null)` keeping exit 22 and the sed code
  extraction, wrapper exec/exit paths, the guard heredoc layout (first run,
  rerun, `ln` failure). A heredoc body placed after a line ending in `||`
  swallowed the next boot.sh command; restoring that layout failed 11 of the
  13 boot tests.
- Full parallel pytest: 3,285 passed, 34,006 subtests passed. Ruff passed.
- NetBSD 6 deploy: `/sbin/diskd` is the 293-byte guard, `/usr/libexec/diskd`
  shares ACPd's inode, the manager's diskd runs as
  `/usr/libexec/diskd -i lo0 -d local.`, ACPd's boot start exited through the
  guard (`(sh)` zombie, one runtime.log line for `-i  -d local.`). Doctor
  passed with `INFO diskd RPC: getVolumeCounts answered`.
- NetBSD 4 LE deploy: the same; three more ACPd-style starts were ignored and
  logged; doctor passed, `getVolumeCounts answered`.
- Lost names (three collisions with the real binary on NetBSD 4): doctor still
  passed and reported `getVolumeCounts failed: -6727`; the mount guard returned
  success with `use_volume_rcs=22 mounted=yes use_volume_errors=-6727` and
  payload selection chose `/Volumes/dk2`; a full redeploy in that state passed;
  uninstall in that state removed `/Volumes/dk2/.samba4`. After the uninstall
  reboot `/sbin/diskd` was Apple's binary again (140 links) and
  `/usr/libexec/diskd` was gone; a redeploy restored the runtime.
- Not run: a live-applied AirPort Utility change on a guarded device, and the
  Samba suites (Samba is unchanged).

# Bonjour off-network skip validation (2026-10-01)

Doctor now requires both attempted Bonjour backends to report absence before
skipping a missing advertisement off-network. A failed native `dns-sd` result
that saw a record keeps the zeroconf failure and native diagnostics available.
The existing successful-fallback selection is unchanged.

- The regression failed before the fix for wrong ports, hosts, addresses,
  foreign devices and unresolved browse hits, independently over IPv4 and
  IPv6 (10 failing subtests). All 16 cases pass after the fix, including
  absent records, unavailable native discovery and valid native fallback.
- Focused doctor, printer, network, CLI doctor and app API tests: 496 passed,
  143 subtests passed. Ruff and `git diff --check` passed.
- Full parallel local pytest suite: 3,079 passed, 33,255 subtests passed in
  145.36 seconds. The native telemetry cleanup failure observed during the
  earlier review did not recur; its code is unchanged by this fix.
- No NetBSD device access, deployment or VM build; this changes host-side
  doctor logic only.

# Regression sensitivity checks

The implementation was checked by deliberately restoring bugs in a disposable
Samba checkout, rebuilding the affected target, and running the named case.
Each mutation below failed at runtime; the source was restored and rebuilt after
each check. All cases passed again after restoration.

Patch numbers in dated entries are as they were then. Later merges folded 0037,
0039, 0040 and 0042 into 0038 plus overlay files, 0030 and 0034 into 0031, 0025
into 0023, and 0026 into 0003. 0020 was dropped: build/_samba4x.sh already
clears configure's getifaddrs results. 0032's pthreadpool driver moved to
tests/samba/tc_pthreadpool_sync_test.c. 0006, 0009, 0010, 0011, 0012 and 0044
were dropped and 0022 was replaced on 2026-09-26 (see "No-pthread workaround
review" below). Later that day every patch was renamed by the Samba component
it changes and split where it held more than one change: 0001 into 0001 and
0049, 0003 into 0003 and 0050, 0013 into 0051 and 0013, 0031 into 0052, 0053
and 0031, 0038 into 0038 and 0054-0057, and 0045 into 0045 and 0058-0060 (see
"Series restructuring" below).

| Deliberately broken behavior | Case that rejects it |
| --- | --- |
| Treat a negative worker read result as an oversized successful read | `read_error` |
| Report open talloc frames from the no-pthread atexit handler (upstream, without 0022) | `exit_frames` (NetBSD 6 device, 2026-09-26) |
| Allow worker creation beyond the configured bound | `limits` |
| Insert queued work at the head rather than the tail | `queue` |
| Reject a matching live durable reconnect immediately | `transition` |
| Remove the bound on live-open retries | `exhausted` |
| Wait from inside the locked recreate callback | `transition` |
| Leave a dangling pool pointer after freeing the pthreadpool | existing pthreadpool lifecycle test, ASan exit 86 |
| Restore the zero-length fd array during worker shutdown | `read`, UBSan detected in worker and rejected by parent |
| Run the aio_fork FIFO synchronously instead of from the event loop | `queue`, `queued_fork_failure` (2026-09-28) |
| Retire a freed request's helper instead of leaving it to finish | `cancel_active`, `teardown`, `orphan_error` (2026-09-28) |
| Keep asserting that a helper freed with its tree connection is idle | `teardown`, smb_panic (2026-09-28) |
| Report an exhausted live-open reconnect as FILE_NOT_AVAILABLE | `exhausted` (2026-09-28) |
| Leave the talloc stack tracker pointer dangling at exit | `exit_late_frames`, ASan exit 86 (2026-09-28) |
| Read the NULL tracker in the no-pthread atexit handler (upstream) | `exit_no_frames`, ASan exit 86 (2026-09-28) |
| Decide HFS growth from smbd's cached size without refreshing it | `stale_size`, `exceeds` (2026-09-28) |
| Refuse growth past the available space on every filesystem, not only HFS | `not_hfs` (2026-09-28) |
| Count the available space in `f_bsize` instead of `f_frsize` units | `boundary` (2026-09-28) |
| Refuse growth equal to the available space | `boundary`, `exceeds` (2026-09-28) |
| Measure growth from offset 0 instead of the file's current end | `boundary` (2026-09-28) |
| Allow growth whose current size cannot be read | `fstat_error` (2026-09-28) |
| Check growth inside the 64 MiB unchecked margin too | `unchecked` (2026-09-28) |
| Refuse growth when the descriptor's volume cannot be queried | `no_volume`, `not_hfs` (2026-09-28) |
| Drop the growth check from synchronous writes (`real_write_file`) | `call_write` (2026-09-28) |
| Drop it from asynchronous writes (`pwrite_fsync_send`) | `call_pwrite_send` (2026-09-28) |
| Drop it from SET_INFO end-of-file (`vfs_set_filelen`) | `call_set_filelen` (2026-09-28) |
| Truncate first, then check | `call_set_filelen` (2026-09-28) |
| Drop it from server-side copies (`vfswrap_offload_write_send`) | `call_offload` (2026-09-28) |
| Return -1 with errno 0 after a failed size refresh | `fstat_error`, `call_pwrite_send` (2026-09-28) |
| Migrate an all-zero legacy FinderInfo and verify it (v3.1.1) | `tdb`, "native verification failed" (2026-09-30) |

The retry tests check eventual success/failure and unlocked waiting without
asserting the particular retry limit of 34. Timing-sensitive cancellation is
also run under sanitizers. An earlier row here, a stale read watcher during
worker replacement, went with the replacement itself on 2026-09-28: a freed
request's helper now finishes instead of being retired.

Samba 4.24.3 validation on 2026-09-12:

- Full local pytest suite: 1,853 passed. Ruff and artifact-manifest checks passed.
- Fresh Linux checkout, full patch series, and 40 regression invocations passed
  with ASan/UBSan. The existing pthreadpool invocation contains three lifecycle cases.
- The same 40 invocations passed on the NetBSD 6 device discovered by `dns-sd`
  at `192.168.1.218`. Temporary executables were removed afterward.
- All three NetBSD `smbd` variants and static regression drivers were rebuilt as
  root using the existing VM toolchains. Stripped `smbd` sizes were 10,159,896
  bytes (6), 10,170,056 bytes (4 LE), and 10,168,500 bytes (4 BE), all below the
  existing build ceilings. Distribution binaries and hashes were refreshed.
- NetBSD 4 device execution remains unverified: the LE device was unreachable
  through its configured London jump host, and the BE device was unreachable
  at its saved addresses. Their test drivers were compiled, not executed.

## Samba 4.25.0rc2 validation (2026-09-12)

Source: `samba-4.25.0rc2`, commit
`b5923e8d9563bcee569eb3f3ecc712a2a3761c1c`, plus the checked-in patch series.
Patch 0021 is removed because rc2 contains that AFP_AfpInfo fix. Embedded srvsvc,
durable-cookie handling and the durable test records follow rc2's updated APIs.

- A fresh Ubuntu 24.04 host build passed all 53 invocations with ASan/UBSan
  after declaring the stream test's `HASH_INODE` dependency explicitly. The
  initial Linux CI attempt failed at linking: shared-module builds do not
  inherit that dependency through `smbd_base` as the appliance build does.
- All 53 regression invocations passed on the LAN NetBSD 6 device (the existing
  40 AIO/durable/pthreadpool invocations plus 13 stream/charset invocations).
- Removing patch 0035 in the disposable VM source made `root_delete`,
  `short_read`, `shrink_missing`, `missing_path`, `invalid_stream`,
  `primary_error` and `extent_error` each fail with the test assertion exit 90.
  Restoring the patch made every stream/charset case pass again.
- The legacy-GCC charset fallback preserved const-qualified results and evaluated
  arguments once on the actual SDK/device combination.
- Deployed the stripped root-built NetBSD 6 binary with debug logging. Doctor
  reported Samba 4.25.0rc2 and no failures; disabled rsync was the expected skip.
- On macOS 26.6.2, the mounted-share script passed all 121 checks: eight trials
  of file/directory/rm-rf and metadata deletion at root and two nested depths,
  plus a 90 KB / 70 KB / 32-byte stream roundtrip and shrink. Every deletion
  succeeded on its first attempt. Scratch objects and the mount were removed.
- The device retained its original 16 MiB RAM disk, with approximately 4 MiB
  free after validation. No SDK/toolchain was rebuilt.
- Full local pytest: 1,853 passed with four workers. One telemetry timeout in
  the earlier `-n auto` run passed alone and in that full rerun. Subsequent
  focused checks passed 58 tests and 32 subtests, including the new six-case
  size-budget boundary test. Ruff and build-input preflight passed.

NetBSD 4 runtime testing is outside this validation: its artifacts and regression
executables are cross-built and checked for static linkage, but the manually
validated device is NetBSD 6. This does not claim macOS 27 or a full Time Machine
backup-cycle test.

All three release artifacts and static regression executables were rebuilt as
root with the normal NetBSD lane helpers. The stripped binaries passed the
static-ELF, no-pthread link-map and 10 MiB size checks, then were copied back and
the artifact hashes refreshed after the required delay:

| Device family | Stripped smbd bytes | Change from 4.24.3 |
| --- | ---: | ---: |
| NetBSD 6 | 10,195,664 | +35,768 |
| NetBSD 4 LE | 10,207,624 | +37,568 |
| NetBSD 4 BE | 10,206,464 | +37,964 |

Copied host build outputs and caches were removed from the VM repository,
including macOS `.build`/`dist`, virtualenvs and distribution `bin` copies.
Subsequent syncs transferred only build inputs and regression sources.

## Native HFS metadata bridge validation (2026-09-13)

Source: the same `samba-4.25.0rc2` commit and ordered patch series above, with
patch 0037 bridging FinderInfo and Finder tags from Apple firmware's private
descriptor-xattr syscalls. The configured `fruit:metadata=stream|netatalk`
store remains authoritative when both copies exist, and reads do not rewrite a
disagreement. Resource forks remain on `fruit:resource=file` and are not
bridged.

- A fresh Ubuntu 24.04 build passed 63 ASan/UBSan invocations: the existing 53
  cases, nine isolated native-metadata cases, and one combined `all` pass that
  checks their sequence and cleanup. The native cases cover the NetBSD 4/6
  syscall return difference, configured-store precedence, native fallback,
  mirror/delete ordering, unsupported non-HFS filesystems, concurrent shrink,
  listing bounds, disabled/ignore-user-xattr modes, FinderInfo size/error
  handling, symlink refusal and directory stream stats.
- The normal NetBSD 6 release gate passed 54 device invocations at the
  `dns-sd`-resolved `192.168.1.218`. The large native test ran once through
  `all` from the guarded mounted-HFS cross-exec path. The rebuilt payload was
  deployed, survived reboot, and passed doctor; disabled rsync was the expected
  skip.
- On macOS 26.6.2, fresh AFP and SMB mounts passed native-to-SMB fallback,
  SMB-to-native mirroring, configured-store conflict precedence, subsequent
  convergence and deletion for files and directories under both `stream` and
  `netatalk` modes. Direct device syscalls confirmed FinderInfo/UserTags were
  absent after deletion; an immediately reused AFP directory vnode retained a
  stale label until its client session was retired. All test objects and mounts
  were removed, and the deployed device was restored to `netatalk` mode.
- The NetBSD 4 LE device was rediscovered at `192.168.1.10`; its live shell
  marker was `\001`, independently confirming the little-endian lane despite
  stale backup filenames. All 54 device invocations passed from its mounted HFS
  volume. Its first payload upload and on-disk verification completed, but the
  reboot returned stock services without SSH. ACP SSH enablement restored port
  22, and a fresh deployment explicitly selected the little-endian payload;
  firmware autostart, managed-runtime activation and the complete doctor check
  then passed after reboot.
- Fresh AFP and SMB mounts against that final NetBSD 4 deployment passed native
  fallback, SMB-to-HFS mirroring, configured-store conflict precedence and
  deletion in `netatalk` mode for both a file and a directory. A fresh AFP
  session confirmed native FinderInfo and UserTags were absent after deletion.
  All local mounts and test objects, plus the two device diagnostic executables,
  were removed afterward.
- The NetBSD 4 BE lane and all static regression executables cross-built
  successfully. No reachable matching BE device was available for execution.

The stripped release artifacts passed their static-ELF, no-pthread link-map,
NetBSD-note and 10 MiB size checks, were copied back after the required delay,
and received these manifest hashes:

| Device family | Stripped smbd bytes | SHA-256 |
| --- | ---: | --- |
| NetBSD 6 | 10,201,560 | `10ac0ae04aa7f43c3c5c2dff42aeeffe716f666435926e393f9905951ee9218c` |
| NetBSD 4 LE | 10,213,656 | `7ec1840e63bb4bd2be07b481be5b84fdeb0fb4814216e39f38e19a870ccc2532` |
| NetBSD 4 BE | 10,212,492 | `ce2dd3df46e3c8795f606ed025c2d6ff48b4b3bada65370f0dd055c80f6ad19f` |

## Native HFS V4 migration validation plan

The V4 native-HFS backend and two-phase migrator supersede the 0037 dual-write
results above. Do not treat this section as a completed live-device validation
until every gate is checked and the actual migration output is recorded.

Offline gates, in order:

1. Apply the complete patch series to a fresh Samba checkout.
2. Build `smbd`, `tc_xattr_hfs_migrate`, `tc_native_metadata_test`, and
   `tc_xattr_migrate_test` for one NetBSD lane.
3. Run host ASan/UBSan regression cases, including combined `all` runs.
4. Run the Python deploy/planner/executor tests and the full local suite.
5. Inspect the migrator binary for static linkage and the resident `smbd` for
   the 10 MiB size budget and absence of pthread dependencies.
6. Repeat offline builds for the other two lanes and update artifact hashes.

The single live migration attempt must not begin until the offline gates pass
and a read-only inventory has captured the TDB path, byte size, record count,
mounted HFS roots, AppleDouble sidecar count, and representative metadata. The
live sequence is NetBSD 4 first and NetBSD 6 last because rebooting the NetBSD 6
router disconnects the NetBSD 4 device behind it.

For each live family:

1. Seed isolated files through SMB with FinderInfo, tags, another Apple xattr,
   a Windows-only ADS, ACL data, and resource forks at 0, 3,802, 3,803, 90 KiB,
   and 1 MiB. Seed an AppleDouble fixture containing embedded xattrs.
2. Record the legacy TDB and sidecar bytes before migration.
3. Run only the migrator `copy` phase and verify that all legacy data remains.
4. Verify native FinderInfo/xattrs directly and resource contents through
   `..namedfork/rsrc`, including hashes and exact lengths.
5. Install but do not activate the V4 payload, then run `cleanup`.
6. Confirm that verified sidecars and completed file records are removed,
   malformed/unsupported sidecars and unmatched records are retained, and the
   TDB is deleted only when empty.
7. Activate Samba and verify SMB reads of every value, then use a fresh AFP
   session to read the same FinderInfo, xattrs, and resource fork.
8. Write new values through AFP and read them through SMB, then reverse the
   direction. Exercise rename, truncate-to-zero, delete-on-close, and base-file
   deletion.
9. Reboot, run Doctor, repeat the cross-protocol reads, and remove all fixtures.

Failure at any step stops before cleanup or activation. Preserve the TDB,
sidecars, migrator output, and device logs for diagnosis; do not retry the live
migration until the failure is understood.

Offline validation of the review fixes on 2026-09-14:

- The final full local suite passed 1,884 tests with ten workers. The focused
  artifact/build/migration suite passed 51 tests and 39 subtests. Ruff,
  diff-whitespace, ELF linkage, size-budget, and manifest checks passed. Existing
  Python forkpty deprecation warnings remain.
- The complete patch series applied to a fresh Samba checkout and removed the
  superseded native-xattr header. A disposable Ubuntu 24.04 build then passed all
  73 real C regression invocations under ASan/UBSan; the Mac host itself lacked
  `pkg-config`/GnuTLS discovery, so no host packages were installed there.
- Native-xattr regression code verifies that set/remove syscalls execute while
  the cooperative file lock is held and that repeated `XATTR_CREATE` returns
  `EEXIST` without issuing a second native write.
- Resource tests distinguish readable conflicts from I/O failures, continue
  checking readability after differences, retry EINTR/partial reads, and retain
  sidecars on errors. Directory-read errors fail the scan; a 150-file fixture
  exercises geometric allocation growth in directory snapshots.
- Per-file TDB cleanup tests exercise failed commits, retained missing-disk
  records, later discovery of those files, and subsequent deploys after native
  edits/deletions. Completed records are retired independently.
- Boot shell tests cover successful and failed migration, cancellation with helper
  termination and RAM cleanup, partial migration with an unavailable volume,
  same-process scan suppression, later attachment, retry after failure, and
  preservation of already-active shares.
- All three NetBSD lanes rebuilt smbd, the migrator, and static regression
  drivers using the existing VM toolchains. Cross-execution and cross-answer
  generation were disabled; the cross-exec command was /usr/bin/false.
- All six root-built, stripped deliverables are static NetBSD ARM executables.
  Resident smbd remains below 10 MiB; the temporary migrator is approximately
  2 MiB. Binaries were copied back, followed by the required five-second wait
  and manifest update.

| Device family | smbd bytes | smbd SHA-256 | migrator bytes | migrator SHA-256 |
| --- | ---: | --- | ---: | --- |
| NetBSD 6 | 10,207,344 | `2f12424643461c42257a3f442177d44abdc7a6cc412d12792b64b193a135d1d0` | 2,126,852 | `fb0101af9be169de8c865dbac61ae36bf0bde3f10a19253ca332d3a7f0c554a6` |
| NetBSD 4 LE | 10,219,572 | `6e41fcc582dfed744c001d2e4d3c9326f6018be1e21cfc25d95cb1f686fedae8` | 2,133,420 | `b4fa7df4a7db140629583179493d9b21bf7d08162d33390f1c149a0156be3f0b` |
| NetBSD 4 BE | 10,218,420 | `ac2f276fd0051cdc8a019d390de192a432ba176b958cdc95d1ce5da0cfc93d1e` | 2,132,952 | `3eab680a8b1f46e80f9b44f86bf4e81238046164e2b26ae33855c12062fa575a` |

Real-device volume identity across unplug/replug and AFP concurrency remain
unvalidated. Unmatched device/inode records are retained rather than guessed.

This offline validation does not claim macOS 27 coverage or a full Time Machine
backup cycle. Live migration results are recorded separately below.

## Live migration validation (2026-09-14/15)

- NetBSD 4 local little-endian (`TimeCapsule6,116`, `192.168.1.10`) was backed
  up before deployment: `xattr.tdb` and `xattr.tdb.bak` were each 8,228,864
  bytes with SHA-256 `cef291e8095f87dc33eda68badb4e5cd2fa92cf9535b905b0987296d4e09b149`.
  The original TDB was retired by cleanup and the backup remained on HFS.
- The first NetBSD 4 reboot exposed NetBSD `sh` behavior for appending to an
  empty `"$@"` under `set -u`. That manager fix was redeployed; the subsequent
  boot passed managed-runtime checks, Doctor, SMB CRUD, SMB EA set/get, and
  SSH-side HFS verification. Temporary test files were removed.
- The migration wrapper was then deployed and cancellation-tested on NetBSD 4:
  TERM removed both the helper process and its RAM executable. A no-reboot
  activation passed all managed-runtime checks.
- NetBSD 6 (`TimeCapsule6,113`, `192.168.1.218`) was backed up before deployment:
  the 8,880,128-byte TDB and `.bak` matched at SHA-256
  `b6ac03bc79292b9275418c9deab2a6feea08396e4dc37523063e660e8e0f8a59`.
- NetBSD 6 deployed and verified the payload, retained 29 unmatched TDB
  records as migrator orphans, and preserved the `.bak`. After manager restart,
  the runtime reached stable `ok` passes. Doctor passed all checks, SMB CRUD and
  EA set/get passed, SSH confirmed `/dev/dk2` mounted as HFS, and temporary test
  files were removed.
- A subsequent NetBSD 6 backup-restore/deploy retry was monitored without any
  manual manager start. The final wrapper-based copy and cleanup both completed
  with `status=0`; the runtime started automatically, Doctor passed all checks,
  and SMB CRUD passed. Wrapper cancellation also removed the helper and RAM
  executable. The `.bak` remains intact; the 29 unmatched records remain
  intentionally retained in the active TDB as migrator orphans.
- The London NetBSD 4 LE target was intentionally skipped. No other live device
  was deployed or migrated.

## Native metadata race-fix validation (2026-09-13)

- Patch 0037 now retains configured AFPInfo authority across AAPL empty-stream
  filtering, treats a native FinderInfo attribute disappearing between its size
  and value reads as absent, and suppresses a synthetic Finder-tag name when the
  name appears in the TDB list returned by the same call.
- The production-code regression fixture directly covers native-only open/read
  and primary materialization, write and unlink failure ordering in both metadata
  modes, AAPL compressed directory FinderInfo, zero-length configured AFPInfo,
  second-read deletion, and concurrent TDB creation during listing.
- The NetBSD 6 helper passed all 54 cross-executed device invocations. The large
  native fixture ran once through `all` from the existing mounted HFS payload
  directory and left no temporary executable behind.
- The final stripped NetBSD 6 `smbd` is 10,201,840 bytes with SHA-256
  `c2f557f9b9e5039036699685ac00db2966e58fcf4f27219b634d2c71eff34132`.
  That exact artifact was deployed to the NetBSD 6 little-endian device at its
  rediscovered address, and the complete doctor check passed after reboot.
- `make test-parallel` passed all 1,859 local tests with ten workers. The focused
  build/runtime suite separately passed 168 tests and 76 subtests.

## Deploy-only migration and TDB precedence (2026-09-19)

Patch 0040 makes the selected legacy TDB representation authoritative during
copy and requires matching native readback during cleanup. The TDB schema stores
attribute names and values, without a per-attribute modification timestamp.
Native-only values and AppleDouble/native resource-fork conflict behavior are
unchanged. Missing directory and record-retirement diagnostics now include the
path and error.

- Rebuilt the migrator as root in the existing NetBSD VM for all three lanes.
  The VM source trees contained additional migration work from a different
  checkout. Isolated temporary targets used the source reconstructed from this
  branch's patches and the existing configured static link settings; the VM's
  original sources and build definitions were preserved. No toolchain rebuild.
- The migration fixture's combined `all` case passed on the NetBSD 6 device.
  It covers conflicting TDB/native values, malformed native FinderInfo repair,
  cleanup refusal after native divergence, copy retry, row retirement, detached
  records, resource forks, directory-read failures, and orphan quarantine.
  The fixture mocks the private HFS syscalls; this does not claim a migration
  of the user's production data. Temporary device executables were removed.
- An obsolete fixture call passed a null TDB to the formerly native-first
  FinderInfo helper and crashed during the initial combined run. It was replaced
  with a real-TDB case demonstrating repair of malformed native FinderInfo.
  The combined device run then passed; isolated cases also passed.
- NetBSD 4 LE and BE artifacts were cross-built and checked for static linkage,
  byte order and NetBSD notes. Their migration fixtures were not device-run.
- Full local pytest passed 2,113 tests and 217 subtests. After correcting saved
  metadata-option selection, 632 deploy/CLI/API tests and 64 subtests passed.
- Migration runs only during deploy. Boot/hotplug migration and checkpoint
  writers were removed. At the time of this validation each deploy phase allowed
  six hours and retained its diagnostic output under the payload's logs directory.

| Lane | Stripped migrator bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 2,128,772 |
| NetBSD 4 LE | 2,135,460 |
| NetBSD 4 BE | 2,135,012 |

### 2026-09-21 migration inactivity guard

- The complete patch series applied to a clean Samba 4.25.0rc2 tree.
- NetBSD 6 built the production migrator and regression fixture incrementally;
  the isolated `guard` case and the combined `all` fixture passed on-device.
- NetBSD 4 LE built both targets and passed the `guard` case on-device. NetBSD 4
  BE built and linked both static targets in the existing configured lane.
- A direct NetBSD 6 invocation returned valid guarded inspection JSON, and the
  firmware SSH path preserved remote exit status 75.
- After the review fixes, the NetBSD 6 `multi` fixture passed on-device; the
  NetBSD 4 LE and BE migrators were rebuilt compile-only as requested.
- The focused build/artifact/migration suite passed 267 tests and 95 subtests;
  `make test-parallel` passed all 2,227 local tests.

| Lane | Stripped guarded migrator bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 2,158,388 |
| NetBSD 4 LE | 2,164,532 |
| NetBSD 4 BE | 2,164,120 |

## Appliance installer validation (2026-09-19)

The combined migration fixture also passed on NetBSD 4 LE. For that device, the
fixture's temporary-path prefix was moved from `/tmp` to an isolated hidden HDD
prefix under `/Volumes/dk2`; the root ramdisk is too small for the TDB and resource
cases. Test files and databases were separate from production backups, and the
fixture executable was removed afterward. As on NetBSD 6, private HFS syscalls
were mocked while the fixture used real files and TDB transactions.

The fixture was compiled in the existing configured NetBSD 4 LE lane with the
branch's migrator source, leaving the VM's original sources and build definitions
intact. Its companion stripped migrator matched the bundled artifact exactly:
2,135,460 bytes, SHA-256
`523673e8e2dadfe573f60b989817142c33f2215c90c0a700db4f4cc5d20bec10`.
No production native code or toolchain changed for the installer work.

## Binding revocation, export-root reload and uninstall exclusion (2026-09-21)

Validated the combined working tree, including the preserved diagnostic and
native-test compiler changes from the concurrent task.

- Host manager regressions cover removal, addition, mixed changes, reordered
  bindings, failed observations, never-applied additions, blocked/failed storage,
  full process-group draining and a plan reverting during shutdown. Root-option
  changes publish the new identity without restarting Samba.
- Focused manager/configuration/uninstall/runner suite: 92 passed. Full
  `make test-parallel`: 2,271 passed and one telemetry replay case exceeded its
  10-second timeout; that case passed alone. Final artifact, regression-runner
  and uninstall checks: 35 passed. Ruff and `git diff --check` passed.
- Uninstall tests execute the real migration-idle shell guard through the
  executor/application flow and verify that busy/failed process inspection
  prevents payload removal and both reboot modes. Four guard cases also passed
  in each appliance's native `/bin/sh`, using controlled process observations.
- All 12 production-code storage-reload cases passed on NetBSD 6 and NetBSD 4 LE.
  They include same-UUID root narrowing/widening, a retained renamed share,
  no real open descriptor, failed reload, unchanged root and pending AIO.
- NetBSD 4 required the test executable in RAM, with scratch data on HFS.
  The same executable aborted in talloc before its first configuration load
  when run from HFS, and passed every case from RAM. Temporary trace statements
  were removed and the clean driver rebuilt for all lanes; the clean drivers
  passed on both devices. Production Samba already runs from RAM. Test cores
  and scratch files were removed; core dumps were disabled for the RAM run.
- Both LAN appliances passed deployment, real SMB client tests and full Doctor.
  The client tests preserve an unchanged tree while narrowing, widening or
  renaming/re-rooting another export, reject operations through the retired
  tree, and verify new writes reach the new root without restarting the parent.
  Durable reconnect, duplicate boot, owned-process recovery and repeated native
  NBNS recovery also passed; Apple's daemons survived the supervision tests.
- Binding removal while storage is held was exercised by the host's actual
  manager with controlled Apple observations and real process groups. No
  AirPort network settings were changed for hardware validation.
- A concurrent deployment interrupted the first NetBSD 6 installation.
  After coordinating exclusive ownership, standard redeployment restored the
  combined runtime; Apple settings and SSH keys remained intact. Final device
  checks were performed after recovery.

All builds used the existing SDKs and audited configured Samba trees, as root.
The BE cache lacked `tc_storage_reload_test` in `NONSHARED_BINARIES`; aligning
that entry with the normal wrapper resolved its attempted shared-library link.
No toolchain was rebuilt. The stripped release binaries were copied back and
manifest hashes updated after the required delay.

| Lane | service bytes | smbd bytes | Hardware validation |
| --- | ---: | ---: | --- |
| NetBSD 6 (NetBSD 7 SDK) | 363,324 | 10,208,744 | Passed |
| NetBSD 4 LE | 321,920 | 10,220,968 | Passed |
| NetBSD 4 BE | 321,320 | 10,219,812 | Build/ELF validation only |

Every release image is static ARM ELF with the expected endianness; all
artifact hashes match `artifact-manifest.json`. BE hardware was not tested.

## Native symlinks, patch 0045 (2026-09-24)

Patch 0045 keeps symlinks native on disk. It serves them to SMB clients as
reparse points, and when a newly created XSym file or symlink reparse
placeholder is closed, it replaces it with a native link. See `DETAIL.md`
"Symbolic Links".

- `tc_native_links_test` passed all 15 cases:
  - on NetBSD 6, on the mounted HFS volume;
  - on NetBSD 4 LE, run from `/mnt/Memory` with its working directory on HFS.
    Run from the disk, the same image aborted in talloc, as documented above.

  The cases include other clients replacing or recreating the name between the
  conversion's steps, rollback on every failure, metadata carried over without
  Finder info or resource forks, and the counted journal commits.
- `tests.samba.links_device --afp` passed 73/73 on both LAN devices after
  `tcapsule deploy`, three runs each:
  - Links named with `: * ? " < > |` work from macOS.
  - Windows, NFS and WSL symlink payloads become native links.
  - FIFOs, sockets, `mklink /D` and Windows-only targets are refused, and
    nothing is left behind.

  `manual_delete` passed 121/121 on both devices, and Doctor passed on both.
- The device suite found four bugs that the unit cases did not:
  - catia had no `readlinkat`/`symlinkat` hooks;
  - a NULL-size `FLISTXATTR` segfaulted smbd inside `vfs_acl_xattr`;
  - macOS sends a zero-length write at offset 1067 right after creating a
    link, which was refused;
  - the test was missing from wafsamba's static allowlist.
- HFS journal panic, `jnl: start_tr: active_tr is NULL`:
  - Without a workaround, completed conversions left the device ready to panic.
    The firmware records the panic in `/mnt/Flash/dmesg.panic`.
  - A repeated cycle (smbd restart, then the device suite) panicked NetBSD 6
    within one to three cycles.
  - Bisecting with runtime switches showed that a completed conversion is
    required. Pre-0045 smbd, xattrs only, and conversions that roll back gave no
    panic in 4–6 cycles each. Synthetic syscall loops never reproduced it.
  - Journal replay after one of these crashes overwrote the first 4 KiB of a
    newly written smbd with an old symlink block.
  - With the original unlinked only after `fd_close` and a `sync()` after each
    conversion, 16 consecutive cycles passed (6 with the on-disk unit test)
    without a panic. The same cycle is the release check for this code path.
- NetBSD 4 BE was build and ELF validated only; the UK device was offline.

| Lane | smbd bytes | Hardware validation |
| --- | ---: | --- |
| NetBSD 6 (NetBSD 7 SDK) | 10,230,852 | Passed |
| NetBSD 4 LE | 10,243,348 | Passed |
| NetBSD 4 BE | 10,242,216 | Build/ELF validation only |

All three lanes were built from sources that matched the exported patch, with
the existing SDKs; no toolchain was rebuilt. The stripped binaries were copied
back after the required delay, and `artifact-manifest.json` was updated.

Review follow-up (2026-09-24):
- The rollback also commits the journal before it removes the link it made.
- The generated `smb.conf` vetoes `.tc-xsym.*`, with `delete veto files = yes`.
- The device suite no longer waits for smbd sessions.
- Changes to unit tests:
  - `tc_native_links_test` counts the `sync()` calls on every outcome.
  - New `tc_catia_links_test` covers the catia link hooks. It is a separate
    binary because including `vfs_catia.c` made the combined test large enough
    that four unrelated cases aborted in talloc when run from the HFS disk. They
    passed from RAM, and the cause was not found.
- Results:
  - NetBSD 6: both tests pass from disk; the crash loop passed 6/6 cycles.
  - NetBSD 4 LE: both tests pass from RAM.
  - Both devices, after `tcapsule deploy`: links suite 73/73, manual_delete
    121/121, Doctor passed. A leftover `.tc-xsym.*` is invisible over SMB, and
    its folder deletes.

| Lane | service bytes | smbd bytes |
| --- | ---: | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 362,348 | 10,230,860 |
| NetBSD 4 LE | 321,648 | 10,243,352 |
| NetBSD 4 BE | 321,048 | 10,242,220 |

Rollback review follow-up (2026-09-26):
- The metadata rollback commits the journal before it checks the new link, so
  no `sync()` sits between that check and the unlink it guards. If another
  client has replaced or removed the link by then, its change stands and the
  original is dropped, as when that happens before the check.
- New `rollback_races` case: other clients replace, remove or relink the name
  during the rollback's `sync()`, make the link uncheckable, or take the name
  the undo freed or the failed symlink left empty. Built against the old
  source, it failed on the NetBSD 6 device (the other client's file was
  unlinked); with the fix, `tc_native_links_test all` passed on NetBSD 6 and on
  NetBSD 4 LE, both from the HFS disk. The window between
  `tc_restore_aside()`'s check and its rename remains an accepted race.
- NetBSD 6: the lane regression run passed all 63 driver runs on the device.
  After `tcapsule deploy`, Doctor passed and the links suite passed 87/87 four
  times in a row with no new `/mnt/Flash/dmesg.panic`.
- NetBSD 4 LE: after `tcapsule deploy`, Doctor passed and the links suite
  passed 87/87 three times in a row; no `/mnt/Flash/dmesg.panic` exists.
- NetBSD 4 BE: build and ELF validation only.
- The xattr migrators rebuilt byte-identical on all three lanes.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,224,920 |
| NetBSD 4 LE | 10,246,580 |
| NetBSD 4 BE | 10,245,468 |

Second review follow-up (2026-09-24), smbd only:
- Finder info and resource forks are left behind only under their exact stored
  names: native, netatalk, and streams_xattr under its configured prefix. A
  substring match used to drop client streams such as
  `backup.AFP_AfpInfo.notes`.
- When the creating handle is write-only (Linux `mfsymlinks` asks only for
  `GENERIC_WRITE`), the XSym body is read through an internal handle. That
  handle must be the same file, and the client's access is not widened.
- Tests: new unit case `convert_write_only`, near-miss names in `metadata`, and
  two device checks (a write-only XSym file; a `backup.AFP_AfpInfo.notes`
  stream). Both device checks fail against the previous smbd.
- Results:
  - NetBSD 6: unit test passes from disk; links suite 74/74 three times; crash
    loop 6/6; manual_delete 121/121; Doctor passed.
  - NetBSD 4 LE: unit test passes from RAM; links suite 74/74 three times;
    manual_delete 121/121; Doctor passed.
  - pytest: 2270 passed.
- Issue #304 review: the device suite gained its link shapes (links to `.`,
  `..`, nothing, a directory and outside the tree; the `repair-xattrs` walk;
  renames of each shape; `rm -rf`). It passed 87/87 on both LAN devices.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,231,584 |
| NetBSD 4 LE | 10,244,304 |
| NetBSD 4 BE | 10,243,176 |

## No-pthread workaround review (2026-09-26)

Did 0046 (static `.data` fault-ahead) make the older no-pthread workarounds
unnecessary? Each patch was reverse-applied in the configured lane tree and
only smbd rebuilt. The variant then ran from the device's RAM path (a symlink
to the data disk, so a reboot restores the deployed smbd) under a workload:
150 connections, cross-connection lease breaks, change notify, and killing the
serving child mid-open. A pass means no abort, panic, core or talloc error and
every check as on the unmodified build.

| Removed | NetBSD 6 | NetBSD 4 LE | Outcome |
| --- | --- | --- | --- |
| 0044 | pass | pass | Dropped: its device-only failure was the fault-ahead bug 0046 fixes. |
| 0009 | pass | pass | Dropped. |
| 0012 | aborts at startup | pass | Dropped: only the call-depth hooks mattered (TLS, below). |
| 0010 | aborts at startup | pass | Dropped (TLS, below). |
| 0008 | pass | pass | Kept, for one process: forked notifyd and cleanupd add two ~2.5 MB RSS processes and 14 KB of smbd. Its notifyd-parent hunk, a TLS workaround, is dropped. |
| 0046 (control) | never opens its listeners | not run | The fault-ahead bug still breaks smbd, so "remove X and 0046" controls cannot isolate X. |

TLS. The NetBSD 6 aborts printed no reason and left no core. An `abort()`
override in the variant printed its caller: `__tls_get_addr`. Samba builds its
objects `-fPIC`, so every `__thread` access calls `__tls_get_addr`, and static
libc's version only aborts. Configure enabled `__thread` on the NetBSD 6 lane
only; the NetBSD 4 compilers lack it, which is why both variants pass there.
0012's call-depth hooks avoided tevent's `__thread` state in
`tevent_req_create`, and 0010 avoided libwbclient's `__thread` client name. The
one other TLS variable, `config_include_depth`, would have aborted on the first
smb.conf `include`. 0013 now leaves `HAVE___THREAD` unset without pthreads, so
`replace.h` defines `__thread` away, and the build refuses `HAVE___THREAD` and
any smbd or migrator with a `.tdata` or `.tbss` section.

Two observations:
- On every build, including the unmodified one, a Mac handle held across the
  kill of its smbd child returns EIO instead of reconnecting. That is by
  design: a durable (not persistent) handle survives a lost connection, not
  the server process. smbXsrv_open_global_verify_record() refuses an open
  whose smbd is gone ("did not clean up record"), as Windows does. Lost
  connections do reconnect on both LAN devices (2026-09-26, smbprotocol with a
  lease and DH2Q, then DH2C on a new connection): after a FIN or an RST in
  0.1 s; with the old connection left half-open, after about 5.5 s the
  answer is FILE_NOT_AVAILABLE (0024's retry window), unless the new session
  names the old one (PreviousSessionId), which reconnects in 0.1 s. macOS does
  that: with its mount's smbd stopped for 60 s, the Mac opened a new session,
  the old smbd got MSG_SMBXSRV_SESSION_CLOSE when resumed and marked the open
  disconnected, and the Mac's DH2C reconnect restored the handle with its
  pending write and all data. `durable_device.py` repeats these checks. In
  about 25 NetBSD 6 runs, the macOS case failed once: the Mac's DH2C reached
  the still-attached open and got FILE_NOT_AVAILABLE after 5.1 s, so the held
  handle returned EIO. In 23 later runs with PreviousSessionId logged, the Mac
  always named the old session and reconnected. The cause of that one failure
  is not known.
- One NetBSD 4 run without 0008 saw a Mac write time out during the notify
  step. Two reruns and the build without all five patches passed.

The candidate series (0013 fix; 0009, 0010, 0012, 0044 dropped) passed the
workload and the links suite (85/85) on NetBSD 6 before landing.

Follow-up, 0011 and 0022 (2026-09-26). Same method, both devices:
- Without 0011 nothing changed. Upstream already opens the SMBX version
  database with a NULL (process-lifetime) parent. Dropped.
- Without 0022 nothing crashed, but smbd logged 471 "Dangling frame" lines at
  level 0, about three per exiting connection child. Upstream's no-pthread
  talloc stack reports every open frame from an atexit handler; pthread builds
  never run that report at exit(). 0022 is now that one change (free the
  tracker quietly), which also makes 0006's early `-V` handling unnecessary, so
  0006 was dropped too. The AIO child's inherited frames are upstream
  behaviour; the new `exit_frames` case checks that a child exiting with open
  frames prints nothing, in place of `fork_stack`.
- A clean rebuild also showed that `waf distclean` in `_samba4x.sh` had not
  been clearing the lane tree, so the committed NetBSD 6 smbd carried a stale
  configure result (`HAVE__STATIC_ASSERT` missing from `smbd -b`). The
  binaries below come from lane trees with `bin/` removed first.

## Series restructuring (2026-09-26)

Every patch is now generated from a replay repository (pristine rc2, the
overlay, then one commit per patch) and named by the Samba component it
changes. Commits that only reformatted, renamed, regrouped or split patches
left the fully patched tree byte-identical; that was checked after each one
with patch_apply_series. The ones that changed source:

- 0024 reuses upstream's reconnect checks for a live open instead of a copy.
- 0023 calls its HFS owner fix under an `#ifdef` instead of a no-op stub.
- 0013 guards only the first pthread probe instead of re-indenting them all;
  configure produced the same results (config.h and the waf cache differ
  only by an `#undef HAVE___THREAD` comment and the regression target list).
- aio_fork: 0052 (errno fix, upstream bug), 0053 (one request path, refactor
  only) and 0031 (bounded helpers). 0031 now queues every request and lets
  one scheduler dispatch the FIFO, and idle helpers are retired after
  upstream's 30 seconds again: the 3-hour interval avoided repeated forks
  during the allocator corruption of issue #295, which was the fault-ahead
  bug that 0046 fixes.
- 0038: the native xattr_tdb helpers and their four copies of the name
  dispatch moved into overlay tc_xattr_tdb_native.c, which also holds the
  link-aware wrappers that 0045 used to add; 0038 is 67 lines instead of 363.
- fruit (0055 FinderInfo, 0056 resource fork): helpers and the larger native
  branches moved into overlay tc_fruit_native_finderinfo.c and
  tc_fruit_native_rsrc.c; the upstream diff went from 659 to 131 lines. Dead
  code and changes to non-native paths were removed. Two intentional changes:
  removing FinderInfo during a nested pathref open now reports it absent, as
  reading it already did, and an all-zero FinderInfo stats as ENOENT.
- 0059: lchmod/lchflags branches that no configure check could enable became
  ENOTSUP, and catia's xattr wrappers run only for link handles without an fd.
- `_samba4x.sh` removes the lane's build tree before configure: `waf
  distclean` never removed anything, so earlier lane builds reused stale
  objects and configure results.

Results: host regression run with sanitizers, 113 cases passed; NetBSD 6
regression drivers on the device, 62 passed; all three lanes rebuilt from
empty build trees (NetBSD 6 smbd reproduced its earlier test build byte for
byte). After `tcapsule deploy` on both LAN devices: Doctor passed, the links
suite with `--afp` passed 87/87, a 150-connection/lease/notify workload
passed, and FinderInfo, resource-fork and xattr visibility between SMB and AFP
matched the previous build exactly.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,227,864 |
| NetBSD 4 LE | 10,250,044 |
| NetBSD 4 BE | 10,248,928 |


## Native resource fork create and delete (2026-09-26)

Two fixes to patch 0056 and its overlay fragment `tc_fruit_native_rsrc.c`,
both found with macOS 26 `xattr` over SMB on native HFS shares:

- Creating a fork failed with "Attribute not found". HFS gives every file a
  resource fork, empty or not, so `fd_open_atomic()` sent its O_CREAT|O_EXCL
  create to a fork that already existed. HFS answered EEXIST, the retry
  without O_CREAT found the fork empty, and the create ended as
  OBJECT_NAME_NOT_FOUND. Now an empty fork opens without O_EXCL, and an
  exclusive create of a fork with data still fails with EEXIST.
- Deleting the AFP_Resource stream left the fork in place. The Mac deletes it
  with delete-on-close, and the native branch ignored that. Now the delete
  truncates `<file>/..namedfork/rsrc` to zero, and removing the file still
  removes its fork without the extra step. Apple's AFP server empties the
  fork the same way: after this fix, a fork deleted over AFP disappears over
  SMB too.

`xattr -w` of a shorter fork over SMB keeps the old tail. The Mac opens the
stream with OPEN_IF and writes at offset 0 without setting EOF, so that is the
client's behaviour on any server.

Results: in the host regression run with sanitizers, 113 cases passed.
`resource_backend` now covers the empty, present and unprobeable fork
exclusive creates, the plain create, the delete from the share root and from a
subdirectory, a failed truncate, and file removal. A NetBSD 4 LE test build
was byte-identical to the shipped one. After `tcapsule deploy` on both LAN
devices, Doctor passed and the links suite with `--afp` passed 87/87. On
NetBSD 6, `durable_device.py` passed 13/13. From a macOS SMB mount on both
devices, these all worked: create, read, rewrite, delete (the fork is empty
on the device), create again after delete, `cp` carrying the fork, and
removing files that have forks. A fork written over SMB read back over AFP.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,228,312 |
| NetBSD 4 LE | 10,250,528 |
| NetBSD 4 BE | 10,249,412 |

## aio_fork buffer size and helper count (2026-09-26)

aio_fork stays off by default. When it is enabled, each helper's shared
buffer is now 8 MiB (`AIO_FORK_BUFFER_SIZE` in patch 0031, was 128 KiB), so
Samba's default 8 MiB SMB2 reads and writes pass through it, and each share
gets `aio_fork:max_children = 2` (was 8).

Upstream sizes the buffer once when it forks a helper. Every byte passes
through it: smbd copies write data in, and the helper reads into it. The
`n > 128*1024` check is the only bound on those copies, so the old config
had to cap `smb2 max read/write` at 131072. Raising the check alone would
overrun the buffer.

Benchmark: a Mac on Wi-Fi 6 wrote and read each file set over a fresh SMB
mount. Every write was fsynced, the share was remounted before reading, and
every file was hash-checked. MB/s write / read, debug logging off, averaged
over two runs unless noted.

| NetBSD 6 | aio off | 128 KiB, 8 helpers | 8 MiB, 4 helpers |
| --- | --- | --- | --- |
| 100 x 1 MB | 4.2 / 8.4 | 3.9 / 8.2 | 4.3 / 8.5 |
| 50 x 10 MB | 12.2 / 14.0 | 9.6 / 12.8 | 11.4 / 12.5 |
| 4 x 500 MB | 17.0 / 15.0 | 13.9 / 14.7 | 15.1 / 13.6 |

| NetBSD 4 LE | aio off | 128 KiB, 8 helpers (one run, stopped) | 8 MiB, 4 helpers (one run) |
| --- | --- | --- | --- |
| 100 x 1 MB | 3.0 / 4.2 | 2.8 / 3.7 | 2.9 / 4.1 |
| 50 x 10 MB | 5.6 / 3.0-4.8 | 5.1 / 4.0 | 5.3 / 4.6 |
| 2 x 500 MB | 6.2 / 5.0 | - | 5.8 / 4.2 |

- The 128 KiB cap caused most of aio_fork's write loss. With 8 MiB buffers,
  aio_fork is still about 10% slower on large files for one Mac copying one
  file at a time. It was never faster, so it stays off.
- A single Mac never used more than two helpers: smbd peaked at 3-4
  processes and about 18 MB of RSS on both families. With 8 helpers it
  peaked at 10 processes, and 2 helpers were no slower.
- NetBSD 4 LE (256 MB RAM, no swap) was not OOM-killed with 8 MiB buffers
  and 4 helpers. Free memory fell to about 5 MB during large transfers and
  was back to 174 MB afterwards. smbd stayed near 17 MB, so the drop is most
  likely the kernel's file cache.
- Debug logging (log level 10) made 1 MB files about three times slower
  (1.3 / 3.9 MB/s on NetBSD 6).

Host regression run with sanitizers: 115 cases passed, including new
`full_buffer` and `over_buffer` cases. An 8 MiB request and one just over
128 KiB pass through a real helper byte for byte, and one byte over 8 MiB is
refused before any helper starts. All three lanes were rebuilt; the NetBSD 6
and NetBSD 4 LE smbd were byte-identical to the benchmarked builds.

After `tcapsule deploy` on both LAN devices (aio_fork off), Doctor passed.
Then aio_fork was turned on (2 helpers, default SMB2 sizes):
- The links suite with `--afp` passed 87/87 on both devices.
- NetBSD 6 ran the whole benchmark once: 4.3 / 9.0, 10.2 / 13.0 and
  15.2 / 13.5 MB/s. NetBSD 4 LE ran the 10 MB set: 5.0 / 4.6 MB/s.
- Every hash matched, smbd peaked at 4 processes, and dmesg showed no OOM
  kills.
- Both devices were then set back to aio_fork off.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,228,304 |
| NetBSD 4 LE | 10,250,528 |
| NetBSD 4 BE | 10,249,412 |

## Durable reconnect diagnostics, patch 0061 (2026-09-26)

Patch 0061 logs each step of a durable reconnect at level 3, prefixed
`tc_reconnect:`. Deploy with debug logging and grep `log.smbd` for it. The
steps are:
- negotiate, with its client GUID;
- session setup, with the previous session it names;
- why `close_previous` asked the old owner to close that session, or left it
  alone (gone, another user, not authenticated);
- the old smbd receiving the close and finishing it;
- each durable open it disconnects;
- the DH2C request and whether it was restored;
- why each connection ended.

It is for the rare Mac reconnect failure recorded above: one stall run in
about 25 got FILE_NOT_AVAILABLE and EIO. The level-3 lines stay out of normal
logs because the default log level is 0. The host regression run was
unchanged; all three lanes built without warnings in the changed files.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,232,608 |
| NetBSD 4 LE | 10,254,592 |
| NetBSD 4 BE | 10,253,480 |

## Disk rebinding helpers moved to an overlay, patch 0041 (2026-09-27)

Patch 0041's connection-binding helpers (`conn_record_bindings`,
`tc_revoked_descriptor`, `tc_stale_disk_tree`, `conn_refresh_bindings`) moved
unchanged into `overlay/source3/smbd/tc_disk_bindings.c`, which `conn_idle.c`
includes where upstream's comment for `conn_force_tdis` begins. The patch went
from 204 to 112 lines. Each smbd grew 16 bytes: talloc's `__location__`
strings now name the overlay file. The migrators rebuilt byte-identical, and
two NetBSD 4 LE builds of the new series gave the same smbd.

- Host regression run (Docker, sanitizers): all 115 cases passed, including
  `tc_storage_reload_test`, which includes `conn_idle.c` and so the overlay.
- NetBSD 4 LE device: deploy and `doctor` passed. `tc_storage_reload_test all`
  passed from `/mnt/Memory` with its working directory on `/Volumes/dk2`. With
  an smbclient session open, SIGHUP to the smbd parent reached the session's
  worker (`smbd_conf_updated` in its log) and the tree stayed connected: the
  next put, list and get worked.
- NetBSD 6 and NetBSD 4 BE were built, not deployed.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,232,624 |
| NetBSD 4 LE | 10,254,608 |
| NetBSD 4 BE | 10,253,496 |

## smbd without helper processes, patches 0005 and 0008 (2026-09-27)

Patch 0008 (renamed from `0008-smbd-single-process-helpers.patch` to
`0008-smbd-helpers-in-parent.patch`) was gated on `#ifndef HAVE_PTHREAD`, a
leftover from when forked helpers aborted without pthread. The 2026-09-26
review above showed they work, and 0008 is kept only to save memory, so it is
now gated on `TC_SAMBA4X_APPLIANCE`. It moved out of the no-pthread section
into a new "smbd without helper processes" section with 0005, the in-smbd
srvsvc pipe. Both patches' comments and 0005's overlay comment were corrected:
smbd is not one process (the parent, one child per client and aio_fork's
helpers remain), and the helpers stay off the device for memory, not because
of the RAM disk. 0008's log lines no longer say "no-pthread". 0041 changed
only in a hunk offset.

Every lane defines `TC_SAMBA4X_APPLIANCE` and none defines `HAVE_PTHREAD`, so
the shipped smbd behaves as before. Host regression builds define neither, so
their smbd now compiles upstream's forked helpers; only the lanes compile
0008's code.

- Each lane tree's `server.c` and `scavenger.c` carry the new gate. Each
  stripped smbd has the three new log strings, none with "no-pthread", and no
  `smbd-notifyd`, `smbd-cleanupd` or `smbd-scavenger` title, as before. Each
  smbd is 56 bytes smaller (shorter strings). The migrators rebuilt
  byte-identical.
- Host regression run (Docker, sanitizers): all 115 cases passed.
- NetBSD 4 LE device, running this smbd (same SHA256) from a deploy for other
  service work, with log level 10:
  - Since smbd's last start its log has "Running notifyd in the smbd parent",
    "Running cleanupd in the smbd parent" and "Skipping the periodic messaging
    dgm cleanup" once each, and no "Started cleanupd pid", "forwarding message
    to scavenger" or "no-pthread" line. All 38 connections since then logged
    `notify_init: notifyd=<parent pid>`.
  - `ps` shows no smbd helper, idle or after sessions: only the parent and one
    child per open connection.
  - `smbclient -L` and macOS `smbutil view` list `Data` and `IPC$` (0005's
    srvsvc).
  - Change notify: a watcher on one connection saw a file created from
    another.
  - cleanupd: a connection's child had a `msg.lock` file and a `msg.sock`
    socket while open, and both were gone after it exited. After ten more
    sessions the parent logged "cleaned up pid" for each and left no entry for
    an exited child.
  - Scavenger: after a durable handle's connection was reset, the parent (not
    a forked scavenger) got the child's message and scheduled the timer. It ran
    60 s later ("do cleanup for file", then share_mode_cleanup_disconnected),
    and a reconnect was then refused with OBJECT_NAME_NOT_FOUND.
  - `durable_device` passed 13/13 and `doctor` passed.
- The NetBSD 6 device was not tested; it was running a backup.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,232,568 |
| NetBSD 4 LE | 10,254,552 |
| NetBSD 4 BE | 10,253,440 |

## Shutdown closes from the share root (0062), durable post-close stat (0029), posix_spawn on NetBSD 6 (0015) (2026-09-27)

A shutdown close (a dropped connection, a bare SMB2 LOGOFF, a tree disconnect)
is not a request on the file's tree, so smbd does not change into its share
first. `close_cnum()` for any tree ends with `chdir("/")`, and a LOGOFF request
has no tree, so the close ran from "/" (or another share's root). Two things
resolve the file by name from there:

- 0029 stats the closed file by name for the durable cookie. The stat failed
  (OBJECT_NAME_NOT_FOUND), close_durable logged "Failed to disconnect durable
  handle ... proceeding with normal close", and the client's reconnect was
  refused. Upstream fstats before closing and has no such failure.
- Upstream's delete on close (files and directories) opens the parent with
  `parent_pathref(conn->cwd_fsp, ...)`. The file survived. With a decoy at the
  same relative path under the device's "/", the decoy also survived: the
  wide-links check refuses a parent outside the share, so this is a missed
  delete, not a wrong one.

0062 changes into the file's own share root at the start of every shutdown
close of a real file or directory (`vfs_ChDir_shareroot`, cached, so free when
smbd is already there). 0060's native-link conversion is not affected: it runs
only on a client CLOSE (NORMAL_CLOSE), after smbd changed into that tree.

Reproduced with the old smbd from a Mac with smbprotocol (4LE: NetBSD 4 LE
device; 6: NetBSD 6 device):

| Case | Devices | Old smbd |
| --- | --- | --- |
| durable open, RST | 4LE, 6 | reconnects |
| IPC$ tree connected last, never disconnected, then RST | 4LE | reconnects |
| IPC$ connected and disconnected, then RST | 4LE, 6 | reconnect refused |
| another tree on the share disconnected, then RST | 4LE | reconnect refused |
| durable open in the second session on the connection, RST | 4LE, 6 | reconnect refused (order dependent) |
| delete-on-close file, RST or bare LOGOFF | 6 | deleted |
| delete-on-close file, IPC$ connected and disconnected, RST or bare LOGOFF | 6 | not deleted |

The new `durable_device` cases (`rst+ipc-tdis`, `rst+second-session`,
`doc:drop`, `doc:drop+ipc-tdis`, `doc:logoff`, `doc:logoff+ipc-tdis`) failed
exactly the four bug cases on the NetBSD 6 device's old smbd, and passed every
control. smbprotocol's default `Session.disconnect()` closes every open and
tree before its LOGOFF, which hides the bug; the suite sends a bare LOGOFF.

0015: Samba's configure never defines `HAVE_POSIX_SPAWN`, so every lane used
the fork/exec fallback. A static probe built by the NetBSD 7 SDK ran on the
NetBSD 6 device (Apple kernel `NetBSD 6.0`, AirPortFW-79100.2): `posix_spawn`
started `/bin/echo`, and for a missing path returned ENOENT with no child. The
fallback is now gated on `TC_SAMBA4X_NETBSD4_COMPAT`; the NetBSD 6 link map has
`posix_spawn`, the NetBSD 4 maps do not. Every pipe except srvsvc goes to
`local_np.c` to start samba-dcerpcd, which the appliance does not ship. With
the fallback a client opening lsarpc got NT_STATUS_CONNECTION_DISCONNECTED
(the forked child's exec failed and its ready pipe hit EOF); with posix_spawn
the ENOENT maps to NT_STATUS_OBJECT_NAME_NOT_FOUND and no process is created.

0029 is still needed: during `durable_device`'s macOS stall case the file's
ctime moved from 05:04:29 to 05:05:33 across `fd_close` (NetBSD 4 LE log).

- Host regression run (Docker, sanitizers): all 115 cases passed.
- Each lane's stripped smbd has 0062's "shutdown close of" log line. The
  migrators rebuilt byte-identical.
- Devices: both deployed with this smbd (SHA256 matched the repo) at log level
  10, then:
  - `durable_device` passed 25/25 on each, the four cases that failed on the
    old smbd included, and `doctor` passed (86 checks, no warnings) on each.
  - The one-off reproductions not kept in the suite passed on each: another
    tree on the share disconnected, IPC$ connected last, the durable open in
    the first or the second session, and delete on close with a decoy under
    the device's "/" (the share file was deleted, the decoy kept).
  - lsarpc: NetBSD 6 returned NT_STATUS_OBJECT_NAME_NOT_FOUND and NetBSD 4 LE
    NT_STATUS_CONNECTION_DISCONNECTED. On each, a session that failed lsarpc
    three times still served srvsvc, and nothing was left under the client's
    smbd (no child, no zombie; NetBSD 4's fallback children are reaped).
    `smbutil view` listed the shares on each.
  - The log since smbd's start had 14 durable disconnects and no failed one, no
    0062 chdir failure, no failed delete on close, and no panic or signal.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,232,864 |
| NetBSD 4 LE | 10,254,956 |
| NetBSD 4 BE | 10,253,844 |

## aio_fork queue from the event loop (0031), exhausted reconnect status (0024), poll_mt (0051), talloc tracker (0022) (2026-09-28)

0031: only a tevent immediate on the helper list runs the aio_fork FIFO, as
tevent_queue does. Freeing, completing or failing a request only arms it, so no
request is dispatched or completed inside another's destructor or callback; a
scheduler turn completes at most one request synchronously, arming the next
turn first. A request freed while its helper works leaves the helper busy with
the pending reply read (pthreadpool_tevent's orphaned job, without refusing the
free); the reply is dropped and the helper reused, or retired if it failed. The
child destructor now accepts a busy helper that holds such a read, because
smbd frees the tree connection, and the helpers with it, before their replies
arrive. 0024: FILE_NOT_AVAILABLE stays the retry loop's internal signal; after
the retries the client gets OBJECT_NAME_NOT_FOUND (MS-SMB2 3.3.5.9.12, as
upstream). 0051 no longer skips registering poll_mt, which needs no pthread.
0022 also clears the tracker pointer after freeing it.

Two findings about when 0031's teardown path runs:
- A connection reset does not free outstanding requests:
  smbXsrv_connection_shutdown_send waits for every pending SMB2 request, and
  the FIFO runs to completion in both versions. The free-every-request path
  is exit_server (SIGTERM, as when the manager stops smbd), which disconnects
  the transport and then closes files with the requests outstanding.
- With debug logging and 8 MiB reads, smbd reads a connection's next request
  only once a large reply is sent, so the FIFO rarely holds anything. The
  device checks below forced it by stopping (SIGSTOP) both idle helpers, so
  that two reads could never finish and the rest waited.

- Host regression run (Docker, sanitizers): all 120 invocations passed, with
  new `teardown`, `orphan_error`, `exit_no_frames` and `exit_late_frames`
  cases and `queue`, `cancel_active`, `queued_fork_failure` and `exhausted`
  updated. Each of the six mutations in the table at the top of this file was
  applied to the built tree in turn, and every listed case failed; restored,
  they passed.
- Device driver runs: the NetBSD 6 lane build ran every regression driver on
  the NetBSD 6 device (SAMBA4X_RUN_REGRESSION_TESTS=1, 68 invocations), and
  all 47 aio_fork and durable cases passed on the NetBSD 4 LE device by hand.
  `full_buffer` had never run on a device: build/samba4-cross-exec.sh ran
  drivers in the login directory, so its 8 MiB scratch file went to the ~4 MB
  RAM root and the case failed. The wrapper now sets TMPDIR to its /Volumes
  scratch directory (tests/test_build_cross_exec.py).
- The migrators changed too: they link lib/util, and 0022 changed
  talloc_stack.c (16-32 bytes).
- Both LAN devices deployed (debug logging): `doctor` passed 86/86 and
  `durable_device` 25/25 on each. `half-open` now gets OBJECT_NAME_NOT_FOUND
  after 34 retries over 5100 ms ("returning NT_STATUS_OBJECT_NAME_NOT_FOUND" in
  the log).
- aio_fork on (VFS_AIO_FORK_ENABLED=1 in /mnt/Flash/tcapsulesmb.conf, runtime
  restarted), two stopped helpers holding one read each and sixteen more
  8 MiB reads sent, then SIGTERM to that client's smbd. NetBSD 6 queued all
  sixteen; NetBSD 4 LE queued 13 and failed three (below). On NetBSD 4 LE, the
  old smbd was run from a symlinked RAM path for comparison:

  | smbd | Teardown | Fork attempts | Queued reads run | Replies built |
  | --- | ---: | ---: | ---: | ---: |
  | old (0031 before) | 2.7 s | 13 (mmap ENOMEM) | 13 | 13 |
  | new | 10 ms | 0 | 0 | 0 |

  The new smbd freed the two helpers once, with the tree connection, on both
  devices, and nothing panicked. On NetBSD 4 LE the limit was smbd's heap,
  not the device's RAM: both devices keep the default 128 MiB data size limit
  (`ulimit -d`), and the NetBSD 4 smbd's phkmalloc grows its heap with sbrk,
  which that limit caps. Samba allocates each read's 8 MiB reply buffer before
  aio_fork sees the request, so fifteen live reads (two at the helpers, 13
  queued) filled it. The next three reads got NO_MEMORY, and helpers forked
  later could not map their 8 MiB buffers, because NetBSD 4's mmap refuses an
  anonymous mapping larger than the room left under that limit. Both versions
  did this. RAM was not the constraint: a queued read's buffer is not written
  until the read runs. The NetBSD 6 smbd's jemalloc (NetBSD 7 libc) maps large
  blocks with mmap before it tries sbrk, so all eighteen buffers fit there.
  Macs stay far below both: Apple's SMB client (SMBClient source) splits I/O
  into 256 KiB to 1 MiB requests with eight in flight per transfer.

  With aio_fork on, `durable_device` passed 25/25 and `doctor` passed on both
  devices; both were then set back to aio_fork off, and `doctor` passed again.

| Lane | smbd bytes | migrator bytes |
| --- | ---: | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,234,624 | 2,151,036 |
| NetBSD 4 LE | 10,256,880 | 2,166,740 |
| NetBSD 4 BE | 10,255,768 | 2,166,324 |

## DOS device names listed as stored, `mangled names = no` (issue 347) (2026-09-27)

Samba's default, `mangled names = illegal`, applies hash2's `must_mangle()` to
every directory entry. It is true for DOS device names: AUX, CON, NUL, PRN,
COM1-COM4 and LPT1-LPT4, in any case, alone or followed by a dot (`con.txt`,
`prn.tar.gz`). COM5-9 and LPT5-9 are in `reserved_names`, but the precomputed
`char_flags` table allows only the digits 1-4 as a fourth character, so they
pass (`LPT9` was listed as stored). `smbd_dirptr_lanman2_match_fn` replaces
such a name with its 8.3 alias before comparing it with the search pattern, so
an exact-name QUERY_DIRECTORY for `Aux` finds the entry and still returns
NO_SUCH_FILE. macOS looks up each path component with that request: creating
the folder succeeds, and nothing under it can be reached. The reporter's
Carbon Copy Cloner backup failed on GarageBand's `Patches/Aux` and
`Patches/Aux/Shared Aux`.

Device checks on NetBSD 6, without a deploy: `mangled names = no` was added to
the share in the RAM `smb.conf`, smbd was sent SIGHUP, and both were restored
afterwards.
- Default: a Mac `mkdir Aux` succeeded and was listed as `AHY9U3~9`. `ditto`
  of a local `Patches/Aux/Shared Aux/a.patch` failed with
  `.../Patches/Aux/Shared Aux: No such file or directory`, and no request for
  `a.patch` reached smbd. The log showed
  `hash2_name_to_8_3: Aux -> 40B34695 -> AHY9U3~9`, then NO_SUCH_FILE for the
  lookup of `Aux`.
- `mangled names = no`: the listing showed `Aux`, `aux.txt`, `CON`,
  `COM1.log`, `nul.txt` and `prn.tar.gz`, `ditto` copied the whole tree, and
  `aux.txt` read back.
- Names stored with a trailing dot or space by another protocol (written over
  SSH, as AFP and rsync write them): by default they are listed as aliases that
  open (`K5DQBL~2/in.txt` for `King Jr./in.txt`). With `no` they are listed as
  stored, and a stat served from the listing works, but opening them fails: a
  Mac sends a trailing dot or space as U+F029 or U+F028. A Mac stores its own
  such names with those characters (`mac` + U+F029 on disk), and they work
  under both settings.
- Apple's firmware image (`/sbin/wcifsfs`, one static binary with 150 links,
  including the SMB1 server) has no DOS device-name strings; COM1 and LPT1 do
  not occur. It links XNU's `utf8_decodestr`, whose SFM mapping turns a
  trailing space into U+F028 and a trailing dot into U+F029, but its callers
  pass flags 5, 8, 8 or 9, and 4, never `UTF_SFM_CONVERSIONS` (0x20); one
  wrapper has no references.

`tests/native/test_samba_config.py` checks the effective setting on every
share of the default, aio and debug, disk-root on NetBSD 4, and missing-volume
renders; without the line, all four cases fail. Only the service binaries
changed; smbd is unchanged. A rebuild of the previous source first reproduced
the committed service hashes on all three lanes.

| Lane | service bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 365,740 |
| NetBSD 4 LE | 325,064 |
| NetBSD 4 BE | 324,472 |

## NetBSD 4 futimens and patch comment audit (2026-09-28)

A comment audit of every patch, `series` entry and overlay file found one code
bug. NetBSD 4 libc has no `futimens()`, and 0002's replacement in overlay
`lib/replace/tc_netbsd4_compat.c` called `futimes()` only under
`#ifdef HAVE_FUTIMES`, which configure never defines (it checks `lutimes`, not
`futimes`; both NetBSD 4 SDKs have `futimes` in `libc.a`). On both NetBSD 4
lanes every handle-based time update therefore failed with ENOSYS:

- SET_INFO of a last-write time on a handle opened for data returned
  NOT_SUPPORTED and left the mtime unchanged, as Windows `CopyFile` does when
  it copies dates. A handle opened only for attributes worked (smbd serves it by
  name), and so did macOS `cp -p` and `ditto` onto a mount, which set times
  through such a handle. NetBSD 6 was unaffected.
- tdb's transaction commit also calls `futimens(fd, NULL)`, which failed
  quietly.

The shim now always calls `futimes()`; its callers pass real times or NULL,
never UTIME_NOW or UTIME_OMIT. The NetBSD 4 LE and BE link maps now pull
`futimes.o` from libc. New `durable_device` cases `settime:data` and
`settime:attributes` set the time and read it back through a new handle: on
the old smbd NetBSD 4 LE failed `settime:data` (0xC00000BB) and passed the
other, and NetBSD 6 passed both.

0007's `tdb_reopen` hunk was dead on every lane and on host builds (tdb reopens
only where libreplace replaces pread/pwrite), so it was removed; lib/tdb's
`open.c` is now pristine. The patch is renamed `0007-tdb-wrap-o-cloexec.patch`
and keeps only tdb_wrap's `O_CLOEXEC` fallback. Everything else in this change
is comments: stripping comments from both patched trees leaves only those two
code differences. The NetBSD 6 kernel probe behind 0003 (ENOSYS for openat,
fstatat, mkdirat, unlinkat and readlinkat; fdopendir works) is now recorded in
`series`. The corrections cover 0002, 0003, 0004, 0008, 0013, 0014, 0015,
0016, 0017, 0018, 0019, 0023, 0028, 0031, 0041, 0043, 0045, 0049, 0051,
0055-0060, the native-metadata heading and seven overlay files, plus
`_samba4x.sh`'s getifaddrs comments, `config.c`'s aio_fork memory note and
`migration.py`'s note on converting `._` files only with a legacy xattr.tdb.

- Host regression run (Docker, sanitizers): all 120 cases passed.
- `make test-parallel`: 2606 passed.
- NetBSD 4 LE device, with this smbd swapped in: `durable_device` passed 29/29
  (`settime:data` included) and `doctor` passed (86 checks). The swap was then
  undone.
- NetBSD 6 device, with this smbd swapped in after a Time Machine backup: the
  regression drivers ran on the device (68 runs, all passed; the rebuilt lane
  reproduced the committed smbd byte for byte); `durable_device` passed 29/29
  and `doctor` passed (86 checks). `cp -p` and `ditto` onto a Mac mount kept
  the source mtime; lsarpc returned OBJECT_NAME_NOT_FOUND with srvsvc and
  `smbutil view` working; the one-off shutdown-close reproductions (another
  tree disconnected, IPC$ connected last, either session order, delete on
  close with a decoy under "/") all passed; the log since smbd's start had 14
  durable disconnects and no failed one, no chdir failure, no failed delete,
  and no panic or signal. The swap was then undone.
- The three service binaries rebuilt byte-identical (`config.c` comment only).

| Lane | smbd bytes | migrator bytes |
| --- | ---: | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,234,616 | 2,151,044 |
| NetBSD 4 LE | 10,256,960 | 2,166,860 |
| NetBSD 4 BE | 10,255,848 | 2,166,444 |

## Metadata migration: 255-byte names and folder resource forks (2026-09-28)

v3.1.1 telemetry showed two inputs that failed a whole deploy's metadata
migration (exit 4, rerun after rerun):

- A file whose name is 255 bytes. Its sidecar name `._<name>` is 257 bytes,
  which HFS refuses with ENAMETOOLONG (the headers claim `NAME_MAX` 511), so
  only ENOENT counted as "no sidecar". Such a sidecar cannot exist; a
  sidecar path over `PATH_MAX` still fails, since a real one could hide there.
- A folder bundle (`pass.txt.rtfd`) whose `._` file holds a resource fork. A
  device probe on NetBSD 6 showed that an HFS folder has no fork at all:
  `dir/..namedfork/rsrc` is ENOENT for reads and creates, and every
  `com.apple.ResourceFork` xattr call on a directory fd is EPERM (a regular
  file accepts both). The migrator failed with a suppressed ENOENT.

A folder's fork is now kept where it is, through the kept-value path used for
oversized values: the `._` file is kept (`sidecars_kept`), a fork in the Samba
database keeps its row and quarantines the database, and everything else about
the folder still migrates. The report counts them as `folder_forks` and each
kept item carries `reason` (`size` or `folder_fork`); deploy says why they were
kept. An empty fork on a folder holds nothing and is not written.

- Real HFS, NetBSD 6 (`/Volumes/dk2` scratch folder, single-database mode with
  no TDB): the committed migrator failed both trees with exit 4 (the folder
  with no message at all); the new one migrated both with exit 0, kept the
  folder's 1,082-byte `._` file, and an ordinary file with FinderInfo and a
  fork still migrated and lost its sidecar in cleanup.
- Host regression run (Docker, sanitizers): all 122 cases passed, including
  the new `long_names` and `folder_forks` cases.
- Each lane first rebuilt the committed migrator byte for byte from its tree.

| Lane | migrator bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 2,152,124 |
| NetBSD 4 LE | 2,168,000 |
| NetBSD 4 BE | 2,167,584 |

### Real legacy metadata and the on-device `hfs` case (2026-09-28)

- Real TDB, NetBSD 6: copies of the device's own `xattr.tdb.bak` (5,418 rows)
  and quarantined `xattr.tdb.orphaned.1` (32) and `.2` (3) were re-keyed onto
  5,421 scratch files under `/Volumes/dk2`. Deploy's migration code ran with
  MaSt pointed at the scratch folder and the diskd claim skipped. All 5,749
  expected values read back natively byte for byte, the 2 oversized values
  stayed in their quarantined database, the other databases were deleted, and
  a second run skipped the finished volume. The originals were not touched.
  On NetBSD 4 LE the same run completed; its backup had no live rows.
- The `hfs` case (see the README) checks the kernel rules the mocked cases
  assume and runs the single-database program on real HFS. Host run (Docker,
  sanitizers): all 123 cases passed; `hfs` takes its skip path on Linux.
- NetBSD 6, driver built in the netbsd7 lane with `TMPDIR=/Volumes/dk2`: `hfs`
  passed and removed its scratch folder. A mutant driver without the folder
  rule (the v3.1.0 migrator) failed it the way v3.1.1 deploys did: reading the
  folder's `com.apple.ResourceFork` is EPERM and the program exits non-zero.
- NetBSD 4 LE (NetBSD 4.0_STABLE), driver built in the netbsd4le lane with
  `TMPDIR=/Volumes/dk2`: `hfs` passed and removed its scratch folder, so the
  NetBSD 4 kernel gives the same ENAMETOOLONG, ENOENT and EPERM answers. Only
  `hfs` ran there: `long_names` and `folder_forks` keep scratch in `/tmp`, the
  root RAM disk, which had 400 KB free. The NetBSD 4 BE driver was not built.
- The migrator source change only names the folder rule for the driver. Its
  comment keeps the file's line count, since `__location__` strings carry line
  numbers. Each lane built identical stripped migrators from the old and new
  source, and the NetBSD 4 BE one matches the manifest hash, so the committed
  binaries stand.

## *at emulation in libreplace (0002, replaces 0003), directory opens (0063, replaces 0004), talloc stack exit (0022, 0064) (2026-09-28)

Apple's kernels have none of the *at system calls. A probe with the raw
NetBSD 7 syscall numbers got ENOSYS on both devices for openat, fstatat,
mkdirat, unlinkat, readlinkat, renameat, linkat, symlinkat, mknodat, mkfifoat,
utimensat, fchmodat, fchownat and faccessat. futimens works on NetBSD 6 only,
and there it mishandles UTIME_OMIT: an omitted atime makes it change nothing,
and an omitted mtime sets the mtime to -1. NetBSD 4 silently ignores the
O_DIRECTORY and O_CLOEXEC bits. Both report O_NOFOLLOW on a symlink as EFTYPE.
Apple's HFS accepts lutimes() on a symlink but changes nothing. fchdir() plus
opendir(".") listed a 3,000-entry HFS directory exactly on both kernels, after
rewinddir, a chdir("/"), a rename of the directory and a dup2() of its
descriptor. That refutes the 2026-05-10 comment behind 0003's name-based
fdopendir, which no committed code had ever tested.

0003 is gone; `vfs_default.c` and `vfs_catia.c` are upstream again. 0002 and
the overlay `lib/replace/tc_at_emulation.c` emulate every call Samba makes
(the list above minus mkfifoat, fchmodat, fchownat and faccessat, which it
never calls, plus futimens) on both lanes. They work from the directory
descriptor with fchdir(), abort if the working directory cannot be restored,
rename within a directory by its own names and between directories with a
name relative to one of them (below), emulate O_DIRECTORY on
NetBSD 4, and give NetBSD 4 an fdopendir() that opens "." from inside the
directory and keeps the caller's descriptor number.
`DISABLE_VFS_OPEN_HOW_RESOLVE_*` build flags replace 0003's vfswrap_connect
hunk. `build/_samba4x.sh` refuses a binary that still links a libc *at stub.
0004 is gone; 0063 returns FILE_IS_A_DIRECTORY when a name that was absent at
lookup is a directory by the time it is opened (upstream bug, on every system
without O_PATH reopen). 0022 is now only the upstream bug fix (NULL tracker,
freed tracker left behind); 0064 proposes reporting stackframes still open at
exit at debug level.

- Diagnostics: a NetBSD 4 LE smbd built from `main` with level-0 logs at
  0004's flip, at a directory opened as a file in open_file_ntcreate, at
  0050's cached-stat fallback, where upstream's SMB_ASSERT in vfswrap_openat
  would fire, and on any fntimes failure logged none of them during
  smbtorture's smb2.dir, create, rename, delete-on-close-perms and
  compound_find.
- `tc_at_emulation_test` (new, 11 cases) passed on the NetBSD 4 LE device
  (from RAM), the NetBSD 6 device and the Linux host under sanitizers. Its
  first NetBSD 6 run caught the futimens UTIME_OMIT bug; futimens is now
  emulated on both lanes.
- `dir_device.py` (new): `main`'s smbd on NetBSD 4 LE failed renamed-open (a
  directory handle listed the directory that took its old name: 0003's
  name-based fdopendir); NetBSD 6, which has a real fdopendir, passed. The new
  build passed every case on NetBSD 6 and all 85 checks on NetBSD 4 LE,
  renamed-open included, and doctor passed on both. Directory nesting now reaches 40
  levels (1,363 bytes) where `main` refused a directory at about 1,000
  absolute bytes (0003 renamed through absolute names, and smbd creates a
  directory by renaming a temporary one); smbd still reports the deepest
  level's delete-on-close as OBJECT_NAME_INVALID, as it looks up the parent by
  full path past NetBSD's PATH_MAX, but the directory is removed.
- Renames between directories past PATH_MAX. The first build renamed between
  two directories through the old name's absolute name, so moving a file from
  the 40th level to the 39th failed with 0xC0000095 (INTEGER_OVERFLOW):
  getcwd() said ERANGE past 1,024 bytes, which smbd maps unchanged. A probe on
  both devices (22 nested 200-byte directories, 4.4 KB) showed getcwd() names
  a directory up to 4,095 bytes with a 4,096-byte buffer (the kernel caps the
  length at MAXPATHLEN * 4, as in NetBSD's source; libc's getcwd() is the bare
  syscall on both SDKs), and ERANGE beyond whatever the buffer; it finds a
  renamed ancestor's new name; it costs about 90 us near the root and 0.7 ms
  (NetBSD 6) or 0.9 ms (NetBSD 4) at 4 KB; and relative rename() and link()
  work at every depth, up, down, across and through ten "../" from past
  4 KB, with a 1,023-byte name argument accepted and 1,024 refused. The
  emulation now works from one of the two directories and names the other
  relative to it, from whichever side gives the shorter name. It still
  refuses, with ENAMETOOLONG (OBJECT_NAME_INVALID), when that name reaches
  PATH_MAX or getcwd() cannot name a directory, where a real renameat()
  would succeed; that is a limitation, and the test accepts either outcome
  there. The new `cross_directory`
  case fails on the first build with ERANGE and passes on the host under
  sanitizers, as root and as a user; all 11 driver cases pass on NetBSD 6
  and NetBSD 4 LE, and the host regression run (134 runs) passes. Deployed
  (smbd 7b2e936b on NetBSD 6, 098049cf on NetBSD 4 LE), `dir_device.py` passes
  all 88 checks on both, including moving a file from the 40th level up a
  level, into a sibling, to the top of the test folder and back down, and
  doctor passes on both.
- smbtorture baseline (`main`, level-10 logging): NetBSD 4 LE 72 passed,
  52 failed, 2 skipped; NetBSD 6 70/54/2. smb2.rw.invalid (a 1-byte write
  at about 16 TiB) hung the smbd child on both devices: NetBSD 4 rebooted and
  NetBSD 6 stopped answering SSH until power cycled. That is the production
  code; it is excluded from these runs and tracked separately.
- smbtorture with the new build on NetBSD 6 (same suites): 73 passed,
  49 failed, 2 skipped. No test went from success to failure.
  smb2.timestamps.delayed-write-vs-seteof and modern_write_time_update-1 now
  pass. smb2.dir.1kfiles_rename, which could not connect in the loaded
  baseline run (and on NetBSD 4 LE listed 1,292 entries for 1,000 files),
  passed on its own in 882 s. The other smb2.rw differences are only
  renamed subtests. The NetBSD 4 LE smbtorture run with the new build is
  still to do.
- Host regression run (Docker, sanitizers): all 130 runs passed.
- `make test-parallel`: 2606 passed; one telemetry test failed once under
  load (a separate load race, tracked separately).

## File growth beyond the available space on HFS, patch 0065 (2026-09-28)

smbtorture's `smb2.rw.invalid` writes one byte at MAXFILESIZE - 1 (16 TiB) on a
delete-on-close file and accepts success or STATUS_DISK_FULL. Against both
devices the client got IO_TIMEOUT after 60 s. The NetBSD 4 LE device rebooted
about a minute later, and after boot diskd and every process on `/Volumes/dk2`
stayed in D state. On NetBSD 6 the connection's smbd child ran in the kernel
for over 45 minutes, other smbd children blocked in D state and sshd stopped
answering. Both needed a power cycle.

HFS has no sparse files. Measured on the NetBSD 6 disk (1.95 TB, 494 GB free),
in a scratch folder outside the share and removed afterwards:

| Growth | Request returns | Close |
| --- | ---: | ---: |
| SET_INFO end-of-file to 1 GiB (unpatched smbd) | at once | 8.9 s |
| One byte written at 1 GiB (unpatched smbd) | at once | 8.8 s |
| Either, on a delete-on-close file | at once | at once |
| `dd` one byte at 64 MiB / 256 MiB / 1 GiB | | 1.5 / 2.1 / 8.8 s (with close) |

HFS allocates every block up to the new end when the file grows (`ls -s`
showed the full size allocated) and writes the zeros when the file is closed,
about 120 MB/s, unless the file is deleted. The 16 TiB request can never fit,
and the incident shows that attempting it takes HFS tens of minutes even on a
delete-on-close file. Growth that fits is still allowed and still costs about
8.8 s per GiB at close: a client can keep the disk busy for about an hour by
growing a file by the whole free space and closing it, as it could by writing
that much data, only with one request.

0065 (new overlay `source3/smbd/tc_file_growth.c`) answers DISK_FULL, before
anything is allocated, when a write (synchronous or through aio_fork), SET_INFO
end-of-file or server-side copy would grow a file on HFS by more than the
available space `fstatvfs()` reports (what `dfree.sh` reports to clients).
Growth of up to 64 MiB past the size smbd last saw is not checked; larger
growth refreshes the size first. Allocation-size requests already allocate
nothing (`strict allocate = no`), and FSCTL_SET_ZERO_DATA gets ENOSYS on
NetBSD. Server-side copies had no MAXFILESIZE check at all. Time Machine's
sparse bundles on the devices use 487,854,080-byte bands (the NetBSD 6
backup's `Info.plist`), so starting a band past its beginning is checked: one
fstat and one fstatvfs per band.

Review follow-up (same day): a failed size refresh that left errno 0 made
`pwrite_fsync_send()` post its request still in progress (tevent ignores an
error of 0), which smbd would have answered with INVALID_PARAMETER; the check
now reports EIO then. The other three callers were not affected
(`map_nt_error_from_unix(0)` is UNSUCCESSFUL). `run.py stage()` now cuts the
four patched callers from the source tree for `tc_file_growth_test`'s `call_`
cases, `real_resource_fork` checks the resource fork's descriptor on the
device, and `growth_device` gained `stream:resource` and `--aio`. The results
below are for the final build.

- Host regression run (Docker, sanitizers): all 135 invocations passed,
  including the 15 `tc_file_growth_test` ones. Each mutation in the
  sensitivity table failed its case and passed again once restored.
- The suite's safety check (0065's log message in the running smbd, read with
  the device's `sed`) found nothing in the unpatched smbd on either device and
  found it in this one.
- Both devices, with this smbd swapped in (RAM symlink, no reboot; NetBSD 4 LE
  after a power cycle), then the installed smbd restored and every scratch
  folder removed:
  - `tc_file_growth_test` passed all 15 invocations from `/Volumes/dk2`.
    `real_volume` saw `hfs` (505,473,265,664 bytes available on NetBSD 6,
    1,984,190,042,112 on NetBSD 4, whose `fstatvfs()` names the filesystem
    too) and refused growth past it. `real_resource_fork`: a fork's
    `..namedfork/rsrc` descriptor reported the fork's 10 bytes, not the file's
    1, and an HFS volume, and growth past the available space through it was
    refused.
  - `growth_device` passed 23/23 on each. The torture sequence, a one-byte
    write, SET_INFO end-of-file, a one-byte server-side copy and a one-byte
    AFP_Resource write past the volume's size each got DISK_FULL in under
    0.1 s with the file or fork unchanged, and smbd logged each refusal. The
    32 MiB hole, the 96 MiB end of file, 96 MiB of 4 MiB writes and 100 MiB of
    resource-fork growth succeeded. The Time Machine-shaped sparse bundle
    (HFS+, 465 MiB bands) started band 0x28 400 MiB in (419,430,400 bytes)
    and, once grown, band 0x51 313 MiB in (327,979,008 bytes); both growths
    were checked and allowed, and the data read back. (The first version of
    this case put the image's ends only 9 and 18 MB into their bands, inside
    the unchecked growth.)
  - `growth_device --aio` (aio_fork, aio sizes 1 and two helpers written to
    the RAM smb.conf only, restored afterwards) passed 11/11 on each for the
    write cases: both refusals, the hole and the sequential writes. smbd logged
    `pwrite_recv returned -1, err = No space left on device`, so the refusal
    came through `pwrite_fsync_send()`.
  - The free space each run consumed matched the debug log's growth.
- Earlier builds of this change also passed `durable_device --stall 0`
  (24/24) and `doctor` on both devices.
- The NetBSD 4 BE build was compiled and checked as big-endian only; no BE
  device was available.
- Full pytest suite (parallel): 2606 passed.

Upstream: the same four growth paths exist in Samba 4.25. Upstream compares
growth with `get_dfree_info()` only under `strict allocate = yes`: in
`strict_allocate_ftruncate()`, and in `vfs_allocate_file_space()` when
fallocate fails (with the default `strict allocate = no`, that function returns
before its check and allocates nothing). Also only under `strict allocate =
yes`, `vfs_fill_sparse()` grows a file for a write past its end with fallocate
and, where fallocate is not supported, writes zeros up to the offset, with no
free-space check. Server-side copies skip the MAXFILESIZE check that writes
have. An upstream version would put one `get_dfree_info()` check in the four
places 0065 uses, enabled by a share option for filesystems without sparse
files (Samba does not advertise FILE_SUPPORTS_SPARSE_FILES); the torture test
would then expect DISK_FULL there. The copy-chunk range check and a free-space
check in `vfs_fill_sparse()` are bug fixes on their own.

| Lane | smbd bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,235,632 |
| NetBSD 4 LE | 10,257,884 |
| NetBSD 4 BE | 10,256,784 |

All three lanes were built clean (`bin/` removed first); the metadata
migrators are unchanged, byte for byte.

Rebased onto the *at emulation series (0002/0063/0064, 0003 and 0004
removed): 0065 applied unchanged apart from one hunk offset in vfs_default.c,
and the later patches moved only by offsets. The host regression run passed
149 runs under sanitizers (both new drivers included) and the parallel pytest
suite 2670. All three lanes built clean with both drivers; the migrators match
the previous build. From RAM with scratch on the data disk, both devices passed
all 15 `tc_file_growth_test` cases and all 11 `tc_at_emulation_test` cases.
Deployed (smbd b99c9c6b on NetBSD 6, ca32a44c on NetBSD 4 LE), both passed
doctor, `growth_device` (23/23), `growth_device --aio` (11/11) and
`dir_device` (88/88). smbtorture's smb2.rw.invalid, which had hung both
devices, passed on each in 1 s. The full smbtorture run (the suites of the
*at emulation entry, now with all of smb2.rw) against `main`: NetBSD 6 70/54/2
-> 72/52/2 (passed/failed/skipped), NetBSD 4 LE 72/52/2 -> 72/52/2. On both,
smb2.rw.invalid and smb2.rw.append went from failure to success. On NetBSD 4 LE
smb2.name-mangling.mangle, smb2.timestamps.delayed-write-vs-seteof and
modern_write_time_update-1 failed where `main` passed, and delayed-2write
passed where it failed; rerun four times each on the new build, the first
three passed once and failed three times, and delayed-2write passed every
time. They flip on the same build (mangle draws random names; the timestamp
tests compare sub-second write times on HFS's one-second clock), and `main`
fails all three on NetBSD 6, so they are not regressions.

| Lane | smbd bytes (rebased) |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 10,237,080 |
| NetBSD 4 LE | 10,259,240 |
| NetBSD 4 BE | 10,258,120 |

## NetBSD 4 listings that change while a descriptor closes (0002) and maximum access for root (0066) (2026-09-28)

smbtorture's smb2.dir.1kfiles_rename listed 1,292 entries for 1,000 files on
NetBSD 4 LE, on `main` and on the *at emulation build alike. The extra 292
were files an earlier subtest's cleanup (smb2_deltree, which deletes each
64 KiB page of a listing before asking for the next) had left behind. Over
SMB, deleting each 2-4 KiB page of a 2,000-file listing on one handle left 981
files on NetBSD 4 and none on NetBSD 6, always starting at t0169, where smbd's
second 4 KiB getdents() began. A local probe on the device (list, unlink each
batch, open and close another descriptor on the directory) reproduced it
without Samba: plain unlinks lost nothing, an extra open-and-close per batch
lost 896 of 2,000, holding the extra descriptor open lost nothing, and
creating files that sort first made the listing repeat entries without end.
NetBSD 6 was correct in every variant.

The cause, from the running kernels (ksyms and /dev/kmem, disassembled with
the lane toolchains): Apple's HFS resumes a listing by the name of its last
entry only while it holds a directory hint for that listing
(hfs_vnop_readdir asks hfs_getdirhint() for the hint keyed by the offset's
entry number and tag; without one, cat_getdirentries() counts entries from
the start). hfs_vnop_close() calls hfs_reldirhints(cp, busy) for a directory
still in use, meant to free hints older than 45 seconds; hfs_getdirhint()
stamps hints with the wall clock (`time`) but hfs_reldirhints() subtracts
them from microuptime() (`time - boottime`). NetBSD 4 compares the result as
an unsigned 32-bit number, so every hint looks stale and is freed on any
close; NetBSD 6 does the same subtraction in signed 64 bits, gets a negative
age and keeps them. smbd opens and closes the parent directory for every
create and delete, so on NetBSD 4 a listing lost its place after each one.

rep_fdopendir() on NetBSD 4 now reads the whole directory up front (HFS
returns at most 64 KiB, about 2,000 entries, per getdents(); the read is made
again if the directory's size or times changed meanwhile), shrinks the buffer
to what it read, and serves readdir() from it with libc's __DTF_READALL, as
NetBSD's own opendir() does for NFS and union mounts; rep_rewinddir() reads it
again. A 2,000-entry directory reads in 13 ms and a 20,000-entry one in about
0.1 s, about 32 bytes per entry; the Time Machine band directory on the
NetBSD 6 device holds 3,026 entries. NetBSD 6 binaries are unchanged by this.

smb2.maximum_allowed.read_only_file failed on both devices: with
`force user = root`, smbd_calculate_maximum_allowed_access_fsp() returned full
access for root before removing write access for a read-only share or a
read-only file, so a MAXIMUM_ALLOWED open of a read-only file asked for write
access and the read-only check refused it with ACCESS_DENIED (also in Samba
master of 2026-09-25). 0066 lets root skip only the ACL check.

- `tc_at_emulation_test` `listing_changes` (new): 600 files listed through
  fdopendir() in batches of 64 while each batch is deleted, and while files
  that sort first are created, with another descriptor on the directory
  opened and closed between batches; then rewinddir(). Passes on the Linux
  host under sanitizers.
- `dir_device.py` `listing-changes` and `read-only` (new). Against the
  previous build (smbd b99c9c6b and ca32a44c): on NetBSD 4 LE, deleting each
  2 KiB page as it arrived left 521 of 1,200 files; creating files during a
  listing repeated 50 names on both devices; MAXIMUM_ALLOWED on a read-only
  file was ACCESS_DENIED on both.
- Host: the regression run passed 150/150 under sanitizers, the pytest suite
  2,678. All three lanes built clean; the NetBSD 6 migrator is unchanged.
- Devices, from RAM with scratch on the data disk: `tc_at_emulation_test`
  12/12 (with `listing_changes` and the 4,095-byte `cross_directory`
  boundary) and `tc_file_growth_test` 15/15 on both.
- Deployed (smbd 5f2f7730 on NetBSD 6, 4279108e on NetBSD 4 LE): doctor and
  `growth_device` (23/23) passed on both. `dir_device.py` passed 100/100 on
  NetBSD 4 LE: nothing left behind or repeated, the restart shows the new
  file, MAXIMUM_ALLOWED on the read-only file grants 0x001f01b9 (no write,
  append or delete-child), a write open is still refused, the maximal-access
  context still reports 0x001f01ff, and the deep hard links name the same
  file. NetBSD 6 passed 99/100: creating files during a listing still repeats
  the last 50 names there (below).
- Also found: at the end of a directory HFS releases the listing's hint (as
  Apple's source does), and the offset still counts entries, so a readdir()
  after the end returns the last entries again when files were created before
  the listing's position (probed on both kernels without Samba: 20 created,
  20 repeated). smbd reads again after the end for the client's next
  request. NetBSD 4's full read keeps returning the end. From the NetBSD 6
  kernel: hfs_vnop_readdir() calls hfs_reldirhint() when a call returns no
  entries (the offset comes back unchanged), and NetBSD 7's libc readdir()
  calls getdents() again on every call after the end; smbd's ReadDirName()
  has no end state either. NetBSD 6 keeps libc's fdopendir() (its hints
  survive closes) and gets a rep_readdir() that keeps reporting the end until
  rep_rewinddir(), with a flag bit in the DIR that libc does not use; libc's
  rewinddir() rebuilds the stream from the current flags, so the bit is
  cleared first. NetBSD 6's mid-listing correctness relies on its hints never
  going stale (the signed comparison above), and a directory keeps at most 32
  hints, recycling the least recently used, so dozens of listings of one
  folder at once could still cost one its place.
- With the NetBSD 6 change: host regression 150/150, pytest 2,678; all three
  lanes built clean, and the NetBSD 4 LE and BE smbd and migrators came out
  byte-identical to the previous build (4279108e, a7a52000), so NetBSD 4 was
  not redeployed. On NetBSD 6 the old smbd (5f2f7730) repeated 50 names while
  files were created during a listing and 5 more when queried after the end;
  with smbd 1556a125 `tc_at_emulation_test` passed 12/12 on both devices
  (`listing_changes` now also reads after the end and rewinds twice), and
  doctor, `dir_device.py` (101/101 on each) and `growth_device` (23/23 on
  each) passed.

## smbtorture's full list with smbd 1556a125 and 4279108e (2026-09-29)

- Full list, one suite per run (NetBSD 6 smbd 1556a125, NetBSD 4 LE
  4279108e): no new failures on either device (NetBSD 6 78 passed, 52 known
  failures, 2 skipped; NetBSD 4 LE 77, 52, 2).
  `smb2.maximum_allowed.read_only_file` now passes on both (0066). On NetBSD
  4 LE `smb2.dir` as one run hit the 30-minute limit before
  `1kfiles_rename`, so `tests/samba/torture.py` now runs `smb2.dir` and
  `smb2.compound_find` subtest by subtest.
- Reruns through `tests/samba/torture.py`: on NetBSD 4 LE
  `smb2.dir.1kfiles_rename` passed, 100 renames with the listing in 491 s
  (739 s in all). On NetBSD 6 `smb2.dir.large-files` passed (405 s) and the
  `1kfiles_rename` run started right after it could not connect
  (NT_STATUS_IO_TIMEOUT after 60 s), as in the full run, where the two ran in
  one smbtorture process. Run on its own the day before, `1kfiles_rename`
  passed on NetBSD 6 (882 s). Under investigation; the known-failures entry
  stays until then.
- The rest of the full tier on the same binaries (the series cleanup below
  rebuilt them byte-identical): `growth_device.py --aio` with
  `write:maxfilesize`, `write:past-volume`, `allowed:hole` and
  `allowed:sequential` 11/11, `durable_device.py` 29/29 and
  `links_device.py` 85/85 on both devices; no smbd children or test folders
  were left behind.

## Upstream fixes split out of 0018, 0035 and 0055; issue and version references dropped (2026-09-29)

- 0067 (the fruit_pwrite_meta() zero-fill, from 0055), 0068 (the
  xattr_tdb_setattr() frame leak, from 0018) and 0069 (the
  streams_xattr_unlinkat() error returns, from 0035) are their own patches
  in the upstream bug-fix section; all three bugs are still in Samba master
  (checked 2026-09-29). The GitHub issue links (0023, 0027, 0050), the
  issue number (0035) and the "rc2"/"Samba 4.25" wording (0005, 0029, 0036,
  the series and overlay tc_embedded_srvsvc.c) became plain descriptions,
  keeping every file's line count.
- The replay's final tree differs from the previous series only in those
  comment lines, `verify` matched, and a clean build of all three lanes gave
  byte-identical smbd and migrators (1556a125, 4279108e, a7a52000; 72691cce,
  aed8ee6e, 41b33052), so nothing was redeployed. pytest 2,729.

## NetBSD 6: a connection child spinning in talloc after `large-files` (2026-09-29)

- Symptom: smbtorture's first connection after `smb2.dir.large-files`
  timed out (NT_STATUS_IO_TIMEOUT after 60 s), both when the two ran in one
  smbtorture process and in separate containers. The smbd child forked for
  that connection (pid 22862) was still running 45 minutes later at 100% CPU,
  all user time, 9 minor faults, 160 KB resident, and had logged nothing.
- Registers, read without stopping it (`ps -o uaddr`, then the trapframe at
  the top of its uarea through /dev/kmem), and later with a ptrace reader:
  the loop is `_tc_free_children_internal` / `_tc_free_internal`, under the
  child's first action after fork(), `talloc_free(s->parent)` in
  smbd_accept_connection() (location server.c:1365).
- The talloc tree it frees, from its heap (symbols from the lane tree's
  unstripped smbd, which strips to the deployed 1556a125):
  `struct smbd_parent_context`'s first child is a freed 16-byte chunk
  (FREE|LOOP, stamped "talloc.c:1765", the talloc_pop() of the parent's
  per-loop stackframe), so `_tc_free_internal` returns 0 without unlinking it
  and the loop never ends. `children` (offset 0x64) and `num_children` (1)
  still name that chunk, whose data is the list entry of the previous child,
  21383 (the `large-files` connection, which exited 0.3 s earlier).
- The same heap page also holds the list entry for 22862 itself, which the
  parent creates with add_child_pid() only after fork() returns, and shows
  21383's entry already freed and its memory reused by cleanupd's per-child
  record. The pages holding `smbd_parent_context` and the listening
  `tevent_fd` show the state at fork(). So one page of the child's heap
  carries writes the parent made after fork(): the parent accepted the new
  connection before handling 21383's SIGCHLD, then removed that entry,
  added the new one and ran cleanupd (patch 0008 runs it in the parent).
- Not reproduced outside smbd: static test programs on the NetBSD 6 device
  (private anonymous memory and libc malloc; plain writes, concurrent
  child reads, MADV_FREE before and after fork(), repeated forks, with and
  without MADV_RANDOM) never showed a child the parent's post-fork writes.
  libc's jemalloc does call madvise(MADV_FREE) when it purges pages.
- Other connections are unaffected: a new connection made while 22862 was
  spinning got a normal child. The parent keeps running; each such child
  holds one CPU until it is killed or the device restarts.

## The fork() repair for Apple's NetBSD 6 kernel, patch 0070 (2026-09-29)

- Root cause of the spinning child above. The NetBSD 6 kernel's ARM pmap
  (stock NetBSD 6 code; the device is a single-core Cortex-A9) marks a page
  unreferenced by making its PTE invalid and keeping the rest.
  pmap_protect() skips invalid PTEs (`l2pte_valid` is `type != INV`), so when
  fork() write-protects the parent, such a page keeps PVF_WRITE in its pv
  entry, and the parent's next write goes through the pmap's "modified"
  emulation, which makes the page writable without a copy-on-write fault.
  The page daemon clears references under memory pressure (as after
  `large-files`); madvise(MADV_DONTNEED) does it on demand. Apple's
  uvmspace_fork, uvm_fault and amap_copy disassemble to the stock code. NetBSD
  4's pmap_protect() treats any non-zero PTE as present and is not affected
  (0 of 16 pages on the NetBSD 4 LE device).
- Measured before choosing: mlockall() in the parent (the kernel then copies
  the parent's memory to the child at fork) cost 15 ms and about 4 MB per
  connection child, since it faults in the whole 2 MiB stack region and every
  heap chunk; repairing before fork() cost 0.4 ms and nothing.
- The repair (overlay lib/replace/tc_fork_repair.c): right before fork(),
  every private mapping is protected PROT_NONE and straight back (in two raw
  system calls, so nothing touches the range meanwhile) and the stack in use
  is written back a page at a time; each repaired range also gets
  MADV_RANDOM. Linker wrappers (fork, _fork, mmap, _mmap, munmap, mremap,
  mprotect) keep a registry of private mappings; the data segment runs from
  __preinit_array_start to sbrk(0), the stack to __ps_strings. A full
  registry falls back to mlockall() around the fork. smbd, the migrator and
  the drivers link it on the NetBSD 6 lane (the wrappers as a separate object
  on the final static links only: configure's probes and Samba's shared
  libreplace must not see them), and so do the NetBSD 6 service and rsync.
  Staging refuses a NetBSD 6 binary that links libc's fork or mmap family
  without the wrappers (`verify_fork_repair`).
- Found while validating: weak `__real_` references leave libc's mmap
  unlinked (every binary died with "TLS allocation failed"); without
  MADV_RANDOM, .bss still leaked, because jemalloc's atfork handler writes
  between the repair and the system call and fault-ahead enters the
  neighbouring pages unreferenced; rsync's reused build directory did not
  relink after a flags change (the staging check caught it; the build now
  always relinks).
- `tc_fork_repair_test` on NetBSD 6: a plain fork() leaked 28 of 28 pages
  (.data, .bss, sbrk heap, jemalloc blocks, a huge block, a private mapping,
  the stack); through the repair 0, three forks in a row, after a read, in a
  nested fork and on the fallback; `regions` and `read_after_repair` passed 20
  of 20 runs. `bounds`: data 0x4d000-0x7b000, stack top 0x7ffff000 (the end
  of the stack mapping).
- With the NetBSD 6 smbd swapped in (59da9f12): doctor passed; every private
  writable mapping of the parent and of a connection child is in the
  registry, the data run or the stack, except 0x99f000-0x9b4000 (.eh_frame,
  no anonymous pages: never written); `smb2.dir.large-files` then
  `smb2.dir.1kfiles_rename` passed (the listing in 525 s), and no smbd child
  was left running. Connection setup (median of 12): 381 ms with the old
  smbd, 391 ms with this one, 452 ms with the old one again; an idle
  connection child took 352 KiB; the parent's map stayed at 28 entries over
  50 connections.
- Clean builds: NetBSD 6 smbd 59da9f12 (+2,608 bytes), migrator 5b3ee6bc,
  service ac37b22f (+2,376), rsync b20b8c4d (+2,376). The NetBSD 4 LE and BE
  smbd, migrators, services and rsync are byte-identical to the previous
  build. pytest and the host regression (the driver skips there) pass.
- Full tier, deployed (NetBSD 6: smbd 59da9f12, service ac37b22f, rsync
  b20b8c4d; NetBSD 4 LE unchanged, smbd 4279108e, service 5b5c579e): doctor
  86/86 on both; `dir_device.py` 101/101, `growth_device.py` 23/23, its
  `--aio` subset 11/11, `durable_device.py` 29/29 and `links_device.py` 85/85
  on both; the tier's drivers 33/33 on both (on NetBSD 4 "kernel" checks that
  a plain fork() leaks nothing), and every driver case 88/88 on NetBSD 6
  (`tc_aio_fork_test`'s helpers are forked through the repair). smbtorture's
  full list: NetBSD 6 79 passed, 51 failed, all known (`smb2.dir.1kfiles_rename`
  now passes after `large-files`, and its known-failure entry is gone);
  NetBSD 4 LE 78, 52, all known (`1kfiles_rename` now runs as its own
  subtest). The new stuck-child check found none. Sampled once a minute during
  NetBSD 6's run, no smbd process held more than 3 registry ranges (of 512),
  with no fallback.

## Fork repair follow-ups: advice, remap, lock failure; every driver in the full tier (2026-09-29)

- `tc_fork_repair.c`:
  - After `fork()` returns, the parent and the child put `MADV_NORMAL` back on
    what the repair gave `MADV_RANDOM` (the registry ranges, the sbrk() heap
    and the stack), so neither process loses UVM fault-ahead between forks.
    The static data and .bss keep `MADV_RANDOM` for patch 0046 (and the
    service's and rsync's constructors).
  - `mremap()` drops whatever the registry held at the new range even when
    the old range was unrecorded. NetBSD's `uvm_mremap()` never moves onto
    mapped pages (`uvm_map_reserve()` refuses them), so this only clears
    stale entries; the host unit tests cover it.
  - The fallback checks `mlockall()`: when it fails, fork() still runs, logs
    "fork() unprotected" once and counts `tc_fork_repair_lock_failures`.
- `tests.samba.check`: the full tier runs `run.execution_cases()` (every
  driver); a driver goes to `/mnt/Memory` when `df` shows room, else to the
  data disk on NetBSD 6, and is skipped with a log line on NetBSD 4.
- Validation, Mac and VM only (no device run, no deploy): pytest 2,768
  passed; host regression passed. Clean NetBSD 6 lane: smbd a47880b7
  (+496 bytes), migrator 4c07bc68 (same size), service d6d046e7 (+416),
  rsync 6849fe9f (+416), each passing `verify_fork_repair`. The NetBSD 4 LE
  and BE services and rsync are byte-identical to the committed ones.

## File times past 2038 and 2040, and before 1970 (2026-09-29)

What each layer keeps, probed on both devices by setting times with utimes()
and reading them back after the kernel dropped its cached vnodes (creating 2 x
`kern.maxvnodes` empty files and looking each up twice; a file holding cached
pages sits on the hold list and outlives that):
- Both kernels store `(uint32)t + 2082844800` modulo 2^32 in HFS and read a
  time back as that minus 2082844800, except that anything before 1970 reads
  as 0. So 1904-1969 is stored but reads as 1970; 2040-02-06 06:28:16 and 2106
  read as 1970; 15032385535 as 2038-01-19; 1901-1903 lands in 2038-2040. Until
  the vnode is dropped, stat returns exactly what was set. Both take -1 in both
  times as "leave them alone" (VNOVAL); one -1 beside another time is stored.
- NetBSD 6 keeps 1970 to 2040-02-06 06:28:15 exactly. NetBSD 4's 32-bit
  time_t ends at 2038-01-19 03:14:07 (its kernel reads 2038-2040 HFS dates as
  1901-1903).
- Apple's afpserver (set from a Mac with mount_afp): only 1970-01-01 to
  2038-01-19 03:14:07 is kept; every other time, 2039 and 1968 included, is
  stored as 0 and shown to the Mac as an invalid date (2068-01-19), on both.
- Samba before this change: TIME_T_MAX was INT32_MAX on every lane (the NetBSD
  7 libc gmtime() fails configure's 64-bit probe with EOVERFLOW), so every
  time after 2038-01-19 03:14:07 was stored as that second and read back as
  "never" (the year 30828). NetBSD 4 was worse: nt_time_to_unix_timespec_raw()
  cast to the 32-bit time_t before the TIME_T_MAX check, so 2040-02-05 became
  1903-12-30 and 2106-02-07 became 1969-12-31.

The change (deliberately better than afpserver, which throws dates away):
- NetBSD 6 lane: `-DTIME_T_MAX=253402300799LL` (year 9999, where gmtime works).
- Patch 0071 (upstream fix): saturate nt_time_to_unix_timespec_raw() at
  time_t's range, so the TIME_T_MAX/TIME_T_MIN clamps apply on 32-bit time_t.
- `tc_at_emulation.c`: every explicit time is clamped to what HFS keeps
  (1904-01-01 to 2040-02-06 06:28:15; 2038-01-19 03:14:06 on NetBSD 4), and -1
  in both times becomes -2. Times before 1970 still read back as 1970 once
  uncached: nothing short of reading the catalog would fix that.

Validation (no deploy, no full tier): pytest passed; host regression passed
(time_range on a 64-bit host, no HFS). Clean builds of all three lanes: smbd
6 42d66742 (+272 bytes), 4le 6d35fc5d (+260), 4be 31543efe (+268); migrators
6 2900f769, 4le 14089612, 4be fa82a935. When the history was squashed the
fork repair's read-only skip moved before this change, and a clean NetBSD 6
rebuild gave smbd f5276dca (the migrator byte-identical); the device checks
below ran with 42d66742. With each device's new smbd swapped in:
- `tc_at_emulation_test time_range` read back from the catalog: passed on
  both (NetBSD 6: 2038-01-18, 2040-02-05 and HFS's last second exact, later
  times at 2040-02-06 06:28:15, before 1904 at 1904; NetBSD 4: 2038-01-18
  exact, later times at 2038-01-19 03:14:06). The other quick-tier driver
  cases passed.
- `dir_device.py --case times`: all 7 passed on both, including 2040-02-05
  and 2106 through SMB (NetBSD 4: both at 2038-01-19 03:14:06).
- `smb2.timestamps`: no unknown failures on either; the three time_t tests
  past 2106 still fail as listed.

## Regression drivers from a RAM disk of their own (2026-09-29)

`tests.samba.check`'s driver step mounts a 12 MiB RAM disk at `/mnt/TcTests`
(tmpfs on NetBSD 6, a 24576-sector mfs on NetBSD 4, which has no tmpfs), runs
every driver from it one at a time with its working directory and TMPDIR on
the data disk, and unmounts it afterwards (a leftover from a crashed run is
unmounted first; one that will not unmount fails the step). Before this, a
driver ran from `/mnt/Memory` only when it fitted (3-5 MB free with the runtime
installed) and was skipped on NetBSD 4 otherwise, so the 2-10 MB drivers never
ran there. The first NetBSD 4 run showed why `tc_xattr_migrate_test` was
listed as NetBSD 6 only: its off-HFS cases make their scratch under /tmp, a
10 MB RAM disk with about 250 KB free there, and its oversized cases failed
with ENOSPC. They now take `TC_MIGRATE_SCRATCH` (the drivers' RAM disk, UFS
like /tmp), which the step sets on NetBSD 4 only.

Validation: the full tier's driver plan, 89 invocations, passed on both
devices from the RAM disk (NetBSD 6 265 s, NetBSD 4 LE 233 s), every driver
included; the RAM disk and scratch were gone afterwards. pytest passed.

## Fork repair skips read-only mappings (2026-09-29)

The repair cycled and advised every recorded private mapping, read-only ones
included, though the parent cannot write to those after fork(), so nothing of
theirs can leak. It now skips ranges without PROT_WRITE (they stay in the
registry, so an mprotect() that makes one writable is still followed).
Validation: the host unit tests (`read_only_ranges_skipped` is new). Clean
NetBSD 6 builds: smbd 4d288d44, service 21e8bb57, rsync 17b5b495, each
passing `verify_fork_repair`; the migrator came out byte-identical
(4c07bc68). Not run on a device.

## A lone retained database drops its verified rows (2026-09-30)

A receipt that is lost or damaged while a legacy `xattr.tdb` is retained (some
of its rows belong to a disk that is not attached) made the next deploy walk
every volume again and rewrite native values from the verified rows still in
the database, over edits made since. With a single source, a deferred retire
now copies the unchanged database to the next free `xattr.tdb.orphaned.N` and
drops the rows cleanup verified, 1,000 per TDB transaction; deploy saves the
receipt with the new fingerprint and without the dropped keys. `inspect`
replays a TDB recovery area a power loss left behind, since TDB refuses
read-only opens until a writer does. Several sources are unchanged.

Validation:
- pytest (2805 passed) and the host regression in Docker with sanitizers,
  including the new `drop_verified` case.
- NetBSD 6 and NetBSD 4 LE, every `tc_xattr_migrate_test` case from the
  drivers' RAM disk (the recovery case replays a real prepared commit there).
- NetBSD 6, deploy's own `migration.py` over SSH against a copy of the
  device's `xattr.tdb.bak` (5,418 rows from 2026-09-14, all on dk2) plus one
  row of a missing `dk7`, in a scratch directory on dk2, with a receipt that
  recorded dk2 complete (5,362 rows verified, 56 orphaned). Only the retire
  step ran: no file was walked or changed and smbd kept serving a Time
  Machine backup. The retire took 8.2 s end to end (the helper under 2 s),
  left 57 rows (56 orphans and the missing disk's row), made
  `xattr.tdb.orphaned.1` byte-identical to the input, and saved a receipt
  that decodes with the database's new fingerprint (14b435d7...) and only the
  56 orphan keys. The file grew from 8,880,128 to 11,100,160 bytes: TDB's
  transaction recovery area. A second retire dropped nothing and left the
  database byte-identical. SIGKILL during the drop left 4,419 and 1,419 rows
  (whole 1,000-row chunks); the next `inspect` opened the database, and the
  stale receipt made the next deploy's completion empty (a walk, as intended).
- Clean builds of all three lanes; smbd came out byte-identical on each. The
  migrator: NetBSD 6 2,157,224 bytes (35775320, the binary tested above),
  NetBSD 4 LE 2,171,080 (98e5ed66), NetBSD 4 BE 2,170,640 (e2c7d81b), each
  about 3-5 KB larger.
- NetBSD 4 LE with the committed migrator (392670f0; a clean LE build of
  6f83f1af reproduces it byte for byte). Every `tc_xattr_migrate_test` case
  passed from the drivers' RAM disk. The same retire against the NetBSD 6
  `.bak` copy dropped 5,362 rows in 15-16 s end to end, left 57 and a
  byte-identical copy; the helper's peak RSS was 3.7 MB (3.8 MB virtual).
- NetBSD 4 LE deploys (`tcapsule deploy`, four in a row). The device's own
  `xattr.tdb.bak` is an empty TDB, so a 5-row database was built with
  tdbtool: one DOS-attribute row (from the NetBSD 6 `.bak`) for each of three
  new files on dk2, an orphan row and a row of a missing dk7.
  1. Walked dk2, verified the three files (their creation time over SMB became
     the row's, 2026-09-14), deferred retirement for dk7, copied the unchanged
     file to `xattr.tdb.orphaned.1` and dropped the 3 rows. The receipt
     decoded with the new fingerprint and only the orphan key.
  2. A file's creation date changed over SMB (SetFile) to 2025-01-02 and the
     receipt corrupted: the deploy walked dk2 again, dropped and copied
     nothing, left the database unchanged, and the edit stayed.
  3. Control: the full 5-row database restored and the receipt removed, as the
     previous build left things. The walk rewrote the edited date back to
     2026-09-14 (the replay this change prevents), then dropped the rows again
     into `xattr.tdb.orphaned.2`.
  4. The edit made again, then a deploy with a valid receipt: no walk, no drop,
     database and receipt byte-identical, the edit intact.
  Doctor passed afterwards. The test files were removed.
- NetBSD 6 with the committed migrator (a45e636f; a clean NetBSD 6 build of
  6f83f1af reproduces it byte for byte, smbd unchanged). Every
  `tc_xattr_migrate_test` case passed. The `.bak` retire: 8.2 s, peak RSS
  3.4 MB; a rerun left the database byte-identical; SIGKILL before the first
  commit left every row and the receipt valid, after three chunks 2,419 rows,
  after all six 57, each reopened by the next `inspect`.
- NetBSD 6 deploys, four in a row, with the device's real `xattr.tdb.bak`
  (5,418 rows, nearly all Time Machine band files) plus the dk7 row and
  DOS-attribute rows for three new files: 5,422 rows.
  1. Walked dk2 (6,230 entries), deferred retirement for dk7, copied the
     unchanged file to `xattr.tdb.orphaned.3` (the device's own `.1` and `.2`
     untouched) and dropped 5,339 verified rows; 83 remained (82 rows whose
     band files a later backup had replaced, and dk7's). The receipt decoded
     with the new fingerprint and only the 82 orphan keys.
  2. A file's creation date changed over SMB to 2025-01-02 and the receipt
     corrupted: a walk, no drop, no copy, the database unchanged, the edit kept.
  3. Control: the full database restored without a receipt. The walk put the
     date back to 2026-09-14, then dropped the rows again (`.orphaned.4`).
  4. The edit made again, then a deploy with a valid receipt: no walk, no drop,
     database and receipt byte-identical, the edit intact.
  The test files were removed; doctor passed on both devices afterwards.
- Not run: NetBSD 4 BE.

## An all-zero FinderInfo in a legacy row (2026-09-30)

v3.1.1 telemetry: one NetBSD 6 install failed three deploys on the same file
with "native verification failed ... name=com.apple.FinderInfo", then
"metadata migration failed ... Input/output error". A static probe calling the
migrator's native xattr syscalls on scratch files under `/Volumes/dk2` (NetBSD
6, then NetBSD 4 LE from `/mnt/Memory`) showed how both kernels store
FinderInfo. Every 32-byte value read back byte for byte on a file and a folder:
each of the 256 single bits over zero and over `M4A `/`hook`, every field set
to 0xff, `slnk`/`rhap` and `hlnk`/`hfs+`. All zeros did not: the write
succeeds (NetBSD 6 returns 0, NetBSD 4 the 32 bytes written) and removes the
attribute, and both reads then report ENOATTR. The TDB path wrote such a value
and its verification read found nothing (cleanup would have reported a
mismatch the same way); the `._` path already skipped one.

An all-zero legacy FinderInfo, as a netatalk entry, an `AFP_AfpInfo` stream or
the raw Apple name, now migrates as no FinderInfo: nothing is written and any
native FinderInfo stays, as for a row without one. fruit lists no
`AFP_AfpInfo` stream for it and removes the stream when a client writes zeros,
as a macOS server does.

Validation:
- Host regression in Docker with sanitizers: all 159 invocations passed. The
  `tdb` case runs the three forms with and without a native FinderInfo through
  the program's copy and cleanup, plus a value with one extended-half bit set
  that still replaces the native one. The syscall mock now drops an all-zero
  FinderInfo write as the kernels do. With the skip disabled, `tdb` failed
  with "native verification failed", as the deploys did.
- NetBSD 4 LE, the netbsd4le lane's driver from the drivers' RAM disk with
  `TMPDIR` on dk2: `tdb`, `hfs` (on real HFS: the kernel rule, then two
  all-zero rows through the program) and `all` passed.
- pytest: 2805 passed.
- Clean builds of all three lanes; smbd came out byte-identical on each. The
  migrator: NetBSD 6 2,157,304 bytes (a45e636f), NetBSD 4 LE 2,171,156
  (392670f0), NetBSD 4 BE 2,170,716 (3b67a7c1), 76-80 bytes larger than the
  builds in the entry above, whose change they include.
- Not run: anything on NetBSD 6 (a Time Machine backup was in progress), a
  deploy, and NetBSD 4 BE.

## Interface table: address-less interfaces never truncate it; split causes (2026-10-01)

Two NetBSD 6 bridges (C86NX2YT on v3.1.1/v3.1.2, C86SN0A5 on v3.1.0) sent
`plan_error: iflist-truncated` in every heartbeat. Their plans never
validated, so discovery registered nothing on a cold start, and deploy
verification failed. Both are on networks with several Apple base stations.
The firmware's ifconfig knows per-peer WDS interfaces (`wds_remote_mac`,
`dwds_role`), so extenders likely add interfaces, and our NetBSD 6 device
already has 10 of the 16 allowed, with addresses on only `lo0` and
`bridge0`. Which cause hit those devices is not known: one code covered three.

The parser now reads the table twice. The first pass counts the kernel's rows
and finds every interface that owns one of the stored addresses, including
owners with no RTM_IFINFO row (the plan gives those a synthetic link). The
second pass stores every owner and lets interfaces with no address fill only
the room that is left, in kernel order; dropping one of those is not a
truncation, because it can never take a role. `TC_MAX_LINKS` is 32. A link
still holds as many addresses as the table (64): a lower per-link cap would
fail a link that keeps many IPv6 addresses through prefix rotation. The plan
roughly doubles in size (below).

Each cause has its own code: `iflist-sockaddr`, then `iflist-links`, then
`iflist-addrs` (a malformed row makes the other counts unreliable). A sysctl
or framing failure is still `iflist` and ranks first. A synthetic link that
finds no plan slot (the parser reserves one for every owner, so this is a bug
guard) reports `iflist-links`; it used to vanish from a validated plan. The
old `addrs` reason, for a link holding more addresses than its slots, is gone:
a link holds the whole table, and a compile-time check keeps it so. The plan
line carries the kernel's totals for `iflist-*` codes. The heartbeat adds
`iflist_links` and `iflist_addrs` for those codes, and leaves out isolated
links with no service address (the server already dropped them).

`HEARTBEAT_MAX_JSON` is 16384. `json_escape` refuses a field longer than its
buffer in payload.c, so those buffers bound the payload at about 9.5 KB with
TC_MAX_LINKS escaped link names; a payload that does not fit sends no
heartbeat. The escaped link name buffer now holds a six-byte escape per byte;
at 32 bytes a name of control bytes stopped the heartbeat.

Discovery's "plan incomplete" line now names the reason for every incomplete
plan (`reason=mode`, `reason=laIP`, ...), not only for `iflist-*`, and adds
the kernel's totals for those. Doctor shows that line. Because the line
changes with the reason, a plan whose reason alternates (say between `mode`
and `laIP` while ACPd is slow) logs a line per change where it logged one.

| Deliberately broken behavior | Case that rejects it |
| --- | --- |
| Count address-less interfaces toward the limit | `test_iflist_extra_address_less_interfaces_are_dropped_not_truncated` |
| Reserve no slot for an owner without RTM_IFINFO | `test_iflist_owner_without_ifinfo_reserves_its_plan_slot` |
| Drop a synthetic link's address silently when no slot is free | `test_topology_synthetic_link_without_a_free_slot_is_incomplete` |
| Let the last truncation cause win | `test_iflist_malformed_row_outranks_interface_overflow`, `test_iflist_interface_overflow_outranks_address_overflow` |
| Leave the kernel totals out of the plan | `test_topology_incomplete_table_names_its_cause_and_the_kernel_totals` |
| Send every link in the heartbeat | `test_heartbeat_reports_only_links_with_a_role_or_service_address` |
| 4096-byte heartbeat buffer; 32-byte escaped link name | `test_largest_possible_heartbeat_still_fits` |
| Leave the kernel totals out of the heartbeat | `test_plan_error_is_short_and_omitted_after_recovery` |
| Leave the cause out of discovery's plan line | `test_incomplete_interface_table_is_logged_with_its_cause_then_recovers` |

These were run against the build with 32 addresses per link; none of them
depends on the per-link cap. `test_iflist_table_that_fits_is_stored_in_kernel_order_unchanged`
(added with the revert) checks that a table of up to 32 interfaces is stored
exactly as the kernel lists it, so devices within the old limit get the
table, and therefore the plan, they had.

Sizes (macOS arm64 host build): `struct device_plan` 42,120 to 83,992 bytes
(32 links of 64 address slots), `struct device_facts` 5,840 to 6,240,
`struct registrant` 8,088 to 10,968 (its entries scale with `TC_MAX_LINKS`;
the test bound is 16 KiB). Discovery keeps two plans in its loop, on its
stack; telemetry and `--print-link-plan` one each. Stripped service: NetBSD 6
368,620 to 370,004 bytes, NetBSD 4 LE 325,180 to 326,708, NetBSD 4 BE 324,588
to 326,116.

Validation:
- pytest: 2945 passed. The
  native suite under ASan/UBSan: 744 passed, with the 32-per-link build.
- All three service lanes built on the VM without warnings.
- On devices, with the build that held 32 addresses per link (the committed
  build differs only in that cap and was not deployed, at the maintainer's
  request):
  - NetBSD 6: the plan validated with the 10 interfaces it had before
    (bridge0 LAN, the rest isolated); discovery registered `_smb` and
    `_adisk` on bridge0; the heartbeat payload lists `lo0` and `bridge0`.
  - NetBSD 4 LE: the same with its 10 interfaces (mgi0/mgi1, bridge0 LAN).
  - Doctor passed on both. Both boot heartbeats reached the server without
    `plan_error`, and it stored `lo0` and `bridge0` as their links, as before.
- Not run, at the maintainer's request: the Samba device suites (drivers,
  dir, growth, durable, links, smbtorture). They exercise smbd, which this
  change does not touch. NetBSD 4 BE has no LAN device. The truncation paths
  ran on the host only.

## Reboots through ACPd, issue #177 (2026-10-01)

Every TimeCapsuleSMB reboot (deploy, uninstall, fsck, flash restore, disable
SSH) now runs `/usr/bin/acp acRB=00000000` over SSH instead of
`sync; shutdown -r now || reboot`. Apple's ACPd saves `ACPData.bin` and then
runs shutdown itself. A direct shutdown made ACPd rewrite that file after
SIGTERM, inside shutdown's few seconds before SIGKILL, and an interrupted
write makes ACPd erase Flash on the next boot. The request runs in the
foreground (ACPd answers before it shuts down); a failed or lost request is
observed with the existing SSH down/up wait, never retried and never followed
by an OS reboot. fsck no longer reboots from its remote script: the host sends
the same request once the script reports a status line. Uninstall no longer
needs the network ACP password.

Firmware facts (Apple images decrypted with the flash command's own code,
root filesystems read from the kernel's md image): `/usr/bin/acp` is a hard
link to `/sbin/ACPd` in NetBSD 4 BE (syAP 106) and LE (syAP 116) firmware
7.5.2 to 7.8.1 and on the NetBSD 6 device; `/usr/sbin/acp` exists in none, so
every on-device call now uses `DEVICE_ACP_PATH`. `acRB` exists in all of them.

Validation:
- pytest: 2940 passed.
- NetBSD 4 LE (192.168.1.10), with ACPData's header and payload Adler-32
  checksums and kernel boot time read before and after each step: deploy,
  fsck (exit 0), uninstall and a second deploy each printed "ACP reboot
  requested.", went down, came back on a new boot, and left ACPData valid
  (84 properties, 10,852 bytes). Doctor passed after the first and the last
  deploy. The deploy reboot ran from deploy's stopped-services state
  (manager, its diskd, smbd, wcifsfs, wcifsnd stopped).
- NetBSD 6 (192.168.1.218), the same checks: deploy and fsck (exit 0) each
  requested the ACP reboot and came back on a new boot with ACPData valid
  (83 properties before; 85 after, from properties ACPd refreshed itself).
  Doctor passed on both devices afterwards.
- Not run: flash restore (it rewrites a firmware bank; the request path is
  the same `remote_request_reboot` and is covered by the CLI tests) and
  disable SSH (its `acp remove dbug` step stays out of bounds for agents).
  NetBSD 4 BE has no LAN device: its firmware ships `/usr/bin/acp` and
  `acRB`, but no reboot ran on it. The Samba device suites were skipped:
  this change does not touch smbd.

## Buffer-cache stall recovery, kern/60584 (2026-10-01)

Telemetry install `3f47e07a` (TimeCapsule6,116, NetBSD 4 LE) went silent after
4.5-7.5 days of uptime five times between July and October (v2.2.7 to v3.1.1
runtimes). Each time, SSH sessions failed with `sh: Cannot vfork`, sshd closed
new connections (`kex_exchange_identification`) and smbd reset them, until a
power cycle. That is `fork()` failing at `kern.maxproc`. PR 353 found Samba
workers sleeping in `needbuf` during Time Machine on NetBSD 6: NetBSD
kern/60584, where `getnewbuf()` sleeps forever once `buf_lotsfree()` refuses a
fresh buffer and none can be recycled. Both kernels' `vfs_bio.c` (netbsd-4
1.167.2.1, netbsd-6) have the same `buf_lotsfree()`, sleep without a timeout,
and wake sleepers on every buffer release (`wakeup(&needbuffer)` on NetBSD 4,
`cv_signal(&needbuffer_cv)` on NetBSD 6). The manager now raises
`vm.bufmem_lowater` while processes wait, wakes them, and restores Apple's value
(build/native/README.md).

Device facts (NetBSD 6 192.168.1.218 and NetBSD 4 LE 192.168.1.10, 256 MB):
- `kern.maxproc` 84 on both; 35 processes at idle on NetBSD 6, 54 on NetBSD 4
  (its kernel threads are separate processes).
- `vm.bufcache` 15: hiwater 40263680 / 40243200, lowater 5032960 / 5030400,
  exactly `hiwater >> 3`.
- Raising `vm.bufmem_lowater` to `hiwater - 16` works at `securelevel=1`;
  `hiwater - 15` gives `EINVAL`; restoring works. NetBSD 4 exports the values
  as 64-bit quads, NetBSD 6 as 32-bit longs.
- `sysctl(KERN_PROC2)` from a static binary returns every process's wait
  message, truncated to 8 bytes. On NetBSD 6, kernel threads (pagedaemon)
  belong to process 0 and are not listed.
- Block reads of unmounted `dk0`/`dk1` go through the buffer cache; opening
  mounted `dk2` gives `EBUSY` on NetBSD 6 (one open per block device) but works
  on NetBSD 4. Flash block devices are `Device not configured`. Telemetry shows
  disks whose data partition is `dk0`, so the wake reads `/dev` on the FFS RAM
  root instead.
- `sysctlbyname()` linked libc's MIB tree learner and `qsort`: 6.5 KB more in
  the NetBSD 4 image. One `CTL_QUERY` of the `vm` node replaced it.

Validation:
- pytest: 2987 passed (native: 762). The unit test covers the decisions (brief waits,
  raise, wake, restore after quiet, stuck counted from before or after the
  raise, retry after stuck, capped then raise once the cache shrinks, refused
  raise, restore of a mark left raised, full table) and the fixture. The
  manager tests run the real loop: in-process wakes, recovery while every
  fork fails (`TC_TEST_FORK_FAIL`, test builds only) with no process started,
  reports only after an episode and merged when they could not start, a hung
  report killed at stop, a stall during stop, refused writes and unreadable
  state logged once. gcc 13 `-Werror` builds of the changed files.
- VM builds of all three service lanes from the final source: NetBSD 6
  375500 bytes (+5496), NetBSD 4 LE 332632 (+5924), NetBSD 4 BE 332052
  (+5936); no warnings; fault-ahead check passed.
- Device driver built from `service/bufstall.c` with
  `TC_BUFSTALL_EXTRA_WMESG` set to the `sleep` wait (`nanoslee` on NetBSD 4,
  `nanoslp` on NetBSD 6): on both devices it listed the running `sleep` (and
  cron, svscan and telemetry, which also sleep there), raised the low-water
  mark to `hiwater - 16`, got `EINVAL` at `hiwater - 15`, ran the 128-pass
  wake (40 ms on NetBSD 6, 94 ms on NetBSD 4), and restored `hiwater >> 3`;
  `sysctl` agreed after each step.
- NetBSD 6: deploy and doctor passed with the first version and with the
  in-process version before the last review fixes (pending-report merge,
  killable report, wake-failure log); neither manager logged a buffer stall.
  The final build was not deployed.
- Not run: the NetBSD 4 deploy (skipped at the maintainer's request), a real
  stall (none has been reproduced; 5 s / 10 s / 60 s and 128 passes are first
  estimates), and the Samba device suites (smbd is unchanged).

## Buffer-stall report delivery and signed-job isolation (2026-10-01)

The manager now uses `telemetry --report`: the existing payload and POST path,
without workspace locking, cleanup, debug downloads or signed-job execution.
A report can run while a signed job owns the workspace, and its timeout or
shutdown cannot interrupt that job. Exit zero acknowledges a successful POST;
opt-out, cancellation and failed transport return nonzero. Normal `--once` and
daemon cycles retain their signed-job behavior.

The manager keeps the in-flight outcome and longest wait separately from new
pending episodes. Success discards only that snapshot and starts the one-hour
cooldown. Failure merges it back and retries after 60 seconds; failed spawning
uses the same delay. The 180-second report deadline remains bounded. Pending
reports are process-local, and an ambiguous HTTP failure can duplicate a report
on retry; no persistent delivery state was added.

Validation:
- Focused manager/telemetry integration tests: 200 passed. Tests exercise a
  report while a real signed job holds the lock, report cancellation without
  disturbing that job or its files, signed DEBUG replies without execution,
  opt-out, HTTP failures, failed starts, timeout retry, delivery cooldown, and
  new episodes during successful and failed in-flight reports.
- `make test-parallel`: 3097 passed, including native compile checks. Ruff
  and `git diff --check` passed.
- All three service lanes rebuilt on the VM with no compiler warnings or
  errors; fault-ahead and the NetBSD 6 fork-repair checks passed. Stripped
  sizes: NetBSD 6 375916 bytes (+416), NetBSD 4 LE 333032 (+400), NetBSD 4 BE
  332452 (+400). Installed those images and updated their manifest hashes.
- Ubuntu 24.04 amd64 as a non-root user: host regression drivers passed with
  ASan/UBSan; the complete service compiled with GCC 13 and `-Werror`.
- Clean Samba builds with regression drivers passed on all three VM lanes;
  smbd and migrator hashes match the previously shipped artifacts.
  Stripped smbd/migrator sizes: 6 10240592/2157304 bytes; 4 LE
  10260104/2171156; 4 BE 10258992/2170716.
- Both device deployments and post-reboot runtime verification passed;
  doctor passed on NetBSD 6 (65 s) and NetBSD 4 LE (69 s). At the maintainer's
  request, the broad Samba device suites were cancelled. A subsequent request
  added only the NetBSD 6 quick smbtorture set and a post-test doctor check.
  All 24 quick suites completed within the 20-minute budget: 73 passed,
  52 known failures, 2 skipped; no new failures or newly passing known failures.
  No spinning smbd children remained, and the post-test doctor passed. Total
  elapsed time was 554 seconds (9 min 14 s). All resource locks were released.
  Logs are under `plan/report-only-20261001/`.
- Telemetry server checked over SSH: running-container heartbeat handler,
  schema, ingest and debug-response code match the server checkout. The
  existing schema accepts the report reason and stores the heartbeat before
  returning optional debug instructions; no server change is required.
  Its focused heartbeat/debug tests passed (41), without live ingest tests.

### Cross-process test clock on macOS Python 3.9

The first CI run exposed four timing assertion failures on macOS Python 3.9:
the fake children's `time.monotonic()` timestamps have separate process-local
origins there. Comparing a new child's start with an older child's completion
therefore produced negative elapsed times. Event timestamps now use
`time.clock_gettime(time.CLOCK_MONOTONIC)`, the shared system clock already used
by the native manager. Runtime code and shipped binaries are unchanged.

- Reproduced all four failures locally on macOS Python 3.9.25 with ASan/UBSan.
- After the fix, all four affected cases passed with ASan/UBSan and three
  pytest workers on both Python 3.9.25 and Python 3.14.
- Ruff and `git diff --check` passed. Logs are in
  `plan/report-only-20261001/ci/clock-py*-{before,after}.log`.

### Buffer-stall log completion synchronization

Ubuntu CI exposed a test ordering race: a low-water write is visible before
the manager finishes its wake and emits the recovery log. The recovery test
and sibling restore/stuck tests now wait for the expected log independently
of the write. They retain the exact message/value checks and existing bounded
waits; runtime code, recovery ordering and shipped binaries are unchanged.

- Reproduced the original assertion failure by compiling a temporary host
  fixture with a 500 ms delay after publishing a low-water write.
- With that delay and ASan/UBSan, all four affected test cases passed.
- The complete manager module passed with ASan/UBSan and three pytest workers:
  79 passed in 174.21 s. Ruff and `git diff --check` passed.
- Reproduction and verification logs are under
  `plan/report-only-20261001/ci/log-race/`.

### 2026-10-02: v3.2.0 release check, excluding smbtorture

Validated `a636aa7b`, then finished against replayed main `1fdb45c0`. The
replay changed only the manager test synchronization above and these notes;
production sources and shipped artifacts were identical. The four updated
manager cases also passed with ASan/UBSan and three pytest workers.

Synced source inputs with checksummed rsync and rebuilt service, rsync,
Samba, the migrator and all regression drivers using the existing VM SDKs.
The three Samba lanes were clean builds. All 12 shipped binaries matched
the original manifest SHA-256 values exactly; no binary or manifest changed.
Stripped sizes in bytes:

| Artifact | NetBSD 6 | NetBSD 4 LE | NetBSD 4 BE |
| --- | ---: | ---: | ---: |
| smbd | 10240592 | 10260104 | 10258992 |
| migrator | 2157304 | 2171156 | 2170716 |
| service | 375916 | 333032 | 332452 |
| rsync | 1037780 | 898904 | 893012 |

Host checks passed: `make test-parallel` (3097 tests), native ASan/UBSan
(906 tests), Swift (578 tests), Ruff, and native macOS app packaging with
full validation. The packaged app reported 3.2.0 / 30200. Ubuntu 24.04
amd64, as a non-root user, passed the GCC 13 service compile and the full
Samba host regression plan with ASan/UBSan.

Deployed NetBSD 6, then NetBSD 4 LE. Both completed the full device plan
without smbtorture, plus deletion/stream and supervision checks:

| Check | Passed on each device |
| --- | ---: |
| Native regression drivers | 89 invocations across 12 drivers |
| Directory operations, including macOS sparsebundle | 106 |
| File growth, including 600 MiB sparsebundle readback | 23 |
| Selected growth cases with AIO enabled | 11 |
| Durable reconnect and timestamps | 29 |
| Native symlinks and Windows/Linux link formats | 85 |
| First-attempt deletion and stream roundtrip/shrink | 121 |
| Supervision, reload and native NBNS recovery | 15 |
| Final Doctor | 86 |

The first NetBSD 4 Doctor caught a native NBNS registration timeout during
startup. Discovery replaced its child after Apple's 100-second timeout and
registered successfully eight seconds later, without intervention. Subsequent
Doctors passed. Keep this initial failure distinct from the final passes.

The manual supervision helper exposed two test defects: SSH output carried
CRLF, which its configuration regexes did not accept, and a restarted smbd
appeared in `ps` before TCP 445 was listening. Normalize command output once
and use the existing bounded port wait before creating a session. Checks
using the captured configuration covered LF/CRLF and both listener outcomes;
the complete supervision sequence then passed on both devices, including
manager TERM/KILL and repeated native NBNS child loss while Bonjour and an
SMB handle stayed usable. Apple's daemons retained their PIDs.

Final Doctor skipped only disabled rsync and the absent USB printer.
Smbtorture was explicitly excluded; BE hardware was not part of this run.
No test scratch directories or driver RAM mounts remained. NetBSD 6's old
panic file was unchanged; NetBSD 4 had no panic file. All locks were released.
Logs, rebuilt outputs, original manifest comparisons and the runnable helper
check are under `~/tmp/tc-release-v3.2.0-20261001-225042/`.

### 2026-10-03: mounted internal disk ATA timers (issue #360)

The native manager passed `/dev/wd0` to Apple's `atactl`, which resolves to
the block device and fails with `Device busy` while the HFS volume is mounted.
Pass the inventory's bare `wdN` name so `atactl` resolves the raw disk instead.
The same argument serves both idle and standby commands.

The real-manager fixture now makes the fake ATA command fail on the block
path. Regression cases cover both commands, `wd0` and `wd1`, a 100-second
timer and explicit zero values. The three focused tests failed before the
fix and passed afterwards, including with ASan/UBSan. The existing case
still checks startup, preference changes and skipping healthy rechecks.

`make test-parallel` passed 3,258 tests; its one artifact-check failure ran
while the rebuilt binaries were being copied, before the manifest update.
After the update, all six artifact tests passed. Ruff and `git diff --check`
passed. The Ubuntu 24.04 amd64 GCC 13 native service compile passed with
`-Werror`; the unrelated Samba host regression was stopped when the user
narrowed validation to quick, relevant NetBSD 6 checks.

Rebuilt all three service variants from checksummed VM inputs. Data-fault-ahead
verification passed on every lane, and fork-repair verification passed on
NetBSD 6. Stripped sizes: NetBSD 6 375,868 bytes; NetBSD 4 LE 333,008 bytes;
NetBSD 4 BE 332,428 bytes. Updated the three manifest SHA-256 hashes.

Deployed the corrected service on NetBSD 6. Its startup log has no ATA command
failure. On the mounted internal disk, `/sbin/atactl /dev/wd0 checkpower`
reproduced `Device busy` (exit 1), while `wd0 checkpower` and `wd0 setidle 300`
both returned 0. The latter reapplied the configured timer. Timed spindown
was not measured. Full device suites were cancelled at the user's request;
the NetBSD 4 deployment already in progress was allowed to finish safely,
without starting its test suites.

Logs are under `~/tmp/tc-issue360-20261003-044035/`.

## Every reboot proven by the ACP uptime, one shared wait (2026-10-03)

Deploy, uninstall, fsck, flash restore, configure's SSH enable and set-ssh
enable/disable now reboot through one function, `services.reboot.reboot_device`.
It reads the device uptime (ACP property `syUT`, seconds since the kernel
started) over network ACP, waits one second, sends one `acRB` request over
network ACP, and polls `syUT` every 5 s. A reading below the starting uptime
plus the elapsed time, less 2 s for whole-second counts and clock drift, can
only come from a new boot. Then port 22 must open, or, after disabling SSH,
stay closed on two checks 5 s apart from 60 s of uptime (one lost connection
attempt also reads as closed). The old proof (SSH stops answering,
then answers) took a dropped SSH connection for a reboot, failed a reboot fast
enough to fall between two probes, and for set-ssh disable took ACPd still
answering during shutdown for a finished reboot. This supersedes the
2026-10-01 entry's request over SSH: the network ACP password is the device
password, so nothing needed the SSH route any more. Start limits are 90 s
(fsck 120 s); the up limits are unchanged (240 s, fsck 420 s). Reads before
the request fail before anything is sent, so an unreachable ACP or a wrong
password never leaves a half-requested reboot. fsck and uninstall still accept
key-only SSH with no password, but only when they will not reboot: a run that
reboots asks for the password (or fails under `--no-input`) before it touches
the disk. `TC_SSH_OPTS` proxy options are rejected, since ACP needs a direct
path.

Measured `syUT` first (read-only, both LAN devices): local `acp -q syUT`
prints hex, network ACP returns a uint32 in about 10 ms, and `kern.boottime`
agrees within 2 s. ACPd starts 4-5 s after the kernel and sshd 7 s (NetBSD 6)
or 10 s (NetBSD 4 LE) after it.

Validation:
- pytest: 3,290 passed. `tests/test_reboot.py` drives the real loop against a
  simulated device and clock (fast reboots between reads, lost requests,
  transient ACP failures, slow reads, a slower device clock, the Mac sleeping
  through the reboot, SSH that never opens or reopens after disabling) plus
  500 seeded random timelines; no timeline reports a reboot that did not
  happen. Mutating the uptime test, the 60 s wait or the start limit fails it.
  A conftest guard fails any test that opens a real ACP connection.
- `swift test`: 657 passed, including the shared reboot stage titles for every
  rebooting operation.
- No reboot requested, both devices in parallel: the wait read the live uptime
  and ended `did_not_go_down` after 92.6 s on each.
- NetBSD 6 (192.168.1.218): deploy (ACP stopped answering 11 s after the
  request, new boot seen at 63 s with uptime 17 s, SSH already open), doctor
  passed; fsck with reboot (fsck_hfs exit 0; new boot seen at 77 s under the
  120/420 s limits), doctor passed.
- NetBSD 4 LE (192.168.1.10): deploy (down at 11 s, new boot seen at 111 s
  with uptime 22 s, activation completed), doctor passed; uninstall (new boot
  seen at 111 s, post-uninstall verification passed right after the wait);
  redeploy (u0 384 s, new boot seen at 79 s), doctor passed.
- Not run: flash restore (rewrites a firmware bank), set-ssh enable/disable
  and configure's enable (they need `acp remove dbug`, which agents do not
  run), the macOS app's own deploy, and NetBSD 4 BE (no LAN device; `syUT` is
  unverified there, but a missing property fails before any request). The
  Samba device suites were skipped: smbd is unchanged.

## No fixed settle sleeps after a reboot or activation (2026-10-03)

Deploy slept 20 s after SSH returned before probing the runtime, and activation
slept 20 s after starting it. The shared reboot wait now returns only once SSH
is open on the new boot, and runtime verification polls for up to 240 s
(200 s after activation), so both sleeps went, with their stages, keyed log
messages and strings.

Validation:
- pytest: 3,290 passed; `swift test`: 657 passed.
- NetBSD 6: deploy (new boot seen at 64 s, runtime ready without the sleep),
  doctor passed. Readiness now passes before NBNS finishes registering, so the
  deploy printed the existing note "discovery native NBNS is not ready"; the
  doctor that followed resolved the NBNS name. The device's discovery log
  shows why: right after boot ACPd's own `wcifsnd` held UDP 137/138, so
  discovery's child exited 0 four times (retries after 2, 4, 8 and 16 s,
  title `nbns=waiting` in between) until the manager stopped ACPd's copy; the
  name registered at 21:04:53, 21 s after deploy finished. Deploy now reports
  `waiting` with a validated plan like `starting` (a quiet skip, since deploy
  does not wait for NBNS); doctor still treats it as not ready and retries.
- NetBSD 4 LE: deploy passed (new boot seen at 114 s, activation verified).
  Doctor then failed only its Bonjour checks: the device's AirPort name
  (`acp -q syNm`) is empty, so its `_smb`/`_airport` instances have blank
  names while the hostname is `base-station-edffbf`. Telemetry shows the same
  failure from another session's doctor at 20:54, before this deploy and while
  no lock was held here, so the device state predates this change; it was left
  alone.

## One shared SSH connection per device (2026-10-04)

A traced NetBSD 6 deploy took 293 s, and 205 s of its 212 s before the reboot
were 179 separate SSH logins of about 1 s each (key exchange on the device's
CPU, then password authentication). The work itself was under 20 s, plus three
10 s flush sleeps. Commands now share one authenticated connection per device:
the first command's ssh becomes the background master (`ControlMaster=auto`,
`ControlPersist=180`, socket in a private `/tmp/tcsmb-ssh-*` directory named by
a hash of host, options and password), and the reboot request, process exit or
ServerAlive (15 s x 3) ends it. A shared session never authenticates, so its
log has no `Authenticated to` line; the transport logs at DEBUG1 and counts
`mux_client_request_session: master session id:` as authenticated, which keeps
a remote exit of 255, or 5-7 under sshpass, the command's own status. Over a
live master `run_ssh` uses pipes with `BatchMode=yes` instead of pexpect's PTY
(287 ms a command against 41 ms: ptyprocess closes every descriptor up to the
fd limit before exec, and its close sleeps 100 ms); if the master ended and no
login ran the command, the password login runs it once. The doctor's SMB
tunnel keeps its own connection.

Validation:
- pytest: 3,512 passed. One native test failed once under load in the full
  run and passes alone; under a repeated `-n 12 tests/native` loop a
  different native test (`test_acp_capture`) failed once. Neither touches the
  SSH transport.
- NetBSD 6: deploy 293 s -> 123 s (179 commands over the master, median
  60 ms; two password logins, before and after the reboot), doctor 34 s ->
  15-16 s (43 s right after the deploy's reboot).
- NetBSD 4 LE (OpenSSH 4.4): deploy passed in 332 s with one password login;
  doctor 39 s -> 18 s. Its remaining time is the device's flash: removing
  `/mnt/Flash/service` took 27 s and writing the 333 KB service there 118 s,
  while the 10 MB smbd reached the HFS disk in 4.4 s.

## Flash writes only for changed files, in large blocks (2026-10-04)

NetBSD 4 spent 146 s of a 332 s deploy on one file: removing
`/mnt/Flash/service` took 27 s and writing the 333 KB replacement 118 s.
Apple's fstab mounts `/mnt/Flash` (FFS on the 1 MB `flash2a`) `synchronous`,
and its flash driver skips sectors whose bytes stay the same. Measured on the
NetBSD 4 LE device with the same 333 KB binary: a new file took 124 s through
`cat` from the SSH pipe and 98-99 s as one large write (`dd obs=512k`, or `cp`
from `/mnt/Memory`); overwriting a file in place took 2.5 s with identical
bytes and 134 s with different ones; deleting it took 28 s; reading it back
0.4 s. NetBSD 6 wrote the 376 KB service in 1.9 s.

Uploads now run `dd of=DEST ibs=65536 obs=1048576 && ls -l DEST` and check
the printed size, replacing `cat >` and the separate, retried size-check SSH
call left from the scp transport. Before removing old software, deploy reads
back each upload bound for `/mnt/Flash` and leaves a file that already holds
its new bytes in place: not removed, not written, not counted against flash
space. A file that cannot be read is written again. Each removal list is now
one `rm -rf` per volume (flash plus RAM, then the payload behind its mount
guard, and one per other detected payload), and uninstall's 19 removals are
one; a removal gets 300 s, as the flush does, rather than the 120 s each path
had alone. A kept config still has its mode set to 600. A changed service still costs about 100 s on NetBSD 4; in-place
overwrite of changed bytes was slower (134 s) and would fail with ETXTBSY
against a still-running service, and the service cannot move to the HDD
because the manager it runs starts the diskd that mounts it.

Validation:
- pytest: 3,516 passed. One test failed once under the load of two deploys
  (`test_bonjour_integration` SIGINT helper, 3 s timeout) and passes alone;
  the discovery code is unchanged here. The filesystem install harness now
  runs the production upload command in a local shell.
- NetBSD 6: deploy passed in 128 s with the service kept (pytest ran in
  parallel on the Mac); doctor passed.
- NetBSD 4 LE: deploy 332 s -> 173 s with the service kept: removing old
  software 35.7 s -> 1.1 s, uploads 126.6 s -> 4.7 s (10 MB smbd 3.9 s
  through `dd`). Doctor passed. The rest is the reboot (102 s), two 11 s
  flushes and stopping the runtime.

## Review fixes: flash symlinks, host build key, ACP poll test (2026-10-04)

- A `/mnt/Flash` destination that is a symlink is now written again even when
  its target holds the new bytes. The keep check ran `cat`, which follows the
  link, so a redeploy left `/mnt/Flash/service` pointing at a copy on the data
  disk, which Apple can unmount. The check is now `test ! -h DEST && cat DEST`;
  `rm -rf` then removes the link, not its target. No release ever put a symlink
  on `/mnt/Flash` (the old `RemoteSymlink`s were `/root/tc-netbsd4*`), so this
  only restores convergence for a hand-made state. `tcapsulesmb.conf` is the
  exception: it is not on the removal list, so a symlinked config is written
  through the link and its upload size check (`ls -l` of the link) fails on
  every run, where the previous check kept it when the bytes matched. Accepted:
  nothing creates that symlink, and the config stays off the removal list.
- The host regression's tree-cache key (`host_build_key`) now covers `CC`,
  `CFLAGS`, `CPPFLAGS`, `LDFLAGS` and `LINKFLAGS` from the environment handed
  to configure and waf (the sanitizer flags are in it; waflib's `c_config`
  reads all four flag variables), and fingerprints `$CC --version`, or
  `gcc --version` without `CC`: Samba's waf (`compiler_c`) tries gcc first on
  Linux. It hashed a literal `cc` before, and a run with another compiler or
  flags reused the previous run's binaries.
- `acp_collect_deadline_ms` read the clock inside, so the `closed` reap test
  subtracted a second clock reading and failed whenever the scheduler paused
  it between the two (a 10 ms pause gave `soon=-6`). The rule moved to
  `acp_collect_deadline_at(c, now)`, which the old function calls with the
  monotonic clock. The test reads it at the close, 99 ms and 100 ms after it,
  and 50 ms before the key's timeout, and expects exactly 5, 5, 100 and 50.
  It also brackets one real-clock call between two clock readings 150 ms
  after the close and checks the result is 100 ms past a time inside them.

Validation:
- Service rebuilt for all three lanes (fault-ahead and fork-repair checks
  passed); each stripped binary grew 24 bytes: NetBSD 6 376,252, NetBSD 4 LE
  333,572, NetBSD 4 BE 332,992. Manifest hashes updated. No deploy: the only
  service change is the split function, with identical behavior.
- New tests: a flash symlink to identical bytes is replaced by a regular file
  (fails without the fix); the build key changes with each compiler setting,
  with the compiler behind an unchanged `CC`, and with `gcc` (not `cc`) when
  `CC` is unset, but not with `ASAN_OPTIONS`.

## fsck restarts file sharing on NetBSD 4, from a boot-hook check before the reboot (2026-10-05)

Telemetry for v3.2.0-2 showed a TimeCapsule6,116 on stock firmware whose
runtime stayed off after `fsck`: its reboot stops file sharing, and stock
NetBSD 4 firmware does not run `/mnt/Flash/rc.local` at boot. Apple's own file
sharing comes back after any reboot. fsck now brings ours back the way deploy
does.

The device probe's first SSH command (`uname`) also reports whether
`/etc/rc.d/LOGIN` runs `/mnt/Flash/rc.local`, the NetBSD 4 firmware autostart
patch. Deploy already probes before anything else, so it learns the boot hook
there and no longer reads `LOGIN` after its reboot. fsck now runs the same
probe before the repair, and checks for an install this version can start.
After the reboot, both call `start_netbsd4_runtime_after_reboot` with the
answer they already have: run `rc.local` unless the boot hook already did,
then wait up to 200 s for the runtime. Nothing is asked of the device after
the reboot, so no failed check there can skip the start: every failure is
reported. If the runtime does not become ready, fsck fails with
`runtime_not_restarted` and the app offers Activate and Checkup. A probe that
fails before the repair stops fsck before it touches the disk. NetBSD 6, a
device with no install, or an install older than v3.1.0 is left alone, and
fsck still succeeds.

- A failed repair still restarts file sharing and keeps its own error first.
  After a reboot it is reported at the `run_fsck` step, not at the reboot or
  SMB startup that followed it.
- `fsck --no-wait` on stock NetBSD 4 says file sharing will stay off until
  Activate.
- `set-ssh` enable, `set-ssh` disable, flash restore and uninstall still leave
  the runtime off after their reboot. SSH is only closed on an installed
  device after `set-ssh` disable, whose reboot already left stock NetBSD 4
  file sharing off.
- Deploy's post-reboot `probe_runtime` step is gone, with its two timeline
  strings in each language.

Validation:
- Full pytest (3,599 passed), ruff, native host checks, and `swift test` (667
  passed). The probe's shell command runs on the host against stand-in
  `LOGIN` files (patched, stock, missing).
- Probe on both devices: NetBSD 4 LE (192.168.1.10) `rc_local_autostart=True`
  (patched firmware), NetBSD 6 (192.168.1.218) `False`. Doctor passed on both.
- NetBSD 4 LE: `fsck --yes`, exit 0 in 1 min 47 s; after the reboot it went
  straight to "NetBSD4 firmware autostart is enabled; waiting for managed
  runtime." and every runtime check passed. Deploy `--yes`, exit 0 in 4 min
  24 s, the same autostart path. Doctor right after the deploy flagged the
  startup grace (services 23 s old); it passed when rerun a minute later.
- NetBSD 6: `fsck --yes --volume dk2`, exit 0 in 12 min 39 s, on the earlier
  revision that asked the device after the reboot; nothing restarts on
  NetBSD 6 in either revision, and doctor passed. Not rerun on this one.
- The `rc.local` start path is covered by unit tests and by deploy: both test
  devices start file sharing by themselves.

## "Activation Needed" from a stopped NetBSD 4 runtime, not from warnings (2026-10-05)

The app showed "Activation Needed" for any checkup warning on an installed
NetBSD 4 device (`f4a77261`, tested against a doctor warning the helper never
emits), and plain Unhealthy when the runtime was actually stopped. Doctor now
tags its missing-runtime FAIL `runtime_not_started`; the app shows that as
"Activation Needed" on NetBSD 4 ("File sharing is not running. Activate
starts it.", ten languages) and a warning checkup as installed, unverified.
CLI `activate` says "NetBSD 4 devices cannot auto-run Samba after a reboot."
only when the device probe found no boot hook.

Validation: full pytest (3,600 passed), ruff, native host checks, `swift
test` (668 passed). No device run: the doctor change adds a result code to an
existing failure.

## Renumbered disk no longer walked under a completed disk's number (2026-10-05)

v3.2.0-2 telemetry (TimeCapsule8,119): the internal disk completed migration
under v3.1.1 while it was dk4. Before the next deploy Apple renumbered the
disks (internal dk2, external dk4). Legacy rows are keyed by (st_dev, st_ino),
and HFS inode numbers repeat across volumes, so the external disk's copy walk
matched the internal disk's dk4 rows to its own files. Its cleanup then saved
coverage that contradicted the internal disk's, and every later deploy failed
with "Inconsistent saved metadata key coverage". The user then uninstalled,
which (we infer from the next deploy finding no legacy database) removed it.

Each completed volume's coverage keys carry the device number its rows were
matched under. `migrate_phase` now skips a volume whose current st_dev is such
a number of another completed volume (`deferred reason=device_renumbered`).
Its rows stay in the database, and it is walked once its number is free again.
Rows a lone source dropped leave the coverage too, so a number whose rows are
all gone does not hold the walk back. Matching a renumbered volume's rows under
its old number would need a stored device map and a migrator change; that is
left out until telemetry shows deferred volumes.

Validation:
- New tests: a disk renumbered to a completed disk's number is not walked while
  that disk's orphaned or kept rows remain, and is walked once the number is
  free (both fail without the fix); a number whose rows were all dropped does
  not defer the walk.
- `tests/test_xattr_migration.py` (89 passed) and ruff on the changed files.
- No device run: the LAN devices hold no legacy database, so deploy skips
  migration there, and renumbering cannot be staged on them.

## MaSt text read as acp prints it (2026-10-05)

Plain `acp MaSt`, which deploy, migration and the storage service read,
prints XML. `acp -A MaSt` prints Apple's own text form, from acp's
PrintFUtils printer (disassembled from the NetBSD 6 7.9.1 and NetBSD 4 7.8.1
acp): `{`, `}`, `[` and `]` alone on their lines, `key=value` entries in
CFDictionary order, data up to 16 bytes as hex, ` |`, the same bytes as text
(0x20-0x7e as themselves, anything else as `^`) and `| (N bytes)`, and a
string value as its raw UTF-8 between quotes with nothing escaped. Doctor's
MaSt probe and the native service's inventory (`build/native/storage/mast.c`)
read that form.

The Python volume and inventory parsers each walked the text with their own
line state machine. The volume parser took a partition's closing brace for
its disk's when the partition had no `deviceName`, so later partitions were
credited to the next disk, and it dropped an unnamed HFS partition that the
XML path names after its device. One decoder now turns the text into the
dictionaries the XML plist gives, following the braces, so both forms go
through the same converters; the text form now also keeps empty disk and
partition objects in the inventory and prefers `size` to `capacity`, as the
XML path always did. Both the Python decoder and the native parser
unescaped quoted strings, and the native one ended a string at its first
quote, so a disk named with a quote or a backslash was misread (the native
parser then rejected the whole MaSt read). In acp's form both now take a
value as written, up to the quote that ends its line; the OpenStep form
still unescapes. The service grew by about 190 bytes.

`acp -A MaSt` read on both LAN devices (NetBSD 6: `AirPort Disk` on the
internal wd0 and a 4 TB external `4TB` on sd0; NetBSD 4 LE: `Data` on sd0,
CRLF lines) gave the same volumes and inventory, sizes included, with the
old and the new Python code, and deploy passed on both.

## Manager and discovery wait through one tc_wait_until (2026-10-05)

`manager.c`'s `wait_until()` repeated `plan_loop_wait()` from
`common/loop.c`. Both loops now call `tc_wait_until()` in
`common/process.c`, which takes a finite deadline, so the manager no longer
includes the plan loop. Discovery, which can have nothing due, passes its
plan poll (`now + TC_PLAN_POLL_MS`) itself, as `plan_loop_wait()` did; the
manager's deadline is always finite (each pass starts from
`now + TC_MANAGER_PASS_MS`). A new `wait_until` case of
`tests/native/unit/test_process.c` covers a timeout, a ready descriptor, a
deadline already past, a signal and a closed descriptor.

The clean lane builds shrank the service from 376,436 to 376,324 bytes on
NetBSD 6, 333,764 to 333,608 on NetBSD 4 LE and 333,184 to 333,028 on
NetBSD 4 BE. The native host tests passed. An earlier build of this change,
with the helper still in `loop.c`, passed the full tier on both LAN devices;
this build passed deploy and doctor on both. On NetBSD 4 LE, doctor run
17 s after the deploy's reboot failed its NBNS query (the name it asked for
was not yet settled) and passed when run again 70 s after boot.


## One xattr-list normalization; configure's interface probes left alone (2026-10-05)

`tc_airport_flistxattr()` and `tc_airport_llistxattr()` repeated the same
handling of the kernel's reply (duplicates dropped, EIO for an unterminated
entry, the size probe and ERANGE answered for the deduplicated list).
`tc_airport_xattr_list_finish()` now does it once; each keeps its own
syscall. The new `list_normalization` case of `tc_native_metadata_test`
covers both calls, including the descriptor call's duplicates and the EIO,
empty-list and errno paths no case reached before.

`_samba4x.sh` no longer clears configure's getifaddrs results or forces the
IFCONF backend: patch 0043 reads smbd's interfaces from routing messages.
Configure finds libc `getifaddrs()` on all three lanes, so Heimdal's krb5
address lookup (`get_addrs.c`), the one remaining caller and used only for
Kerberos client logins a standalone smbd never makes, links libc's
`getifaddrs()`/`freeifaddrs()` instead of libreplace's IFCONF backend; per
0043 neither parses Apple's NetBSD 4 interface list. smbd shrank from
10,240,592 to 10,239,720 bytes on NetBSD 6, 10,260,104 to 10,259,232 on
NetBSD 4 LE and 10,258,992 to 10,258,156 on NetBSD 4 BE. The NetBSD 6
migrator's hash changed with the same size and the same strings in another
order (the committed one came from an incremental build); the NetBSD 4
migrators are byte-identical.

Clean builds of all three lanes, the host regression with sanitizers, and
the full tier on both LAN devices passed (deploy, every driver case, doctor,
dir_device with the Mac case, growth_device with and without aio,
durable_device, links_device). NetBSD 6 ran on its internal `AirPort Disk`
share. smbtorture's full list: 78 passed and 52 known failures on each
device, nothing new either way.

## A helper whose app went away stops where Cancel would have, and reports it (2026-10-05)

v3.2.0-2 telemetry showed a GUI deploy reach `migrate_xattrs_copy` and vanish
with no `deploy_finished`, twice for one user, while the device migrator kept
running; earlier releases left 10-52 started deploys per release without a
result. Nothing stopped the helper when the app quit, and its first write to
the closed pipe raised `BrokenPipeError` from `sink.error()`, before the
finished telemetry. The event sink now treats a broken pipe as the app being
gone: it sends nothing more and points stdout and stderr at `/dev/null`. Every
stage starts with a write, so the helper learns of it at the next stage at the
latest. It then stops where the app's Cancel button would have let the user
stop it: on entering a stage whose policy allows cancelling (or that has no
policy, which the app treats the same). The stage in progress and any later
ones the app offers no Cancel for run to their end, so a Flash write reaches
its flush (`enable_boot`, then `flush_boot_hook` and the reboot request) and
flash writes both banks. A lost copy phase still removes the old software
(`replace_software`) and stops before `check_flash_capacity`; a lost cleanup
phase finishes the installation and requests the reboot, stopping before
`wait_for_reboot_down`, so NetBSD 4 then needs Activate. Long waits check
between polls under the same rule. Telemetry says `cancelled` /
`client_disconnected` with `stopped_before_stage` and
`disconnected_during_stage` (the stage running when the write failed; a stage
event is sent before its stage becomes current).

Validation: full pytest, ruff, `swift test`. `tests/test_helper_disconnect.py`
runs the real helper with a stand-in deploy and a local telemetry server and
closes its stdout during the first stage: during `migrate_xattrs_copy`, the
copy and `replace_software` finish and `check_flash_capacity` never starts;
during `enable_boot`, `flush_boot_hook` still runs. Each time the helper exits
130 with an empty stderr and the server gets the finished event with both
stage names. With the broken-pipe handling reverted the helper exits 120 and
sends nothing.
On NetBSD 6 the real API helper's deploy, with its stdout closed as
`pre_upload_actions` began (a stage the app offers no Cancel for), ran on
through `replace_software`, exited 130 after 22 s with an empty stderr and
posted `client_disconnected` with `stopped_before_stage: check_flash_capacity`
and `disconnected_during_stage: pre_upload_actions`; the runtime was stopped
and the boot hook gone, as that stage leaves them. A deploy right after it
finished and doctor passed.

## Deploy waits for a migration an interrupted deploy left running; uninstall stops it (2026-10-05)

In v3.2.0-2 telemetry one user's deploy reached `migrate_xattrs_copy` and was
lost with the app; the device migrator kept running, and the next two deploys
and an uninstall each failed after the 5 s idle-jobs wait with "migration or
diagnostic work is still active". Deploy now probes for a running migrator
before its inventory (`device.migration_jobs`: `ps` rows plus the migrator's
`--log` size, time and last progress line) and, if one runs, shows
`wait_for_previous_migration` and polls every 5 s while the log grows or the
process uses CPU. It fails with `previous_migration_stalled` only after 360 s
without progress, a minute past the migrator's own 300 s stall guard.
Uninstall removes everything such a job works on, so it now stops it:
`render_stop_idle_jobs` shares the idle-jobs classifier (now keyed by pid)
with the deploy wait, sends SIGTERM each pass and SIGKILL once the attempts
are spent, and fails only if a job survives that.

Validation: full pytest (3,646 passed), ruff, `swift test` (672 passed).
On NetBSD 6, with the branch's migrator left blocked on its request as a
stand-in (`multi copy` reading a pipe that never sends; its 300 s guard ends
it), deploy showed "Waiting for the metadata migration from an earlier
installation to finish...", waited about five minutes, then migrated and
finished; doctor passed. A NetBSD 4 deploy with nothing running added no
wait stage. The wait and stop scripts ran on both devices' `/bin/sh`: the
wait reported the stand-in busy, the stop ended it (and the runtime's
telemetry daemon, which the manager restarted), and the wait then passed.

## Doctor reports a running metadata migration (2026-10-05)

While a migration an interrupted deploy left running was still working,
doctor reported "installed Samba version v2.2.9 is older than current" and
sent the user to Install / Update, which then failed with "migration or
diagnostic work is still active". Doctor now probes for a running migrator
first (`device.migration_jobs`, one `ps` plus the migrator's log) and, if one
runs, stops with a FAIL tagged `metadata_migration_in_progress` that names the
phase and how many files it has checked; the app shows "A metadata migration
is still running on the device. “Install / Update Samba” waits for it to
finish before installing." in ten languages, since deploy waits for such a
migration itself. It does not say whose: a live deploy's
migrator looks the same as one an interrupted deploy left. A failed
probe adds nothing and doctor goes on as before.

Validation: full pytest (3,649 passed), ruff, `swift test` (673 passed).
With the stand-in running, doctor reported it on NetBSD 6 ("(copy phase)")
and NetBSD 4, whose `ps` cannot read the migrator's arguments and shows
`(tc-xattr-hfs-mig)`; there the message leaves the phase out. With nothing
running, doctor reported the outdated version as before.

## Metadata migration progress in the app and the CLI (2026-10-05)

A migration of a large legacy `xattr.tdb` can run 10-20 minutes (`af6becb9`
on v3.2.0-2: 561 s copy, 620 s cleanup) while the app showed only "Migrate
metadata", and v3.2.0-2's interrupted deploys left during such stretches. The
migrator now logs `progress phase= volume= entries= matched= total=` at a
volume's start and end and at most every 5 s between (it reads the clock once
per 256 entries, and a clock stepped backwards counts as time passed). While a
phase runs, deploy reads the log's end every 5 s over a second SSH session
and sends a `progress` event with the file count; the app shows "Files
checked: N" in the stage's row and the install overlay (ten languages),
keeping only the latest of a run of progress events, and the CLI prints a
line at most every 30 s per stage (a new stage prints at once). The app's
workflow stores see a replaced progress event because the event observer also
compares the last event's id, not only the count; before that the overlay
stayed at the first count of each run. The helper writes one event at a time
under a lock, since the poller thread sends events beside the main thread.
The count restarts on each disk, which is what the migrator counts. Deploy's
wait for an earlier migration reports the same count. Only files are shown: `matched` stays below `total` when rows
belong to other volumes or to deleted files, so the record counts stay in the
log for diagnosis.

Clean lane builds on main @ 6547050d changed only the migrators (stripped:
NetBSD 6 2,157,696 bytes, NetBSD 4 LE 2,171,588, NetBSD 4 BE 2,171,148, each
about 400 bytes more); smbd was unchanged.

Validation: full pytest (3,669 passed), ruff, `swift test` (676 passed), the
host regression in Docker with sanitizers (the new `tc_xattr_migrate_test
multi` case checks the start, end, rate-limited and backwards-clock lines
against a fake clock).
Deployed with the rebuilt migrators to NetBSD 6 and NetBSD 4 (neither had
legacy metadata left, so no migration ran); doctor passed on both.
On NetBSD 6, a legacy `xattr.tdb` from a v2.2.9 deploy (60 files given an
attribute over SMB) migrated with progress lines at each volume's start and
end: dk2 `entries=6427 matched=61 total=61`, dk4 112 entries and `matched=0`
(its rows belong to dk2). The walks took under 5 s, so no interval lines and
no CLI progress line. NetBSD 4's `ps` shows no migrator arguments, so there
deploy's wait for an earlier migration cannot find its log and shows no
counters; it still sees progress through the migrator's CPU time.

## Mounted-share xattr repair removed (2026-10-05)

Removed `repair-xattrs` from the CLI, app API, macOS maintenance workflow,
telemetry, translations and current documentation. The April workaround only
cleared the macOS `arch` flag (and optionally broadened POSIX permissions); it
never repaired attribute values. Patch 0018 handles missing legacy TDB rows,
and native HFS operations bypass the TDB entirely through patch 0038 and the
native FinderInfo adapter. Deploy-only metadata migration and every Doctor
check remain unchanged. Historical validation entries above are retained.

Removed tests specific to the deleted feature and kept shared behavior tests
using supported operations. CLI/API tests exercise rejection of the retired
operation. The #304 device suite now uses Python `os.walk` without following
links, preserving client traversal coverage without importing the removed tool.
Code added on main since the first version of this change also used the
feature: the dashboard session and state synchronizer observed the repair
store's errors and credential failures, the planned-maintenance cancellation
test also ran a metadata scan, and the proxy-option config test checked the
repair profile. Those now cover the remaining workflows only.

Validation:
- `make lint`, `make test-parallel` (native host checks, then pytest: 3,598
  passed) and `make test-swift` (677 passed, none skipped).
- Every catalog passed `plutil -lint` and holds the same 1,061 strings and 15
  plural keys; no removed key is still referenced and no string became unused.
- Ruff and `git diff --check` passed.
- No build inputs, native runtime, deploy code or shipped artifacts changed;
  no VM build, device access or deployment was needed. `links_device.py` was
  not run against a device.

## Finished telemetry waits for the started event still being sent (2026-10-05)

CI for `5d980575` (Ubuntu, Python 3.14.7) failed
`test_helper_disconnect.py[migration]`: the helper exited with SIGSEGV
instead of 130. The helper sends an operation's started event from a daemon
thread and its finished event in the foreground, then exits. A started event
still being sent at that point was cut off: a stress run of the same scenario
showed the server a request with headers and no body, and with the started
reply held for 0.5 s the helper's started event never arrived at all. The
interpreter then shut down around that daemon thread, the only other thread
left, which is the likely way the helper crashed. The crash itself did not
reproduce in about 2,800 runs of the scenario (official 3.14.7 and 3.14.8
images, and GitHub's own 3.14.7 build in `ubuntu:24.04`, with and without CPU
load) or in three passes of CI's full `pytest -n 4` on GitHub's build.

`TelemetryClient` now keeps its background send threads, and a synchronous
send first waits for those still running, sharing one limit
(`PENDING_SEND_WAIT_SECONDS`, the 20 s one send's two attempts can take).
Every operation's synchronous send is its finished event, so the CLI and the
app helper no longer exit with a send in progress, and started now always
reaches the server before finished. A started send that is still running
when finished goes out is one to an unresponsive server, and finished costs
the same 20 s there; offline sends fail at once.

Validation:
- New tests against a local server that holds started events: a finished
  send waits for one still in flight and arrives after it; with nothing in
  flight it does not wait; with three stuck it waits one shared limit, not
  one each, and still sends. `test_helper_disconnect.py` holds the started
  reply 0.5 s and requires started before finished. Without the fix the
  in-flight and helper tests fail (the helper's started event is lost), and
  a per-send limit fails the shared-limit test.
- `make lint`, `make test-parallel` (3,601 passed) and `make test-swift` (677
  passed). The new tests passed 12 times in a row with every CPU busy.
