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

The cases cover talloc isolation through the real AIO fork path, preservation of
worker errors, successful and failed synchronous fallback, worker limits and
share isolation, FIFO saturation, cancellation, worker failure recovery and idle
cleanup. Durable tests cover live-to-disconnected transition, bounded retry
exhaustion, unlocked waiting and identity/ownership rejection. They do not assert
the literal retry limit of 34.

The sanitizer cases exposed two bugs fixed by patches 0033 and 0034: a
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
cases cover per-file TDB retirement, failed transaction commits, subsequent boots
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
During deploy migration, TDB values replace conflicting native values; cleanup
requires exact readback before retiring the TDB record. Native-only values and
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
