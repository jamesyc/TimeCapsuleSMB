# Patched Samba regression tests

These tests compile the actual Samba 4.25.0rc2 sources after applying the repository's
ordered patch series. The AIO, durable and stream drivers include the production C files,
following Samba's existing VFS unit-test pattern. They use real bundled talloc,
tevent, worker processes and DB/NDR code. Only I/O failures, process liveness and
waiting are controlled. The existing pthreadpool lifecycle test runs alongside
them. Test executables are temporary and are removed after device validation.

The `Patched Samba regression tests` CI job runs on Linux with pthread
disabled and AddressSanitizer/UndefinedBehaviorSanitizer enabled. To reproduce:

```sh
python3 -m tests.samba.run host --work /tmp/my-new-samba-test-directory --jobs 2 --sanitizers
```

The work directory must not exist. Dependencies are listed in the CI job. Global
Samba allocations are retained at process exit, so leak detection is disabled;
address and undefined-behavior checking remain enabled.

For NetBSD release validation, use the existing VM/toolchains and the matching
test device credentials with the normal lane wrapper:

```sh
SAMBA4X_CROSS_EXEC_REMOTE_DIR=/Volumes/dk2 \
    SAMBA4X_RUN_REGRESSION_TESTS=1 ./build/samba4x.sh
```

Use `samba4xoldle.sh` or `samba4xoldbe.sh` for the other lanes. The wrapper builds
static tests, executes all cases using `samba4-cross-exec.sh`, and stops before
staging `smbd` if any test fails or is missing. Ordinary offline builds remain
compile-only; run this validation before copying rebuilt release artifacts.
`SAMBA4X_BUILD_REGRESSION_TESTS=1` also compiles and strips the test executables
offline, but does not claim a device-validation pass.

Use the device's actual mounted HFS volume in that path; it is not always `dk2`.
The native-metadata fixture is too large for the appliance's root/RAM scratch
space. The runner refuses other locations, and the cross-exec helper verifies
that `/Volumes/...` is a distinct mounted filesystem before every upload and
removes each temporary executable afterward.

On the NetBSD 4 appliance, run the drivers from RAM with their working
directory on the HFS scratch volume. A disk-backed driver was observed
aborting in talloc before loading its first configuration; the identical
image passed every case from RAM, matching production Samba's placement.
`tests.samba.check`'s driver step does this on both devices: it mounts a
12 MiB RAM disk of its own at `/mnt/TcTests` (tmpfs on NetBSD 6, mfs on
NetBSD 4, as `boot.sh` mounts `/mnt/Locks`), copies one driver at a time
there, and unmounts it afterwards; `/mnt/Memory` has too little room for the
largest drivers (9.7 MB). On NetBSD 4 it also sets `TC_MIGRATE_SCRATCH` to
that RAM disk, because `tc_xattr_migrate_test` makes its off-HFS scratch under
`/tmp`, which there is a nearly full 10 MB RAM disk. Disable core dumps for
manual runs so Apple's default `/tmp/%n.core` does not exhaust that root RAM
disk.

The cases cover talloc isolation through the real AIO fork path, preservation of
worker errors, successful and failed synchronous fallback, worker limits and
share isolation, FIFO saturation, cancellation, worker failure recovery and idle
cleanup. Durable tests cover live-to-disconnected transition, bounded retry
exhaustion, unlocked waiting and identity/ownership rejection. They do not assert
the literal retry limit of 34.

Storage reload cases compile the production connection code and the unchanged
parent/worker callback bodies from the source being built. They check revoked
versus closed descriptors, sentinels, fake/closing/non-disk handles, volume UUID
and root replacement, configured export narrowing/widening (including retained
renamed shares), failed reloads, and asynchronous tree closure with AIO
pending while another tree stays usable. Apple can reuse the same disk path,
device number and inode after a cable bump; retained descriptor validity is the
additional signal. Error injection covers this decision, while physical USB
detach/reconnect and client durable reconnect remain device integration checks.

