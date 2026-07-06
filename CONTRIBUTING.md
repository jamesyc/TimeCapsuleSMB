# Contributing

Thanks for helping improve TimeCapsuleSMB. This project touches real device firmware, checked-in static binaries, and old NetBSD targets, so small, well-tested changes are preferred.

## Local Setup

From the repository root:

```bash
./tcapsule bootstrap
.venv/bin/tcapsule validate-install
```

Use the repo-local virtualenv for development commands:

```bash
.venv/bin/pytest
```

For a full local verification run, prefer:

```bash
make test-parallel
```

Use single-process pytest for focused debugging when xdist makes a failure harder to inspect:

```bash
.venv/bin/pytest tests/test_some_area.py
```

## Pull Requests

- Keep changes scoped to one behavior or maintenance task.
- Include tests for new behavior and meaningful edge cases.
- Do not remove doctor tests.
- Do not commit local secrets, `.env` files, virtualenvs, generated build directories, or device logs with passwords, public IPs, or serial numbers.
- If you change package metadata or repository automation, validate the changed config before opening the PR.

## NetBSD Artifacts

Changes under `build/` may require rebuilding the affected artifact on the NetBSD VM before the work is complete. Do not rebuild the NetBSD toolchains unless that is explicitly required; they are already prebuilt and take hours to recreate.

If you change Samba source patches or build scripts, add a short explanatory comment for non-obvious compatibility work. The target devices are old and constrained, so future readers need to know whether a line is working around NetBSD 4, NetBSD 6, static linking, missing pthread support, ramdisk limits, or tiny userspace constraints.

Checked-in binaries under `bin/` are used directly by deploy flows. If a binary changes, update the artifact manifest and run the artifact tests:

```bash
.venv/bin/pytest tests/test_artifacts.py tests/test_artifact_resolver.py
```

## Device-Specific Notes

- Time Capsule targets do not support pthread.
- NetBSD 4 devices have a very small userspace; avoid assuming tools like `awk`, `grep`, `tr`, `cut`, `wc`, or `scp` exist on-device.
- Runtime shell helpers must explicitly check critical commands with `|| return 1`; NetBSD 4 `/bin/sh` does not reliably enforce `set -e` inside functions called from conditional contexts.
- Avoid adding runtime state files unless they are truly required across process boundaries and the reason is documented.

## Reporting Bugs

Use the GitHub issue templates. For doctor failures, attach sanitized output from:

```bash
.venv/bin/tcapsule doctor --json
```

Remove passwords, public IPs, and serial numbers before posting logs.
