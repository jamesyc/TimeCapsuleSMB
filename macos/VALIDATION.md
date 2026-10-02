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