The sanitizer cases exposed two bugs fixed by patches 0033 and 0031: a
zero-length descriptor array on the worker shutdown message, and a cancelled
request's socket watcher surviving until after a replacement reused its fd.
The `read` and `cancel_active` cases cover these paths; sanitizer exit code 86
is deliberately distinct from the workers' expected shutdown codes 1 and 2.

The stream cases use the real `vfs_streams_xattr.c` with a controlled xattr
backend. ENOATTR is deliberately distinct from ENODATA even on Linux. They
cover root/nested deletion, missing and populated extents, read/shrink behavior,
missing primary streams, path lookup failures and real I/O errors. The charset
case checks const-preserving return types and single argument evaluation on
both the SDK compiler fallback and modern compilers.

The native-HFS cases include the real `vfs_xattr_tdb.c` and `vfs_fruit.c` with
controlled TDB, private-syscall, and lower-VFS backends. They cover the NetBSD
4/6 syscall return difference, native Apple-xattr stream translation, special
FinderInfo/resource filtering, non-HFS TDB fallback, FinderInfo synthesis,
native and async xattr reads, resource create/truncate open paths and flags,
stream stats, AAPL metadata, empty forks, directory rejection, and
synchronous/asynchronous dispatch decisions.

The migrator cases include the production one-shot parser. They cover valid,
ordinary, corrupt, duplicate, and unsupported AppleDouble records; embedded
FinderInfo and xattrs; oversized and missing native attributes; 1 MiB streamed
resource forks; copy/cleanup separation; resource-copy restart markers;
byte-for-byte verification; sidecar retention/deletion; and the intentionally
blank resource-fork payload. A real temporary `xattr.tdb` case migrates
FinderInfo under both public metadata settings, canonical Apple xattrs,
ordinary ACL data, and a fragmented Windows stream, then verifies TDB deletion
and detached-volume orphan retention through the program entry point. Additional
cases cover per-file TDB retirement, failed transaction commits, subsequent deploys
with a previously absent volume, prevention of stale-value replay, directory-read
errors, and ordinary directories whose names begin with `._`. The `orphans` case
(v3.1.0) covers the proven-orphan versus unresolved split of unmatched rows,
quarantine of an all-orphan database to the first free `xattr.tdb.orphaned.N`
slot, retention when an unresolved row remains, no quarantine after a failed
walk, the `fingerprint` mode, and the filesystem-boundary skip. A detached disk
is modelled with a foreign device number in the row key; the unit test has one
device, so a same-device row for a missing inode is a proven orphan, which is
also why the test's `ENOATTR` override applies only where the platform aliases
it to `ENODATA` (Linux) — on NetBSD the real library returns the real value.
Real stream/backend
integration tests cover the 3,802-byte Apple-xattr boundary and unchanged Windows
ADS fragmentation. Resource tests inject read failures after an earlier mismatch
and check that cleanup retains the sidecar.
Host runs keep those cases isolated and repeat them once through `all` to check
cross-case cleanup under sanitizers. Device runs use that same reset-isolated
`all` invocation as their sole run so the 6.8 MiB static fixture is uploaded once.
The `guard` case covers inactivity expiry, progress resets in the real hashing,
directory, metadata, and resource loops, checked output flushing, and teardown.
The `multi` case opens real read-only TDBs through the deploy input parser. It
covers mtime and nanosecond precedence, UUID/path ties, unique older values,
fragment ownership, raw FinderInfo, source changes, failed flushes, unresolved
older sources retaining newer databases, and whole-file quarantine without
changing the original bytes. The `oversized` case (issue 345) covers values
larger than one native attribute: 3,802 bytes migrate without the stream
marker, 3,803 bytes and a 12,979-byte extent-stored container value stay in the
TDB while the rest of the file migrates, cleanup still verifies the rest, the
single-database program quarantines instead of deleting (and keeps the database
live beside an unresolved row), multi coverage marks every holding source `X`
and quarantines both databases byte for byte, the deploy parser accepts `X` and
rejects unknown kinds, the report lists at most 50 values while counting all,
a hard link to a record with kept values counts and lists it once (deploy
walks and the single-database stats line),
a `._` file keeps its oversized value (and is retired at 3,802 bytes), and an
anchor claiming 35 extents still fails the volume. Python deployment tests cover completion receipts,
absent volumes, subsequent native edits, and interrupted software installation.
During deploy migration, merged TDB values replace conflicting native values;
cleanup requires exact readback before whole-database retirement. Native-only values and
resource-fork conflicts retain their existing behavior. `fruit:metadata=stream|netatalk` selects
the preferred legacy value only during migration, and `fruit:resource=file`
supplies AppleDouble sidecars to the migrator. Non-HFS shares retain the original
TDB and AppleDouble behavior.

