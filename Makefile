# Makefile for TimeCapsuleSMB — Deploy modern Samba onto Apple AirPort Time Capsules
# Targets are numbered by workflow stage:
#   install → 1 discover → 2 configure → 3 deploy → 4 activate/flash →
#   5 doctor → 6 fsck/repair → 7 uninstall → dev
SERVICE = TimeCapsuleSMB

# Variables
VENV_DIR = .venv
PY = $(VENV_DIR)/bin/python
PIP = $(VENV_DIR)/bin/pip
T = $(VENV_DIR)/bin/tcapsule

.PHONY: help install clean \
        discover validate-install \
        configure \
        deploy deploy-dry-run \
        activate flash-backup flash-patch flash-restore \
        doctor \
        fsck repair-xattrs \
        uninstall uninstall-dry-run \
        lint test test-parallel test-c coverage coverage-html set-ssh

# ── Environment ──────────────────────────────────────────────────────────────

help: ## Print this help message
	@printf '\033[01;32m${SERVICE} — Deploy modern Samba onto AirPort Time Capsules\033[00;37m\n\n'
	@printf "\033[33mUsage:\033[0m\n  make [target] [yes=1] [json=1] [dry=1] [no-reboot=1] [reboot=1]\n\n\033[33mTargets:\033[0m\n"
	@grep -E '^[-a-zA-Z0-9_\.\/]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; \
		{printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

install: ## Bootstrap the .venv (python deps, editable install, host tools)
	@if [ ! -d "$(VENV_DIR)" ]; then \
		./tcapsule bootstrap; \
	fi; \
	$(PIP) install -e ".[dev]"; \
	echo "Environment ready: $(T)"

clean: ## Remove .venv and all temporary/generated files
	rm -rf $(VENV_DIR)
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	@echo "Cleanup complete."

# ── Stage 1 · Discover (mDNS/Bonjour) ─────────────────────────────────────────

discover: install ## [STEP  1] List all mDNS/Bonjour devices on the network (json=1 for machine-readable output)
	@echo "Discovering devices..."
	$(T) discover $(if $(json),--json)

validate-install: install ## [STEP  1] Validate the local install (binaries, artifacts, venv state)
	$(T) validate-install $(if $(json),--json)

# ── Stage 2 · Configure (writes .env) ─────────────────────────────────────────

configure: install ## [STEP  2] Configure the device and write .env (yes=1 approves enabling SSH via ACP)
	@echo "Configuring device..."
	$(T) configure $(if $(yes),--yes)

# ── Stage 3 · Deploy ──────────────────────────────────────────────────────────

deploy: install ## [STEP  3] Deploy/update Samba onto the device (yes=1 skips reboot prompt; no-reboot=1 activates in place)
	@echo "Deploying to device..."
	$(T) deploy $(if $(yes),--yes) $(if $(no-reboot),--no-reboot)

deploy-dry-run: install ## [STEP  3] Print the deployment plan without changing the device (json=1 for machine-readable output)
	@echo "Dry-run deployment plan..."
	$(T) deploy --dry-run $(if $(json),--json)

# ── Stage 4 · Boot hook (NetBSD 4) — activate or flash ────────────────────────

activate: install ## [STEP  4] Start the deployed Samba runtime now (yes=1 skips the restart prompt)
	$(T) activate $(if $(yes),--yes)

flash-backup: install ## [STEP  4] Back up and inspect the firmware banks (no write; json=1 for machine-readable output)
	@echo "Backing up flash memory..."
	$(T) flash $(if $(json),--json)

flash-patch: install ## [STEP  4] Patch the primary firmware bank LOGIN boot hook (yes=1 skips the write prompt)
	@echo "Patching firmware boot hook..."
	$(T) flash --patch $(if $(yes),--yes)

flash-restore: install ## [STEP  4] Restore a firmware bank from Apple stock firmware (yes=1 skips the write prompt; reboot=1 reboots after)
	@echo "Restoring firmware bank..."
	$(T) flash --restore $(if $(yes),--yes) $(if $(reboot),--reboot)

# ── Stage 5 · Doctor (verify the result) ──────────────────────────────────────

doctor: install ## [STEP  5] Non-destructive diagnostic of the deployed runtime (json=1 for machine-readable output)
	$(T) doctor $(if $(json),--json)

# ── Stage 6 · Maintenance (fsck / xattr repair) ───────────────────────────────

fsck: install ## [STEP  6] Repair the internal disk before deploy (yes=1 skips the prompt; no-reboot=1 runs fsck only)
	$(T) fsck $(if $(yes),--yes) $(if $(no-reboot),--no-reboot)

repair-xattrs: install ## [STEP  6] Repair broken xattrs on the disk (yes=1 repairs without prompting; dry=1 only scans)
	$(T) repair-xattrs $(if $(yes),--yes) $(if $(dry),--dry-run)

# ── Stage 7 · Uninstall ───────────────────────────────────────────────────────

uninstall: install ## [STEP  7] Remove TimeCapsuleSMB from the device (yes=1 skips the reboot prompt; no-reboot=1 keeps it running)
	$(T) uninstall $(if $(yes),--yes) $(if $(no-reboot),--no-reboot)

uninstall-dry-run: install ## [STEP  7] Print the uninstall plan without changing the device (json=1 for machine-readable output)
	@echo "Dry-run uninstall plan..."
	$(T) uninstall --dry-run $(if $(json),--json)

# ── Development ───────────────────────────────────────────────────────────────

lint: install ## Run the Ruff linter (py39 target, E9/F63/F7/F82 only)
	$(PY) -m ruff check src tests macos/TimeCapsuleSMB/tools tcapsule

test: install test-c ## Run the pytest unit test suite (single process)
	$(PY) -m pytest -q

test-parallel: install test-c ## Run the pytest suite with xdist (C compile checks + pytest -n auto --dist loadfile)
	$(PY) -m pytest -n auto --dist loadfile

test-c: ## Compile-check the mdns/nbns advertiser C sources (outputs to /tmp)
	cc -Wall -Wextra -Werror -o /tmp/mdns-advertiser-test build/mdns-advertiser.c
	cc -Wall -Wextra -Werror -o /tmp/nbns-advertiser-test build/nbns-advertiser.c

coverage: install ## Run tests with coverage and show missing lines
	$(PY) -m coverage run -m pytest
	$(PY) -m coverage report

coverage-html: coverage ## Write an HTML coverage report to htmlcov/
	$(PY) -m coverage html
	@echo "Open htmlcov/index.html to inspect line-by-line coverage."

set-ssh: install ## Advanced SSH enable/disable helper (yes=1 skips the legacy prompt)
	$(T) set-ssh $(if $(yes),--yes)
