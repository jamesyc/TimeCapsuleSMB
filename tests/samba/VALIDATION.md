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
