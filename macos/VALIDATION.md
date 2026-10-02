# Swift operation completion validation

## 2026-10-02 — backend event ingress and uninstall ordering

The stale-request regression now delivers through the fake helper's actual
callback, including the `BackendClient` stage and confirmation handling. It
exposed a gap before workflow filtering: an old request could disable
cancellation and leave a pending confirmation that kept the device busy.
`BackendClient` now drops explicitly mismatched request IDs before those state
changes. A separate regression preserves compatibility with events that omit
their request ID.

A new ordering regression blocks a successful uninstall's registry write,
finishes helper cleanup, completes an install from a reopened dashboard session,
then drains persistence. The current install survives both in memory and after
reload; the old uninstall cannot erase it.

Validation on this Mac:

- The strengthened stale-event test failed with five assertions before the
  backend guard; the uninstall ordering case passed without production changes.
  Log: `/tmp/tcapsule-review-gaps-red.log`.
- Focused backend/coordinator/workflow suites: 55 tests passed.
  Log: `/tmp/tcapsule-review-gaps-focused.log`.
- Full `swift test --package-path macos/TimeCapsuleSMB`: 610 tests passed.
  Log: `/tmp/tcapsule-review-gaps-swift-full.log`.
- `make test-parallel`: native service compile/smoke checks and all 3,097 Python
  tests passed, with 44 Python `forkpty` deprecation warnings.
  Log: `/tmp/tcapsule-review-gaps-python-native.log`.
- `make lint` passed. Log: `/tmp/tcapsule-review-gaps-lint.log`.
- `git diff --check` passed. No VM/device access or held locks.

## 2026-10-02 — simplify workflow state ownership

Maintenance now reads state, results, plans and selections directly from its
child stores and forwards their change notifications. SSH result application,
failure-triggered SSH refresh and credential invalidation subscribe to the
specific child publishers, so these side effects no longer depend on an
asynchronous copy into the maintenance facade.

`MaintenanceWorkflowOperation` now owns its event observer and backend
subscriptions directly; the forwarding `MaintenanceOperationRunner` was
removed. Deploy, Doctor, Flash and maintenance use the coordinator for all
execution, including tests created from an injected backend. Confirmation replay
uses the operation lane so device ownership survives the transition.

Removed unused coordinator mirrors of active/rejected operations. Workflow
callers still receive rejected start results, and the coordinator retains its
live operation map, confirmation identity and device reservation checks.
Progress/recovery snapshots now copy existing values and explicitly reset
terminal diagnostics and stale summaries. Failed snapshot mapping is
nonoptional because it always produces a result.

Validation on this Mac:

- New maintenance regressions cover immediately readable child state and view
  notifications, fsck selection/plan invalidation, each remote child's credential
  and SSH-refresh side effects despite another selected error, and preservation
  of an SSH observation's timestamp when unrelated child state changes.
- Snapshot checks cover preserved attempt identity/progress metadata and clearing
  obsolete summary/error/diagnostic fields when recovering an interrupted run.
  Coordinator tests assert returned rejections and actual reservation/release.
- Focused workflow/coordinator/registry suites: 197 tests passed.
  Log: `/tmp/tcapsule-workflow-simplify-focused.log`.
- Full `swift test --package-path macos/TimeCapsuleSMB`: 608 tests passed.
  Log: `/tmp/tcapsule-workflow-simplify-swift-full.log`.
- `make test-parallel`: native service compile/smoke checks and all 3,097 Python
  tests passed, with 44 Python `forkpty` deprecation warnings.
  Log: `/tmp/tcapsule-workflow-simplify-python-native.log`.
- `make lint` passed. Log: `/tmp/tcapsule-workflow-simplify-lint.log`.
- Independent review and `git diff --check` found no remaining issues.
  No VM/device access or held locks.

## 2026-10-02 — deploy results recover after an initial registry write failure

Terminal deploy snapshots no longer require the attempt's initial snapshot to
have persisted successfully. Removed `expectedOperationID` from the registry
methods and dashboard callers. Request-ID filtering and the shared ordered
write queue still reject old events and preserve operation order; profile
existence and terminal-versus-progress checks remain intact.

