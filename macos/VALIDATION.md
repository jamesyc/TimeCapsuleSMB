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

## 2026-10-03 — Bonjour discovery and verified device identity

### Behavior

- Host callers use `BonjourQuery` or the single `discover_snapshot_detailed`
  convenience function. Installed `dns-sd` selects native discovery; otherwise
  separate zeroconf IPv4 and IPv6 transports are merged. Provider selection stays
  fixed after empty results, errors, permission denial and cancellation. GUI
  discovery requests `_airport`; CLI and unfiltered API discovery retain all
  five service types. Public timeouts are finite and at least five seconds;
  older shorter saved values load as six seconds.
- Native discovery uses four workers, bounded streamed output, scoped generations,
  withdrawal handling and owned child cleanup. Resolution shares one three-second
  grace. Within a scan, silence/errors preserve earlier address evidence, while
  fresh family answers replace it and explicit withdrawals/negative replies
  remove it. Both Apple's `No Such Record` output and numeric errors are covered.
  This practical finite-scan policy adds no persistent DNS cache.
- Doctor browses once and scopes evidence to the configured endpoint's observed
  interface. Address-family filtering applies only to SMB endpoint selection;
  the full scoped snapshot remains available for ADisk, AFP, device-info,
  duplicate and conflicting-observation checks. A related service's incomplete
  address lookup cannot manufacture a missing advertisement or hide bad TXT.
  Supplemental hostname resolution remains available. Missing families can be
  informational; concrete identity errors remain failures.
- Diagnostics derive counts/errors from provider evidence and serialize attempts
  once. Direct and nested service summaries retain optional interface indexes,
  use the same bounded serializer and do not add hardware MACs to telemetry.
- Candidates group by optional normalized Apple `waMA`; observations without a
  usable MAC retain deterministic scoped IDs. One real selected record supplies
  the connection target. Authenticated SSH probes read syAP, syAM and optional
  waMA using read-only ACP queries. Failed/malformed MAC output does not discard
  usable model information. Configure rejects contradictory advertised and
  authenticated identities before committing configuration.
- Saved profile UUIDs stay independent of discovery IDs. The registry actor
  validates construction, save, update and checkup; final-save validation also
  protects against intervening mutations. Conflicting saves/checkups preserve
  existing profiles. Names offer explicit reconnect only when exactly one current
  candidate matches the suggested saved profile, including legacy profiles
  without a MAC. AirPort Utility's internal matching algorithm is not established;
  this policy uses Apple's identity fields and authenticated confirmation.
- Fixture setup calls production construction/save primitives with explicit
  existing-profile selection. The unused automatic-merging save method is removed.
  Concurrent saves reject duplicate endpoint/hardware ownership; production
  persistence coverage verifies the winning credentials/config survive rejection.
- Permission preflight distinguishes actual results, structured policy denial and
  inconclusive probes. Cancellation tears down callbacks once; late completion
  cannot restart cancelled work. Lane/readiness changes are checked before helper
  launch. All ten catalogs retain localized reconnect and recovery copy.
- Existing deterministic packaging order and manager-test child-start
  synchronization fixes are retained. macOS CI installs Python dependencies for
  the Swift/helper integration tests. No native build inputs, binaries, manifest,
  environment files or runtime state files are changed by this follow-up.

### Follow-up regression verification

The new doctor and ambiguous-reconnect regressions failed before the production
fixes (`/tmp/tc-implementation-reconnect-before.log` records the Swift reproduction).
Doctor cases cover both partial ADisk address families, reversed record order,
missing services, invalid TXT and wrong targets. Reconnect cases cover both name
fields, both peer orders and refresh removing the competitor. Existing provider,
scoping, cancellation, rollback, localization and packaging regressions are retained.

- `make test-parallel`: host native compile checks and all 3,203 Python tests
  passed in 173.38 seconds. Log: `/tmp/tc-implementation-python-full.log`.
  Python 3.14 emitted 44 existing `forkpty()` deprecation warnings.