The `long_names` and `folder_forks` cases (v3.1.1 telemetry) cover a 255-byte
name, whose 257-byte `._` name cannot exist, and a folder's resource fork, which
HFS cannot hold: its `._` file or TDB row keeps it and the rest of the folder
migrates. Those cases mock the kernel. The `hfs` case checks the same rules
against Apple's kernel and reports a skip anywhere else: with `TMPDIR` on the
device's HFS disk, which cross-exec sets, it checks ENAMETOOLONG for the long
`._` name and ENOENT and EPERM for a folder's fork, runs the single-database
program over a scratch tree holding a file and a folder bundle with `._` files
and a folder with a TDB row, and reads the real attributes back. Device runs of
`all` include it. To run only this case on a NetBSD 6 device, build the driver
in a lane tree (the fast single-driver loop: stage it with `run.py stage`, then
`waf build --targets=tc_xattr_migrate_test`) and, with the stripped driver in
`/Volumes/dk2`:

```sh
ssh root@<device> 'cd /Volumes/dk2 && TMPDIR=/Volumes/dk2 ./tc_xattr_migrate_test hfs; rc=$?; rm -f tc_xattr_migrate_test; exit $rc'
```

For a macOS mount of a device under test, also run:

```sh
.venv/bin/python -m tests.samba.manual_delete /path/to/mounted/share
```

This creates unique test objects at the share root and two nested depths and
checks first-attempt unlink/rmdir/rm -rf, metadata and a 90 KB stream roundtrip
and shrink. Cleanup retries cannot turn an observed failure into a pass.

### Installed native-manager integration

`device_supervision.py` is an opt-in test against a deployed device. It requires
`smbprotocol` on the host (not a runtime dependency), device SSH credentials, and
an available HFS share. It interrupts SMB service, so run it when backups can
be interrupted:

```sh
python -m tests.samba.device_supervision --config .env.backup6
```

The test verifies direct-child process groups, durable network reconnect,
SIGHUP reload, and disconnection of one replaced or reconfigured scratch
share root while a second tree in the same session stays usable. Root changes
cover narrowing, widening and a simultaneous rename without restarting smbd. It keeps a file open during
Samba, discovery, telemetry, and manager failures, checks fresh-client recovery,
and verifies that duplicate boot does not replace a healthy generation. Apple's
mDNSResponder, diskd, and afpserver must retain their PIDs. Native NBNS child
death must recover to one correctly owned, ready wcifsnd process.

It creates uniquely named scratch data and temporarily appends two shares to
the RAM configuration, restoring that configuration afterward. No Flash or
AirPort settings are changed. Root replacement checks the real Samba reload
path; physical USB revoke/reconnect still needs a spare disk and is a separate
hardware test.

Validation on 2026-09-20 used the installed unified service on the LAN NetBSD 6
and NetBSD 4 LE appliances. Deployment, Doctor, durable reconnect, targeted share
reload, duplicate boot, active-client process recovery, and native NBNS recovery
passed on both. The native reload and durable regression binaries also passed
on NetBSD 4 LE. On NetBSD 6, changing AFP advertising, telemetry opt-out, and
rsync enablement preserved Samba and Apple daemon PIDs; original settings were
restored afterward. The focused manager/process/storage/staging suite passed
53 tests under AddressSanitizer and UndefinedBehaviorSanitizer on the host.

