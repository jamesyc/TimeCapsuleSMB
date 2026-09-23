# TimeCapsuleSMB Makefile
#
# Prerequisites:
#   - macOS or Linux: Python 3 and smbclient for configure/deploy/doctor
#
# Quick start:
#   1) ./tcapsule bootstrap
#   2) .venv/bin/tcapsule configure
#   3) .venv/bin/tcapsule deploy
#   4) .venv/bin/tcapsule doctor
#
# Targets:
#   make venv                    - create local virtualenv at .venv
#   make install                 - install Python dependencies when inputs change
#   make lint                    - run Ruff against Python sources and tests
#   make test                    - run C compile checks and Python pytest suite
#   make test-parallel           - run C compile checks and pytest-xdist suite
#   make coverage                - run Python tests with coverage and show missing lines
#   make coverage-html           - write an HTML coverage report to htmlcov/
#   make coverage-native         - report native C coverage with LLVM tools
#   make test-c                  - compile-check all four native helpers
#   make discover                - run tcapsule discover (depends on install)
#   make bootstrap-host          - run the host bootstrap helper
#   make set-ssh                 - advanced SSH toggle helper
#   make clean                   - remove the .venv directory

.PHONY: venv install lint test test-parallel coverage coverage-html coverage-native test-c discover bootstrap-host set-ssh setup clean

VENVDIR := .venv
PYTHON := python3
PIP := $(VENVDIR)/bin/pip
PY := $(VENVDIR)/bin/python
# Editable installs see source edits immediately; dependency inputs need pip again.
DEPS_STAMP := $(VENVDIR)/.deps-installed

$(PY):
	$(PYTHON) -m venv $(VENVDIR)

venv: $(PY)
	@echo "Run: source $(VENVDIR)/bin/activate"

$(DEPS_STAMP): $(PY) pyproject.toml requirements.txt
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	$(PIP) install -e ".[dev]"
	@touch $@

install: $(DEPS_STAMP)

lint: install
	$(PY) -m ruff check src tests macos/TimeCapsuleSMB/tools tcapsule

test: install test-c
	@# Native test children close descriptors up to the host soft limit, which can exceed a million.
	ulimit -n 256; $(PY) -m pytest

test-parallel: install test-c
	ulimit -n 256; $(PY) -m pytest -n auto --dist worksteal

coverage: install
	ulimit -n 256; $(PY) -m coverage run -m pytest
	$(PY) -m coverage report

coverage-native:
	$(PY) -m tests.native.coverage

coverage-html: coverage
	$(PY) -m coverage html
	@echo "Open htmlcov/index.html to inspect line-by-line coverage."

test-c:
	./build/native/host-check.sh

discover: install
	$(VENVDIR)/bin/tcapsule discover

bootstrap-host:
	./tcapsule bootstrap

set-ssh: install
	$(VENVDIR)/bin/tcapsule set-ssh

setup: install

clean:
	rm -rf $(VENVDIR)
