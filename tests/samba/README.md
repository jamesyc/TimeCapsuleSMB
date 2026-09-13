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
SAMBA4X_RUN_REGRESSION_TESTS=1 ./build/samba4x.sh
```

Use `samba4xoldle.sh` or `samba4xoldbe.sh` for the other lanes. The wrapper builds
static tests, executes all cases using `samba4-cross-exec.sh`, and stops before
staging `smbd` if any test fails or is missing. Ordinary offline builds remain
compile-only; run this validation before copying rebuilt release artifacts.
`SAMBA4X_BUILD_REGRESSION_TESTS=1` also compiles and strips the test executables
offline, but does not claim a device-validation pass.

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

For a macOS mount of a device under test, also run:

```sh
.venv/bin/python -m tests.samba.manual_delete /path/to/mounted/share
```

This creates unique test objects at the share root and two nested depths and
checks first-attempt unlink/rmdir/rm -rf, metadata and a 90 KB stream roundtrip
and shrink. Cleanup retries cannot turn an observed failure into a pass.