- `swift test --package-path macos/TimeCapsuleSMB`: all 656 tests passed,
  including the new reconnect and production persistence conflict regressions.
  Log: `/tmp/tc-implementation-swift-final.log`.
- Focused Python checks passed 867 tests and 247 subtests:
  `/tmp/tc-implementation-python-focused-final.log`. The final CLI diagnostics
  annotation cleanup passed 119 CLI tests and 19 subtests:
  `/tmp/tc-implementation-cli-final.log`.
- Provider-to-Swift fixture freshness, Ruff and `git diff --check` passed.
- Native release packaging with `--full-validation` passed dependency/signature
  validation and bundled helper smoke checks. Log:
  `/tmp/tc-implementation-package.log`; app:
  `/tmp/tc-implementation-package/TimeCapsuleSMB.app`.
- Live discovery returned six resolved services, all dual-stack:
  `/tmp/tc-implementation-live-discovery.json`. Read-only doctor `--skip-smb`
  passed on NetBSD 6 and NetBSD 4:
  `/tmp/tc-implementation-live-doctor6.log` and
  `/tmp/tc-implementation-live-doctor4.log`.
- Both LAN rows were claimed and re-read before the live checks, then released.
  No locks are held. No deploy/reboot or VM builds were performed. Work remains
  uncommitted.

### Earlier evidence and remaining limits

- Previous full checks: 3,201 Python tests and 654 Swift tests passed in
  `/tmp/tc-fixes-full-python-final.log` and `/tmp/tc-fixes-swift.log`.
  Previous live discovery and both device doctors passed in
  `/tmp/tc-fixes-live-discovery.json`, `/tmp/tc-fixes-live-doctor6.log` and
  `/tmp/tc-fixes-live-doctor4.log`.
- Earlier release packaging passed `--full-validation`:
  `/tmp/tc-review-fixes-package.log` and
  `/tmp/tc-review-fixes-package/TimeCapsuleSMB.app`.
- Earlier Mac sanitizer/deploy tests passed 906 cases:
  `/tmp/tc-bonjour-three-mac-sanitizers.log`. Ubuntu 24.04 ARM passed 906
  sanitizer/deploy tests, 14 artifact checks and 2,226 remaining Python tests
  with three environment skips: `/tmp/tc-bonjour-ubuntu-arm-native.log` and
  `/tmp/tc-bonjour-ubuntu-complete.log`. Git 2.49 supplied `rebase --empty=stop`.
  Ubuntu x86 under Rosetta did not have a clean pass because of descriptor and
  build-wrapper timeout limitations: `/tmp/tc-bonjour-ubuntu-ci.log` and
  `/tmp/tc-bonjour-ubuntu-amd-final.log`.
- Packaged permission allowed/denied flows were manually checked earlier. The
  first undecided privacy prompt still needs a fresh account/VM; no privacy
  settings were reset. Ubuntu checks are earlier evidence, not this follow-up's
  verification.

## 2026-10-03 — Bounded family completion and discovery simplification

- Zeroconf transport selection and requested address types are independent.
  Browse and targeted resolution explicitly query missing A/AAAA records using
  the dependency's family resolvers; a cached single-family ServiceInfo no longer
  ends dual-stack completion. Both transports share absolute deadlines. Browse
  admission closes before the existing three-second grace; targeted resolution
  keeps its original total budget. Partial answers survive silence and errors.
- Pending names rotate in insertion order behind fresh work. Source generations
  reject withdrawn/replaced services and old SRV targets. Cancellation closes both
  transports, and late link-local answers retain their observed interface scope.
  Each transport can use the shared grace independently; no cross-transport
  coordination or persistent cache is added merely to save that bounded wait.
- The zeroconf minimum is 0.148.0 in both dependency files. Supported ServiceInfo
  address parsing replaces the compatibility ladder. UDP route selection is
  shared from core.net. Doctor's name/IP selection paths use one validation block,
  retaining every service, identity, ADisk, AFP and conflicting-evidence check.
