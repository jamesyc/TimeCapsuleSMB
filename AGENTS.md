# AGENTS.md

TimeCapsuleSMB deploys a modern Samba 4 server onto Apple AirPort Time Capsules (NetBSD 6 / NetBSD 4 firmware). Two parts: a Python CLI (`src/timecapsulesmb/`, package `timecapsulesmb`) and a SwiftUI macOS GUI (`macos/TimeCapsuleSMB/`). The Python side is what actually talks to devices; the GUI is a front end for it.

> **Active rewrite:** this repo is the frozen behavior oracle of a clean-architecture rewrite that now lives at `https://github.com/felipe-dos-santos81/apple-time-capsule-utils` (workflow: `AGENTS-REWRITE.md`; CI green across Python 3.12/3.14 + Swift as of 2026-08-10). Do not restructure this repo's architecture or rename public symbols — the rewrite's characterization suite pins them. Fix bugs here if they affect the rewrite, but keep changes scoped.

## Setup and verification

- `./tcapsule bootstrap` is the **only** repo-root launcher command. It builds `.venv` and installs the `tcapsule` console script. After that, always use `.venv/bin/tcapsule ...` — the root `tcapsule` script just `execv`s the venv binary and refuses anything else (tcapsule:51).
- **Gotcha:** `./tcapsule bootstrap` installs base deps only — ruff, pytest-xdist, and coverage come from the `[dev]` extras (`pyproject.toml`). `make lint`/`make test` fail with `No module named ruff` until `.venv/bin/pip install -e ".[dev]"`. `make install` does both (bootstrap + dev extras) and is a prerequisite of every other Makefile target.
- Full verification: `make test-parallel` (C compile checks + pytest-xdist with `--dist loadfile`). For focused debugging use single-process `.venv/bin/pytest tests/test_foo.py`.
- Device workflow stages are Makefile targets, numbered by stage: `make install` → `discover`/`validate-install` → `configure` → `deploy`/`deploy-dry-run` → `activate`/`flash-backup`/`flash-patch`/`flash-restore` → `doctor` → `fsck`/`repair-xattrs` → `uninstall`/`uninstall-dry-run` → `set-ssh` (`make help` lists all). Variables: `yes=1`/`json=1`/`dry=1` pass `--yes`/`--json`/`--dry-run`; deploy/fsck/uninstall also accept `no-reboot=1`; `flash-restore` accepts `reboot=1`. `make test`/`test-parallel` also run `test-c`.
- Lint: `make lint` (ruff on `src tests macos/TimeCapsuleSMB/tools tcapsule`; config: py39 target, line-length 100, only E9/F63/F7/F82 selected).
- Swift tests are macOS-only: `swift test --package-path macos/TimeCapsuleSMB`. **Environment quirk:** requires full Xcode — machines with only CommandLineTools fail with `no such module 'XCTest'`; protocol-level characterization of the `tcapsule api` backend is the fallback.
- If any checked-in binary under `bin/` changes, update `src/timecapsulesmb/assets/artifact-manifest.json` and run `pytest tests/test_artifacts.py tests/test_artifact_resolver.py`.
- CI matrix (`.github/workflows/ci.yml`): Ubuntu+macOS × Python 3.9/3.12/3.14, plus a Swift test job and a `package_app.py` packaging job.

## Architecture (non-obvious wiring)

- Entry point `src/timecapsulesmb/cli/main.py` dispatches one module per command (`deploy.py`, `flash.py`, `doctor.py`, `configure.py`, ...) sharing `cli/context.py` (CommandContext). Every command except `api` runs a version gate that fetches GitHub (3h cache) before dispatch.
- The macOS app never talks to devices directly. `Backend/BackendClient.swift` spawns the Python `tcapsule api` backend (`cli/api.py` → `app/helper.py` → `app/service.py`) as a subprocess speaking a JSON-lines protocol over pipes (`app/events.py`, `app/requests.py`). `TCAPSULE_HELPER` env overrides the helper path. Import-boundary tests enforce `app/` must not import `cli/` (tests/test_import_boundaries.py).
- On-device runtime is POSIX sh under `src/timecapsulesmb/assets/boot/samba4/`: `manager.sh` orchestrates numbered `common.d/*.sh` steps. These files are copied to real devices — changes run on the Time Capsule. Host-side flows and the on-device manager are two parallel state machines that agree only via string-matched log lines, exit codes, and fixed paths.
- `bin/` holds prebuilt statically-compiled NetBSD binaries (smbd, mdns/nbns advertisers, rsync) for three families: `netbsd6` (NetBSD 6), `netbsd4le`/`netbsd4be` (NetBSD 4 little/big endian). Rebuilding requires the NetBSD VM flow under `build/` (takes hours) — do not rebuild casually.
- `build/mdns-advertiser.c` and `build/nbns-advertiser.c` are compile-checked only (`make test-c` compiles them to /tmp).

## Device constraints (mandatory for on-device code)

- No pthread on the device. NetBSD 4 has a tiny userspace: do not assume `awk`, `grep`, `tr`, `cut`, `wc`, or `scp` exist on-device.
- Runtime shell helpers must use explicit `|| return 1` checks — NetBSD 4 `/bin/sh` does not reliably enforce `set -e` inside functions called from conditional contexts.
- Storage: `/mnt/Flash` is persistent but ~900KB (only small loader scripts live there); `/mnt/Memory` is a 16MB ramdisk Samba runs from; the internal HDD is large but unmounts when idle. Avoid new runtime state files unless truly required across process boundaries.

## Conventions and gotchas

- `tcapsule configure` writes `.env` in the repo root (gitignored) with `TC_HOST`/`TC_PASSWORD`. Never commit `.env`, device logs, or logs containing passwords/IPs/serial numbers.
- Telemetry is on by default; `tests/conftest.py` blocks unmocked telemetry posts.
- Keep changes scoped to one behavior; do not remove doctor tests (CONTRIBUTING.md).
- The big fixture-heavy test files hold most behavior tests — prefer adding cases there over new per-module files: `tests/test_cli.py` (~8900 lines), `tests/test_deploy_modules.py` (~8900), `tests/test_storage_runtime.py` (~7000, on-device pure-shell MaSt parser vs golden fixtures), `tests/test_app_api.py` (~4700, the api protocol contract pin), `tests/test_checks.py` (~5500, doctor checks).
- Device behavior is faked per-test via `mock.patch` of `run_ssh`/transport functions; there is no shared fake-device class. `tests/storage_fixtures.py` holds golden MaSt outputs.

## Graphify helper

A prebuilt knowledge graph of this repo lives in `graphify-out/` (not committed; regenerate with the /graphify skill). For architecture questions run `graphify query "<question>"`; `graphify-out/GRAPH_REPORT.md` lists communities, god nodes, and surprising connections, and `graph.html` is the interactive view. The graph is rebuilt from the current tree, so use it for orientation but verify against code before making changes.
