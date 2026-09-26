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

On the NetBSD 4 appliance, run the storage-reload driver from `/mnt/Memory`
with its working directory on the HFS scratch volume. A disk-backed driver
was observed aborting in talloc before loading its first configuration; the
identical image passed every case from RAM, matching production Samba's
placement. If RAM is full, stop and drain the managed runtime before removing
its disposable RAM smbd image, run the driver, then remove the driver and
start the installed `rc.local` again. Disable core dumps for these runs so
Apple's default `/tmp/%n.core` does not exhaust the small root RAM disk.

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
changing the original bytes. Python deployment tests cover completion receipts,
absent volumes, subsequent native edits, and interrupted software installation.
During deploy migration, merged TDB values replace conflicting native values;
cleanup requires exact readback before whole-database retirement. Native-only values and
resource-fork conflicts retain their existing behavior. `fruit:metadata=stream|netatalk` selects
the preferred legacy value only during migration, and `fruit:resource=file`
supplies AppleDouble sidecars to the migrator. Non-HFS shares retain the original
TDB and AppleDouble behavior.

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

### Native symlinks (patch 0045)

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
original file back. The original is only unlinked after its fd is closed. A
file created without read access, as Linux `mfsymlinks` does, is read through an
internal handle that must be the same file. Each
conversion commits the journal with `sync()`, and so does a rollback before it
removes the link it made; the test counts these calls (see the HFS journal note
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
not known. On NetBSD 4 the catia binary itself needs the RAM procedure above.

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