- Providers construct one neutral diagnostics envelope directly. The obsolete
  merged envelope, provider introspection, duplicate command summaries, unused
  collector/profile helpers, NSS alias and setup preflight forwarder are removed.
  CLI failure telemetry excludes the confirmed appliance MAC at the shared
  diagnostic boundary; the local configure/doctor identity payload is preserved.
- Fixture generation still exercises both wire decoders and asserts their
  equivalence, then writes each reviewed payload once. Swift keeps all twelve
  distinct scenarios and the save/reload/edit coverage. Relative to the reviewed
  dirty tree, production source shrank by 272 lines and the fixture by 1,433 lines.

Verification:

- New completion regressions failed before the fix, covering delayed IPv4/IPv6,
  bounded partial results and targeted resolution. A real zeroconf cache and
  response-delivery test verifies that the missing-family DNS query is sent.
  Added checks cover fair admission, cancellation, stale generations, scope,
  inconclusive retries and both doctor selection paths in both record orders.
- `make test-parallel`: host native compile checks and all 3,252 Python tests
  passed in 172.90 seconds (`/tmp/tc-ponytail-full-final.log`). Python 3.14 emitted
  the 44 existing forkpty deprecation warnings. Final obsolete-type/helper removal
  also passed 177 focused tests and 22 subtests (`/tmp/tc-ponytail-orphans.log`).
- `swift test --package-path macos/TimeCapsuleSMB`: all 656 tests passed
  (`/tmp/tc-ponytail-swift-final.log`).
- Python 3.9.6 with zeroconf 0.148.0: 286 focused discovery, doctor, CLI and
  diagnostics tests passed (`/tmp/tc-ponytail-py39-verified.log`).
- Ruff, fixture freshness and `git diff --check` passed. Native release packaging
  uses `--full-validation`; output is `/tmp/tc-ponytail-package/TimeCapsuleSMB.app`
  and the final log is `/tmp/tc-ponytail-package-verified.log`.
- Live discovery returned six resolved, dual-stack services
  (`/tmp/tc-ponytail-live-discovery.json`). Read-only doctor `--skip-smb` passed on
  NetBSD 6 and NetBSD 4 (`/tmp/tc-ponytail-live-doctor6.log` and
  `/tmp/tc-ponytail-live-doctor4.log`). Both LAN rows were claimed and re-read,
  then released. No deploy/reboot, native payload changes or VM builds occurred.
  No locks are held. Changes remain uncommitted.

## 2026-10-03 — Remove unused Bonjour bookkeeping

- Removed the unused `BonjourQuery._name` assignments and the native provider's
  unconsumed resolve-attempt counter. Provider selection and emitted diagnostics
  remain unchanged; the zeroconf counter is still used and retained.
- The first full run exposed a manager-test startup race: the NBNS audit checked
  Samba's start count after waiting only for discovery. The audit and two sibling
  tests now establish both child starts before checking that supervision preserves
  them. Assertions and runtime code are unchanged.
- An injected 12-second delay in the fake Samba child's startup log reproduced
  the original assertion failure. That delay also exceeded the existing 15-second
  total startup wait in one corrected case under load. With an eight-second
  injected delay, all four affected cases passed in 57.81 seconds; no test timeout
  was increased. Logs: `/tmp/tc-bonjour-cleanup-race-before.log` and
  `/tmp/tc-bonjour-cleanup-race-after.log`.
- Final `make test-parallel`: native host compile checks and all 3,252 Python tests
  passed in 188.31 seconds, with 44 Python 3.14 forkpty deprecation warnings.
  Log: `/tmp/tc-bonjour-cleanup-tests-final.log`. The initial failure is retained in
  `/tmp/tc-bonjour-cleanup-tests.log`.
- `swift test --package-path macos/TimeCapsuleSMB`: all 656 tests passed.
  Log: `/tmp/tc-bonjour-cleanup-swift.log`. Ruff and `git diff --check` passed.