The big-endian Samba artifact was rebuilt with the
existing NetBSD 4 SDK and checked as static ARM MSB; the UK device was unreachable,
so that build does not constitute big-endian hardware validation.

Physical USB detach/reconnect after this refactor remains pending because the
spare disk was unavailable. The scratch-root test verifies targeted reload and
unchanged-share continuity, while the native tests inject Apple's observed
revoked-descriptor behavior. Neither substitutes for the missing cable test.

### Native symlinks (patches 0045, 0058-0060)

The `tc_native_links_test` cases run the real `source3/smbd/tc_native_links.c`
in a scratch directory under `$TMPDIR` or the working directory, with real
symlink, rename, unlink, readlink and stat calls; Samba's VFS indirection, the
share-mode table, xattr storage and change notification are replaced. They pin
the XSym body byte for byte to digests the macOS client wrote on a Time Capsule,
reject every malformed or ordinary 1067-byte file, and accept only symlink
reparse payloads (Windows, NFS and WSL forms), refusing FIFOs, sockets, devices,
junctions and unknown tags. Hooks inside the replaced rename let other "clients"
replace or recreate the name between the conversion's steps: nothing of theirs
is replaced, and every failure (symlink, rename, attribute copy, times) puts the
original file back. Hooks in the replaced `sync()`, symlink and unlink let them
also take the name while a failed conversion is undone: their object stays, and
the original is kept aside rather than put over it. The original is only
unlinked after its fd is closed. A
file created without read access, as Linux `mfsymlinks` does, is read through an
internal handle that must be the same file. Each
conversion commits the journal with `sync()`, and so does a rollback before it
checks and removes the link it made; the test counts these calls (see the HFS journal note
in `DETAIL.md`). The metadata case checks that attributes and
streams set before close move to the link, but never Finder info or resource
forks. Those are matched by their exact stored names, so a stream such as
`backup.AFP_AfpInfo.notes` still moves. Buffers must grow on `ERANGE`
(`vfs_acl_xattr` does not accept NULL-size queries). The read and write cases serve and
accept the XSym view a Mac still holds of a link it just created: its bytes,
and writes (such as the zero-length write at its end macOS sends) that leave it
unchanged.

`tc_catia_links_test` runs the real `vfs_catia.c` link hooks against a recording
NEXT module, with the mappings vfs_fruit sets for macOS. Link reads and creates,
and the xattr calls made by path on a link without an fd, must reach the name on
disk (`x:y`, not the private-use character the Mac sends). The caller's handle
must keep its client name. It is a separate binary because including
`vfs_catia.c` pulls in most of the VFS layer. At that size, the other link cases
aborted in talloc when run from the HFS disk and passed from RAM; the cause is
not known. On NetBSD 4 the catia binary itself runs from the drivers' RAM disk above.

`links_device.py` exercises a deployed device from a Mac. It needs device SSH
credentials in the env file; its Windows and Linux client cases also need
`smbprotocol` on the host (not a runtime dependency) and are skipped without it:

```sh
.venv/bin/python -m tests.samba.links_device --env .env --afp
```

It uses `TC_SHARE_NAME`, or the device's only share; pass `--share` otherwise.

It creates links over SSH (including names with `: * ? " < > |`) and an XSym
file as an earlier release wrote it, then checks them from a macOS SMB mount:
listing, readlink, reading through, `ln -s`/`ln -sf`, xattrs on the link, `touch
-h`, `mv`, `cp -pR`, `rm`/`rm -rf`, and rewriting a legacy link. Issue #304's
shapes (links to `.`, `..`, nothing, a directory and outside the tree) are listed,
walked by `repair-xattrs` and `find`, renamed in place, across folders and over a
file, and removed with `rm -rf`, leaving their targets alone. `--afp` checks
that links made over SMB and AFP read the same over the other protocol. The SMB2
cases check the reparse listing, `FSCTL_GET_REPARSE_POINT`, removing a directory
link, `mklink` and Linux NFS/WSL symlink creation, refusal of `mklink /D`,
Windows-only targets, FIFOs and sockets with nothing left behind, and that a
stream and DOS attributes set before close move to the new link. It works only
in a `__tc_links_test__` folder on the share and removes it at the end.

