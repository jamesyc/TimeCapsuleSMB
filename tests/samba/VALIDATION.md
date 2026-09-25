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