- No VM or device access, deployment, or binary changes. No locks held; changes
  remain uncommitted.

## 2026-10-03 — macOS Bonjour CI failures

- CI run `37113981609` failed only in the macOS Python 3.9 and 3.12 Bonjour
  integration tests (11 and three failures respectively). Ubuntu, macOS Python
  3.14, Swift, native sanitizers, Samba regressions and packaging passed.
- Reproduced the missing IPv6 answer on local Python 3.9.6. The native fixture
  passed `time.monotonic()` epochs between processes, but that Python/macOS
  combination gives each process a separate clock origin. Both sides now use
  `clock_gettime(CLOCK_MONOTONIC)`. A regression with an offset parent clock
  failed before the fix and passes afterward.
- Remove/re-add ordering now uses a signal and observed browse events instead
  of short sleeps. The partial-answer retry test uses the existing two-second
  fixture browse window and covers delayed process launch, avoiding a 100 ms
  admission window shorter than CI process startup.
- Broader verification exposed duplicate link-local IPv6 addresses spelled with
  an interface number and its equivalent name. Snapshot merging now reuses
  `same_scoped_ip`; regressions verify equivalent scopes merge and distinct scopes
  remain separate. The equivalent-scope case failed before the fix.
- Python 3.9.6: all 2,351 tests in the CI non-native phase passed in 240.39 seconds
  (`/tmp/tc-ci-py39-full.log`). The 113 focused provider/completion tests also
  passed (`/tmp/tc-ci-py39-focused.log`).
- Python 3.14: `make test-parallel` passed native compile checks and all 3,257
  tests in 228.91 seconds, with 44 forkpty deprecation warnings
  (`/tmp/tc-ci-local-full.log`). All 656 Swift tests passed
  (`/tmp/tc-ci-swift.log`). Ruff and `git diff --check` passed.
- No VM/device access or binary changes; no locks held. Remote CI validation of
  this patch is pending.

## 2026-10-03 — macOS packaging timings and progress

- Timed the release/universal/ZIP/full-validation/Developer ID/notarization
  command with temporary instrumentation and a separate output directory.
  It succeeded in 349.11 seconds, including successful notarization, stapling,
  Gatekeeper assessment and verification of the extracted distributable ZIP.
  Existing `dist` output was preserved. Detailed subprocess and phase timings:
  `/var/folders/ng/mwbdswb919d7w5j7428_lm9c0000gn/T/tcapsule-package-profile-tjxevsmg/timings.json`.
- Swift app/helper builds took 67.64 seconds; cached Python copies 1.94 seconds;
  Python cleanup/ad-hoc signing/verification 11.52 seconds; cached native tool
  preparation/copying 1.08 seconds; bundle validation 65.87 seconds; Developer
  ID signing/verification 72.83 seconds; smoke tests 3.26 seconds; notarization
  archive 9.54 seconds; Apple submission/wait 100.12 seconds; stapling/Gatekeeper
  2.27 seconds; final ZIP creation/extraction/verification 11.92 seconds.
- The native cache message preceded about 253 seconds of silent work, ending
  at notarization acceptance. Across the run, 4,147 Mach-O inspection
  subprocesses included 2,580 repeated identical commands. There were 322
  Developer ID signing calls, taking 49.33 seconds before inspection and
  verification overhead. Secure timestamps and notarization remain enabled.
- A read-only experiment memoized Mach-O inspection results within one
  validation pass, without editing the packaging script. Validation of the
  signed app passed in 39.02 seconds, versus 65.87 seconds in the initial run.
  Follow-up plan: scope metadata reuse to immutable phases; sign independent
  binaries with a small worker pool before signing enclosing bundles; combine
  redundant signature checks around the final signing pass; build both Swift
  products together per architecture while preserving each architecture's
  products before Swift Build overwrites the shared output directory. These
  speed changes were not implemented in this initial investigation.