After changing the conversion path, run the suite several times in a row and
check `/mnt/Flash/dmesg.panic` on the device: without the journal commit, the
HFS panic appeared within one to three runs, while each run on its own passed.

`durable_device.py` checks durable-handle reconnect against a deployed device
from a Mac (it needs `smbprotocol` on the host):

```sh
.venv/bin/python -m tests.samba.durable_device --env .env
```

It opens a file with a lease and a durable v2 request, drops the connection by
FIN, by RST and half-open, and reconnects from a new connection. A half-open
connection's open is refused with OBJECT_NAME_NOT_FOUND after 0024's retry
window (the case checks the wait too, as an immediate refusal has the same
status) unless the new session names the old one. The `rst+ipc-tdis` and
`rst+second-session` cases drop the connection after another tree or session
has closed, and the `doc:` cases end a session holding a delete-on-close file
without a CLOSE (by reset or a bare LOGOFF); without 0062 smbd closes those
files from `/` and loses the durable handle or the delete. The `settime:`
cases set a file's last-write time through a handle opened for data (as
Windows `CopyFile` does) and through one opened only for attributes (as macOS
does), then read it back; NetBSD 4's `futimens` replacement (0002) once failed
the data case with NOT_SUPPORTED. `--case NAME`
(repeatable) runs only the named cases. The macOS case stops the smbd serving
a mount for `--stall` seconds (default 60; 0 skips it) while a write is pending
and checks that the Mac's reconnect keeps the handle and its data. It works in a
`__tc_durable_test__` folder and removes it at the end.

### Directory operations: the *at emulation (patch 0002) and directory opens (0063)

