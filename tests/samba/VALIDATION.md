# Regression sensitivity checks

The implementation was checked by deliberately restoring bugs in a disposable
Samba checkout, rebuilding the affected target, and running the named case.
Each mutation below failed at runtime; the source was restored and rebuilt after
each check. All cases passed again after restoration.

| Deliberately broken behavior | Case that rejects it |
| --- | --- |
| Omit the AIO child's talloc stack reset | `read` |
| Treat a negative worker read result as an oversized successful read | `read_error` |
| Allow worker creation beyond the configured bound | `limits` |
| Insert queued work at the head rather than the tail | `queue` |
| Reject a matching live durable reconnect immediately | `transition` |
| Remove the bound on live-open retries | `exhausted` |
| Wait from inside the locked recreate callback | `transition` |
| Leave a dangling pool pointer after freeing the pthreadpool | existing pthreadpool lifecycle test, ASan exit 86 |
| Restore the zero-length fd array during worker shutdown | `read`, UBSan detected in worker and rejected by parent |
| Leave the cancelled request's read watcher alive during worker replacement | `cancel_active`, bounded timeout |

The retry tests check eventual success/failure and unlocked waiting without
asserting the particular retry limit of 34. Timing-sensitive cancellation is
also run under sanitizers, which reproduced the stale-watcher failure that an
ordinary build could miss.

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

Full local pytest passed 1,859 tests with four workers. A ten-worker run hit the
known timing-sensitive ACP telemetry timeout; that exact case passed immediately
alone before the clean full rerun. This validation does not claim macOS 27
coverage or a full Time Machine backup cycle.

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