- Added flushed start/completion messages with monotonic elapsed seconds for
  packaging phases, including individual architecture builds, validation
  substeps, signing, Apple submission/wait, stapling and archive verification.
  Failure and interruption messages retain the original exception. Messages
  use separate stderr lines so streamed subprocess output stays readable.
- `.venv/bin/pytest tests/test_macos_package_app.py -q`: all 119 tests passed
  in 2.49 seconds, covering timing before work begins, elapsed time, failures,
  interruption, repeated decorated calls and notarization progress.
  Ruff and `git diff --check` passed. With the edited script, read-only full
  validation of the signed app passed in 66.0 seconds and extracted ZIP
  validation passed in 3.5 seconds, with each phase's timing visible.
- No VM/device access or binary changes; no locks held. Changes are uncommitted.

## 2026-10-03 — Faster macOS packaging

- Implemented all four planned improvements: Mach-O inspection reuse within
  immutable validation phases; four workers for independent Developer ID
  signing jobs; one verification pass after final signing; one Swift build and
  product-path query per architecture for both executable products.
- Metadata is process-local and discarded after each validation phase, even
  on failure. No cached inspection results survive signing or another build.
  The native tool layer's existing fingerprint checks are preserved.
- Worker signing excludes the app and Python framework main executables:
  `codesign` can treat these as their enclosing bundles. Both containers are
  signed after all leaves complete. The first experimental worker run failed
  when the main executable was included; the corrected ordering passed the
  end-to-end run. Signing failures prevent container signing. Secure timestamps,
  hardened runtime, full dependency/architecture/minimum macOS checks,
  notarization, stapling, Gatekeeper and extracted ZIP verification remain.
- Developer ID builds no longer ad-hoc sign and verify Python beforehand.
  Ad-hoc builds still sign Python after cleanup. Final signature verification
  now runs for both modes, including without `--full-validation`.
- Both Swift executables and resources are copied aside immediately after each
  architecture build, preserving correctness with Swift Build's shared product
  directory. Staging is cleared to avoid retaining removed resource bundles.
- The same release/universal/ZIP/full-validation/Developer ID/notarization
  command passed in 216.85 seconds, versus the initial 349.11 seconds.
  Apple's upload/processing wait was 75.04 seconds versus 100.12 seconds;
  excluding that variable wait, packaging fell from 248.99 to 141.81 seconds
  (43.0%). These are individual runs, not benchmark medians.
- Phase comparison: Swift 67.64 -> 51.72 seconds; Python finalization
  11.52 -> 0.06 seconds; signing plus final bundle validation
  138.70 -> 59.92 seconds. Inspection subprocesses fell from 4,147 to 1,956,
  Swift invocations from eight to four, and signature verification invocations
  from 768 to 320. Detailed final timings:
  `/private/var/folders/ng/mwbdswb919d7w5j7428_lm9c0000gn/T/tcapsule-package-optimized-cxz9ry8y/timings.json`.
- All 130 packaging tests passed in 1.71 seconds. Coverage includes scope exit
  and failure, empty inspection results, alias reuse, worker overlap/bounds,
  signing failures, container main-executable exclusion, both signing modes,
  architecture-specific staging, stale resources and missing products. Ruff,
  Python 3.9 import/scoping checks and `git diff --check` passed.
- The successful full run verified notarization, stapling, Gatekeeper, resource
  loading, Python imports, native tool execution and the extracted universal
  app ZIP. The existing `dist` output was preserved.
- The complete ad-hoc universal packaging path also passed in 37.7 seconds,
  using the freshly built Swift products without recompiling them. This run
  omitted `--full-validation` and notarization, and verified cleanup, ad-hoc
  signatures, final bundle validation, helper/tool smoke tests and ZIP extraction.
  Output: `/private/var/folders/ng/mwbdswb919d7w5j7428_lm9c0000gn/T/tcapsule-package-adhoc-l3jlbpzr`.
