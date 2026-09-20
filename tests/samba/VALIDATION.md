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
  writers were removed. Each deploy phase allows six hours and retains its
  diagnostic output under the payload's logs directory.

| Lane | Stripped migrator bytes |
| --- | ---: |
| NetBSD 6 (NetBSD 7 SDK) | 2,128,772 |
| NetBSD 4 LE | 2,135,460 |
| NetBSD 4 BE | 2,135,012 |
