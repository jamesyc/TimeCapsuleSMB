# Swift operation completion validation

## 2026-10-02 — test synchronization

Result delivery and helper completion are distinct. Tests which start another
operation or inspect availability now wait for both their expected result and
resource availability. Pending confirmation still reserves the resource.

- The failed-install/checkup test pauses the background SSH runner after its
  result, proves that the device is still busy, releases it, and waits for
  availability before Doctor. It verifies the operation sequence and profile.
- The SSH refresh cache test finishes the first refresh before testing cache
  suppression/expiry, and checks a fresh timestamp and payload after expiry.
- Flash tests wait between dependent steps. Three fresh-runner cases pause
  backup, plan and write separately and check that capabilities remain disabled.
- The fsck error sequence pauses after the backend error before exercising the
  subsequent malformed-response case. Repair and discovery fixture waits now
  also require completion.
- PauseGate checks cancellation under its registration lock. Tests cover an
  already-cancelled task, cancellation after registration, and cancellation
  concurrent with explicit release; no cancellation test needs manual release
  to satisfy its completion expectation.

Validation on this Mac:

- Focused Dashboard, SSH, Flash, Maintenance, Discovery and test-support suites:
  94 tests passed. Log: `/tmp/tc-swift-completion-tests-1.log`.
- `swift test --package-path macos/TimeCapsuleSMB`: 582 tests passed, zero failures.
  Log: `/tmp/tc-swift-completion-full-1.log`.
- `git diff --check` passed. No production changes or device access in this commit.

## 2026-10-02 — production operation completion

The dashboard now retains a failed workflow's SSH refresh until that device is
available. Duplicate failure publications are coalesced by request identity;
a successful retry or a newer successful SSH check supersedes pending work.
The refresh resolves the current registry profile and is discarded after profile
deletion or session disposal.

Add Device and the profile editor keep their actions busy until both helper
cleanup and local profile persistence finish. Reset invalidates old UI updates
without discarding files still owned by persistence. Helper completion publishes
retry availability even when the visible workflow has already failed.

Confirmation presentation waits for helper exit while continuing to reserve the
device. Accept/cancel actions carry the displayed confirmation's identity, so
another device's completion or a delayed alert dismissal cannot target a new
request. The displayed request stays stable while other devices finish.

Validation on this Mac:

- Ten new deterministic tests pause runners after terminal events and block the
  registry's final persistence write. They cover both completion orders, failure
  and retry, reset during persistence, another workflow claiming the device,
  duplicate failures, successful retry/manual SSH supersession, historical SSH
  results, profile deletion, session disposal, confirmation before helper exit,
  cross-device ordering, and stale acceptance/dismissal.
- Sensitivity check: the first nine tests were run against the original production
  sources from `a587815a`, with only an adapter mapping the new confirmation API
  back to the old UI calls. Eight tests failed with 33 assertion failures; the
  session/profile-disposal case already passed. Behavioral assertions were
  unchanged. Log: `/tmp/tc-swift-completion-all-before.log`.
- Final `swift test --package-path macos/TimeCapsuleSMB`: 592 tests passed,
  zero failures. Log: `/tmp/tc-swift-completion-full-final.log`.
- Native debug app packaging with `--full-validation` passed, including bundled
  helper validation. The packaged app launched and rendered its device dashboard
  and Checkup screen. Log: `/tmp/tc-swift-completion-package.log`.
- Real HelperRunner integration against NetBSD 6 and NetBSD 4 LE used disposable
  local registries/config copies and an in-memory password store. An intentionally
  wrong password caused a remote authentication error before filesystem commands;
  automatic SSH status completed, then Doctor was accepted and returned 86 PASS,
  0 WARN, 0 FAIL, 7 INFO on each device. A real activation confirmation was
  presented after helper exit, cancelled, and released the device without
  executing activation. No deployment or reboot was performed.
  Logs: `/tmp/tc-swift-completion-live-6.log` and
  `/tmp/tc-swift-completion-live-4.log`. Reproduction harness is retained locally
  under ignored `plan/swift-operation-completion/`; it is not part of CI.
- Both device locks were released after validation. `git diff --check` passed.