- No VM/device access or checked-in binary changes; no locks held. No commit.

## 2026-10-04 — Caching for repeated macOS packaging runs

- Timed the release/universal/ZIP/full-validation/Developer ID command on an
  unchanged tree without notarization: 129.0 seconds with every existing cache
  warm. Swift took 53.4 seconds, Developer ID signing 19.0 and final bundle
  validation 39.2; the rest was ZIP creation and verification.
- Swift: building arm64 and then x86_64 in one `.build` recompiled each
  architecture every run. Measured by hand: the same architecture again took
  0.4 seconds, but arm64 after x86_64 took 151 seconds and x86_64 after arm64
  30 seconds. Packaging now runs one `swift build --arch arm64 --arch x86_64`
  (universal products, no `lipo`) in `.build/package-swift/<architectures>`,
  apart from `swift test`'s `.build`. Products still declare macOS 14.0
  (`vtool`). A no-op build takes 2.2 seconds.
- Signing: 318 leaf Mach-O files were identical every run but each was re-signed
  with a secure timestamp. Their Developer ID signed bytes are now kept in
  `.build/package-app/developer-id-signatures`, keyed by the unsigned file's
  sha256, its name (the default identifier) and the certificate's SHA-1 from
  `security find-identity`. If the identity matches no keychain certificate
  or more than one, nothing is cached. The Python framework and the app are
  still signed every run.
- The first notarized run with reused signatures was rejected
  (submission 3b018d58-86c5-4c8e-829f-a6e5d7318828): `python.o` was "not signed".
  codesign keeps an object file's signature in extended attributes and leaves
  its bytes alone, so the copied bytes were unsigned. The cached verify pass
  for those same bytes hid it. A signed copy is now stored only when signing
  changed the file's bytes. A `codesign --verify` pass is kept only for a file
  with an embedded `LC_CODE_SIGNATURE`, and stored records follow the same rule.
- Validation: during a packaging run, the output of `lipo -archs`, `otool -L/-D/-l`
  and `vtool -show-build` is kept under each file's sha256 in
  `.build/package-app/macho-tools-v1`; passes of `codesign --verify` follow
  the rule above. The path inside the output is stored as a placeholder. Every
  check still runs on the current files; `--no-cache` turns both new caches
  off. Both caches keep `PACKAGE_CACHE_KEEP_ENTRIES` (4) runs' worth of
  entries, oldest removed first.
- Same command on the unchanged tree: 24.1 seconds, versus 129.0 (Swift 2.2,
  signing 1.4 with 318 of 318 reused, bundle validation 3.2). After a Python
  source change: 37.1 seconds, of which site-packages took 14.1. A cold run
  that built the new Swift scratch path and refilled the caches took 132.7 seconds.
- The user's full command with `--notarize` then passed in 125.0 seconds with
  317 of 318 signatures reused. Apple's wait took 88.9 seconds, and Apple
  accepted the app with no issues (submission 03bb71c9-dbb8-49a0-9cae-1bafc974d4f6).
  Stapling, Gatekeeper and verification of the extracted ZIP passed. Output
  went to a scratch directory, so `dist` was left alone.
- `pytest tests/test_macos_package_app.py`: 146 passed. New tests cover one
  universal build per architecture set and separate scratch paths. For the
  tool cache they cover reuse across runs until the bytes change, sharing
  between paths, unreadable records, eviction and `--no-cache`, and passes
  that are not kept. For signatures they cover reuse only with the same bytes,
  name and certificate; no reuse without a known certificate or without cache;
  object-file signatures; failed signing; and eviction. They also cover
  certificate lookup. Ruff passed, and the script still runs under Python 3.9.6.
- Not done: splitting site-packages into a dependency layer and the
  timecapsulesmb package (would save about 6 seconds after a Python change),
  and faster ZIP compression (level 1 halves the 10 seconds but adds 8%).
- No VM/device access or binary changes; no locks held.
