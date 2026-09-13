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

Final validation on 2026-09-12:

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