Neither Apple kernel has the *at system calls (ENOSYS for openat, fstatat,
mkdirat, unlinkat, readlinkat, renameat, linkat, symlinkat, mknodat and
utimensat on NetBSD 4 and NetBSD 6; NetBSD 4 also lacks futimens and
fdopendir, and NetBSD 6's futimens mishandles UTIME_OMIT), so every call smbd
makes relative to a directory goes through `lib/replace/tc_at_emulation.c`.

`tc_at_emulation_test` runs that file against a real directory: every call
relative to a descriptor, absolute and with AT_FDCWD; the real calls' errno for
each failure; the working directory restored after every call and no descriptor
leaked over 200 rounds; names resolved from a descriptor's directory after it is
renamed; renames within a directory, through two descriptors of it, across
directories and onto existing names; a directory more than PATH_MAX from the
root; renames and links between directories past PATH_MAX (up, down, sibling,
cousin, deep to shallow and back, names sharing a prefix but not a component),
which the emulation makes with a name relative to one of the two directories,
including out of and into the deepest directory getcwd() can name (4,095
bytes); past that, and between branches too far apart for a relative name,
the emulation cannot make the call a real renameat() would, and the case
accepts either the move or a clean ENAMETOOLONG (never ERANGE, nothing
moved) rather than pinning the limitation; O_NOFOLLOW (ELOOP, not NetBSD's EFTYPE) and O_DIRECTORY (emulated on
NetBSD 4, whose kernel ignores it); utimensat/futimens with UTIME_NOW and
UTIME_OMIT; and fdopendir over a 2,500-entry directory (the caller's descriptor
number, rewinddir, a moved working directory, a renamed directory, close-on-exec
and closedir); and listings of a directory that changes while another
descriptor on it is closed between batches, deleting (nothing skipped),
creating (nothing repeated), reading again after the end (nothing repeated)
and rewinding, twice (the new contents). NetBSD 4's HFS drops a listing's
place on any such close, which smbd makes for every create and delete, so its
emulated fdopendir() reads the whole directory up front and its rewinddir()
reads it again. Both kernels' HFS repeat a listing's last entries when it is
read again after its end; NetBSD 6, whose HFS otherwise keeps a listing's
place, uses libc's fdopendir() and a readdir() that keeps reporting the end
until rewinddir(). That wrapper uses NetBSD's DIR, so only the NetBSD 6 device
runs it; the host runs the same checks against its own readdir(). Host builds compile the emulation into the driver. It needs only
libreplace and is small enough for `/mnt/Memory`; on NetBSD 4 run it from
there. On 2026-09-28 a driver run from the HFS disk there stayed in disk wait
and the device's next reboot hung; that one was not shown to cause the other,
and running from RAM avoids the question. Apple's HFS
accepts lutimes() on a symlink but keeps the link's times, so the link-time
check accepts either result; the target must never change.

`dir_device.py` checks the same paths over SMB against a deployed device:

```sh
.venv/bin/python -m tests.samba.dir_device --env .env.backup4 --record /tmp/after.json --compare /tmp/before.json
```

Its open matrix tries every create disposition with and without
FILE_DIRECTORY_FILE and FILE_NON_DIRECTORY_FILE, for listing, attribute and
read/write access, on an existing directory: whatever opens must be a directory
handle (SMB2 READ is INVALID_DEVICE_REQUEST), must list, and must leave nothing
behind. A file handle on a directory, the state patch 0063 now prevents and
patch 0004 used to paper over, fails that READ. The other cases list a
600-entry directory in small responses with RESTART_SCANS, REOPEN and
RETURN_SINGLE_ENTRY; open directories with catia-mapped names; keep listing a
directory handle's own directory after the directory is renamed on disk; nest
40 directories (1,363 bytes), create, list and rename at the bottom, and move a
file up a level, into a sibling, to the top of the test folder and back down (the old
vfs_default fallbacks renamed through absolute names and so refused to create a
directory within about 1,024 absolute bytes; smbd still looks up a few names,
such as a delete-on-close file's parent, by full path from the share root, and
NetBSD refuses those past PATH_MAX, so the deepest levels are deleted over SSH);
delete and rename directories; and set
last-write times through attribute-only handles. The Mac cases copy a
40-file tree with ditto and compare it, list a 150-entry directory twice
and create, fill and detach a sparsebundle (every smbfs create costs about
3 seconds on the appliance with debug logging, so they are kept small). `--record` and `--compare` save and
diff the status of every open and error case between two smbd builds.

For a wider comparison, run Samba's own smbtorture folder suites from a Linux
container against the device before and after a change (smbtorture built from
the pinned Samba with `--nonshared-binary=smbtorture`), and compare the
per-test outcomes: many tests fail against any appliance configuration, so only
a changed outcome matters.

### File growth on HFS (patch 0065)

HFS has no sparse files, so growing a file allocates every block up to its new
end. smbtorture's `smb2.rw.invalid` writes one byte at MAXFILESIZE - 1 (16 TiB);
before 0065 that request never finished on either device (2026-09-28), so keep
`smb2.rw.invalid` out of any smbtorture run against a device whose smbd lacks
0065. With the patch, a write, SET_INFO end-of-file or server-side copy that
would grow a file on HFS by more than the volume's available space fails with
DISK_FULL before anything is allocated. That is the answer `smb2.rw.invalid`
expects from Windows, so it passes when smbtorture runs without `--target`;
with `--target=samba3` or `samba4` it expects success (a sparse file) and fails
on a patched device. Either result is safe; only a hang is a regression.

`tc_file_growth_test` runs the real `source3/smbd/tc_file_growth.c` with the
file's size and its volume's statvfs answers controlled, so the 16 TiB request
against a 2 TB volume is decided without any allocation. Its cases cover growth
that needs no system call (appends, overwrites, shrinks and holes up to
64 MiB), a cached size behind smbd's own writes, growth that fits, growth past
the available space (the torture request included), the exact byte boundary in
`f_frsize` and `f_bsize` units, other filesystems, stream placeholders whose
descriptor has no volume, and a failed size refresh. `real_volume` asks the
real `fstatvfs()` about a new file in `$TMPDIR`: on a device's HFS disk, growth
past the available space is refused; on other systems it is allowed. The check
never writes, and the case confirms the file did not grow. `real_resource_fork`
checks, on a device, what an AFP_Resource handle relies on: the descriptor
patch 0056 opens (`<file>/..namedfork/rsrc`) reports the fork's size, not the
file's, and an HFS volume, so growth of the fork past the available space is
refused too.

The `call_` cases run the four callers 0065 patches, cut from the patched
source by `run.py stage()` (`tc_file_growth_callers.inc`), with the I/O below
them counted instead of done: synchronous writes (`real_write_file`, which also
serves every stream write), asynchronous writes (`pwrite_fsync_send`, used with
aio_fork), SET_INFO end-of-file (`vfs_set_filelen`) and server-side copies
(`vfswrap_offload_write_send`). A refused request must fail with ENOSPC or
DISK_FULL without reaching the write, truncate or copy; growth that fits, a
shrink, a zero-length write and a POSIX append must reach it. A size refresh
that fails without setting errno must still fail an asynchronous write with an
error of its own (EIO): tevent ignores an error of 0 and would finish the
request while it is still in progress, which smbd reports as INVALID_PARAMETER.

`growth_device.py` sends the refused requests to a deployed device over SMB
(it needs `smbprotocol` on the host):

```sh
.venv/bin/python -m tests.samba.growth_device --env .env.backup6 [--aio]
```

It first looks for 0065's refusal message in the smbd the device runs and
stops if it is missing, because without the patch these requests make HFS
allocate the whole growth. Each refused request grows a file past the volume's
total size, which no amount of freed space can make fit, and must fail with
DISK_FULL within 10 seconds and leave the file's size unchanged: the torture
sequence (64 KiB, one byte at MAXFILESIZE - 1, then a zero-length write at
MAXFILESIZE, which succeeds), a one-byte write, SET_INFO end-of-file, a
one-byte server-side copy, and a one-byte write through a file's AFP_Resource
stream (the fork keeps its 10 bytes, and can still grow by 100 MiB). A write
32 MiB past the end, an end of file set to
96 MiB and back, and 96 MiB of sequential 4 MiB writes must still succeed.
`mac:sparsebundle` mounts the share and does to a sparse bundle what Time
Machine does, with the devices' backup band size (487,854,080 bytes): it
creates an HFS+ image that ends 400 MiB into a band, writes 600 MiB of files,
grows the image and reads the files back. Writing each image end starts its
last band hundreds of MiB past the band's beginning, growth beyond the 64 MiB
0065 leaves unchecked, so the case checks that those band files reached that
size. Files are opened delete-on-close in a `__tc_growth_test__` folder,
which is removed at the end.

`--aio` runs the suite with the manager's aio_fork settings (as with
`VFS_AIO_FORK_ENABLED`) written to the running smb.conf in RAM only, and puts
the original back at the end. The device suite otherwise runs with the default
synchronous writes. SMB2 sends stream writes and zero-length writes
synchronously either way; with debug logging, the suite checks that smbd logged
the refused one-byte write completing through aio_fork.

## Test tiers and tooling

AGENTS.md ("Test tiers") defines a quick tier (every iteration, about 25
minutes) and a full tier (per commit batch, about 2-2.5 hours).
`python -m tests.samba.check --tier quick|full --out DIR --build` runs one:
`vm_build.py` builds the three lanes on the VM, `swap_smbd.py` runs the
built smbd on a device without deploying (quick) or the full tier deploys,
and each device then runs the regression drivers from RAM, doctor,
`dir_device.py`, `growth_device.py` (both with `--quick` in the quick
tier), in the full tier also `durable_device.py` and `links_device.py`, and
`torture.py`, which runs smbtorture in Docker and reports only failures
missing from `smbtorture/known_failures.txt`. `locks.py` claims and releases
the rows of the shared lock file around each phase.