Regression coverage uses a disposable registry path made temporarily unwritable
by replacing its file with a directory. After the initial writes drain, the test
restores the file and completes the operation. Six cases cover success, failure
and confirmation cancellation, each with missing or previously saved install
history. They verify the in-memory and reloaded snapshots, dashboard availability
and successful persistence of the rsync setting. A workflow-level test replaces
the old direct stale-snapshot test: an earlier request's stage, confirmation,
error and result cannot change a newer attempt, whose own result still saves.
Existing blocked-persistence/retry coverage is unchanged.

Validation on this Mac:

- Before the fix, the new recovery regression failed with 58 assertion failures.
  Log: `/tmp/tcapsule-initial-save-red.log`.
- Focused Swift workflow/registry suites: 91 tests passed.
  Log: `/tmp/tcapsule-initial-save-focused.log`.
- Full `swift test --package-path macos/TimeCapsuleSMB`: 603 tests passed.
  Log: `/tmp/tcapsule-initial-save-swift-full.log`.
- `make test-parallel`: native service compile/smoke checks passed; all 3,097
  Python tests passed. The run reported 44 Python `forkpty` deprecation warnings.
  Log: `/tmp/tcapsule-initial-save-python-native.log`.
- `make lint` and `git diff --check` passed. No VM/device access or held locks.

## 2026-10-02 — cancellation stops dashboard activity

Install confirmation cancellation now finishes the attempt as failed with
`confirmation_cancelled`. Overview reports running work only while the
coordinator owns a live operation. An orphaned saved `installing` state is shown
as interrupted, and an SSH-backed Checkup can replace it.

Workflow events are consumed synchronously after backend history is updated.
Dashboard writes capture the originating event's values, run in arrival order
across sessions, and reject updates belonging to an older install attempt.
Uninstall, fsck and metadata repair wait for the cancellation event before
resetting their local state; helper cleanup continues to reserve the device.

Validation on this Mac:

- The original Install cancellation regression failed against the old behavior
  with 12 assertion failures. Log: `/tmp/tcapsule-cancel-red.log`.
- Deterministic regression tests cover duplicate dismissal, immediate retry or
  Checkup while registry persistence is blocked, dashboard reopening, a delayed
  Checkup followed by Install, old attempt updates, profile deletion/selection,
  failed persistence, orphaned saved state, and cancellation before helper
  cleanup. Maintenance cases include Uninstall, activation, SSH enablement,
  fsck, metadata repair and both Flash write modes, including changed plan
  options. Tests check actual workflow behavior and dashboard availability.
- A full run exposed the same result-before-release race in the existing
  successful-Install/Refresh Status test. It now pauses after the result, checks
  that the device is still owned, releases the runner, and waits for availability
  before Refresh Status. The Checkup/Install snapshot test also waits for release.
- Final `swift test --package-path macos/TimeCapsuleSMB`: 602 tests passed,
  zero failures. Log: `/tmp/tcapsule-cancel-full-final-2.log`.
- Native debug app packaging with `--full-validation` passed, including helper
  and bundled-tool smoke checks. Log: `/tmp/tcapsule-cancel-package.log`.
- Real HelperRunner integration using that package passed on NetBSD 6 and
  NetBSD 4 LE: cancel Install, reload its saved failure, run Checkup, cancel
  Uninstall, and verify the saved installation remains. Doctor returned 90 PASS,
  7 INFO on NetBSD 6 and 80 PASS, 8 INFO on NetBSD 4 LE, with zero warnings or
  failures on either device. The tests used disposable local
  registries/config copies and an in-memory password store; confirmations were
  never accepted, so no deployment, uninstall or reboot occurred.
  Logs: `/tmp/tcapsule-cancel-live-6.log` and
  `/tmp/tcapsule-cancel-live-4.log`. The local harness is retained under ignored
  `plan/swift-cancellation/` and is not part of CI.
- In the packaged app UI, using disposable profile storage, cancelling the
  Install/reboot confirmation showed `Operation cancelled` on Overview with
  neither Connection nor Runtime spinning. Run Checkup and Install were enabled.
  Running Checkup then returned Overview to Healthy (90 PASS, no WARN or FAIL).
  The subsequent Connection availability refresh was triggered manually by the
  user, rather than automatically by cancellation.
- Both device locks were released. `git diff --check` passed.

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
