# TimeCapsuleSMB Makefile
#
# Prerequisites (macOS):
#   - Homebrew: https://brew.sh
#   - pyenv:    brew install pyenv
#   - Build deps: brew install zlib bzip2 readline
#
# Quick start:
#   1) make install           # create venv and install Python 3 deps
#   2) make airpyrt           # provision pyenv Python 2.7.18 + local AirPyrt venv
#   3) make discover          # run mDNS discovery to list devices
#   4) make setup             # select device; enables SSH via AirPyrt if needed
#      (or run: python setup.py)
#
# Targets:
#   make venv                    - create local virtualenv at .venv
#   make install                 - install Python dependencies into .venv
#   make discover                - run discovery/discover_timecapsules.py (depends on install)
#   make airpyrt                 - clone+install AirPyrt into local .airpyrt-venv (via pyenv Python 2)
#   make airpyrt-clone           - clone AirPyrt repo only
#   make airpyrt-bootstrap       - ensure pyenv Python 2.7.18 is installed
#   make airpyrt-venv            - create local Python 2 venv from pyenv version
#   make airpyrt-install         - install AirPyrt into .airpyrt-venv
#   make clean                   - remove the .venv directory
#   make airpyrt-clean           - remove the local .airpyrt-venv
#   make airpyrt-uninstall-pyenv - uninstall the pyenv Python 2.7.18 interpreter

.PHONY: venv install discover setup clean \
        airpyrt airpyrt-clone airpyrt-bootstrap airpyrt-venv airpyrt-install airpyrt-clean airpyrt-uninstall-pyenv

VENVDIR := .venv
PYTHON := python3
PIP := $(VENVDIR)/bin/pip
PY := $(VENVDIR)/bin/python

venv:
	$(PYTHON) -m venv $(VENVDIR)
	@echo "Run: source $(VENVDIR)/bin/activate"

install: venv
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt
	@echo "Optional: run 'make airpyrt' to install AirPyrt (Python 2)."

discover: install
	$(PY) discovery/discover_timecapsules.py

setup: install
	$(PY) setup.py

clean: airpyrt-clean
	rm -rf $(VENVDIR)

# --- AirPyrt (acp) installation helpers ---
# AirPyrt is Python 2.x-based. We isolate it in .airpyrt-venv using pyenv's Python 2.7.18.
DEPSDIR := .deps
AIRPYRT_DIR := $(DEPSDIR)/airpyrt-tools
AIRPYRT_ENV := .airpyrt-venv
AIRPYRT_PY := $(AIRPYRT_ENV)/bin/python
AIRPYRT_PIP := $(AIRPYRT_ENV)/bin/pip

PYENV_VERSION := 2.7.18
PYENV_BIN := $(shell command -v pyenv 2>/dev/null)
PYENV_ROOT := $(shell pyenv root 2>/dev/null)
PYENV_PY2 := $(PYENV_ROOT)/versions/$(PYENV_VERSION)/bin/python2.7

# Homebrew prefixes for build deps (zlib, bzip2, readline)
BREW := $(shell command -v brew 2>/dev/null)
ZLIB_PREFIX := $(shell brew --prefix zlib 2>/dev/null)
BZIP2_PREFIX := $(shell brew --prefix bzip2 2>/dev/null)
READLINE_PREFIX := $(shell brew --prefix readline 2>/dev/null)

# Pin virtualenv to a version that can create Python 2.7 envs
VIRTUALENV_VERSION := 20.16.7

airpyrt: airpyrt-clone airpyrt-bootstrap airpyrt-venv airpyrt-install ## Clone and install AirPyrt into local venv

airpyrt-clone:
	@mkdir -p $(DEPSDIR)
	@if [ ! -d $(AIRPYRT_DIR) ]; then \
		git clone https://github.com/x56/airpyrt-tools.git $(AIRPYRT_DIR); \
	else \
		printf "airpyrt-tools already cloned at $(AIRPYRT_DIR)\n"; \
	fi

airpyrt-bootstrap:
	@if [ -z "$(PYENV_BIN)" ]; then \
		echo "pyenv not found. Please 'brew install pyenv' and ensure it is on PATH."; \
		echo "Also initialize it in your shell (e.g., eval \"$$(pyenv init -)\")."; \
		exit 1; \
	fi
	@if [ -z "$(BREW)" ]; then \
		echo "Homebrew not found. Install from https://brew.sh to provide build deps (zlib, bzip2, readline)."; \
		exit 1; \
	fi
	@if [ ! -d "$(ZLIB_PREFIX)" ]; then \
		echo "Missing zlib. Run: brew install zlib"; \
		exit 1; \
	fi
	@if [ ! -d "$(BZIP2_PREFIX)" ]; then \
		echo "Missing bzip2. Run: brew install bzip2"; \
		exit 1; \
	fi
	@if [ ! -d "$(READLINE_PREFIX)" ]; then \
		echo "Missing readline. Run: brew install readline"; \
		exit 1; \
	fi
	@if ! pyenv versions --bare | grep -qx "$(PYENV_VERSION)"; then \
		echo "Installing Python $(PYENV_VERSION) via pyenv (one-time)..."; \
		env \
			LDFLAGS="-L$(ZLIB_PREFIX)/lib -L$(BZIP2_PREFIX)/lib -L$(READLINE_PREFIX)/lib" \
			CPPFLAGS="-I$(ZLIB_PREFIX)/include -I$(BZIP2_PREFIX)/include -I$(READLINE_PREFIX)/include" \
			PKG_CONFIG_PATH="$(ZLIB_PREFIX)/lib/pkgconfig:$(BZIP2_PREFIX)/lib/pkgconfig:$(READLINE_PREFIX)/lib/pkgconfig" \
			pyenv install -s $(PYENV_VERSION); \
	else \
		echo "pyenv Python $(PYENV_VERSION) already installed"; \
	fi

airpyrt-venv: install airpyrt-bootstrap
	@echo "Creating local AirPyrt venv at $(AIRPYRT_ENV) using $(PYENV_PY2)"
	@$(PIP) install -q "virtualenv==$(VIRTUALENV_VERSION)"
	@if [ ! -d $(AIRPYRT_ENV) ]; then \
		$(VENVDIR)/bin/virtualenv -p $(PYENV_PY2) $(AIRPYRT_ENV); \
	else \
		echo "$(AIRPYRT_ENV) already exists"; \
	fi

airpyrt-install: airpyrt-clone airpyrt-venv
	@echo "Installing AirPyrt into $(AIRPYRT_ENV)"
	@if [ ! -x "$(AIRPYRT_PIP)" ]; then \
		if [ -x "$(AIRPYRT_PY)" ]; then \
			echo "pip not found in venv; bootstrapping via python -m pip"; \
			$(AIRPYRT_PY) -m ensurepip || true; \
			$(AIRPYRT_PY) -m pip install --upgrade pip; \
		else \
			echo "AirPyrt venv python missing at $(AIRPYRT_PY)"; \
			exit 1; \
		fi; \
	fi
	$(AIRPYRT_PIP) install $(AIRPYRT_DIR)
	@echo "Done. Use AIRPYRT_PY=$(AIRPYRT_PY) when running setup.py, or add $(AIRPYRT_ENV)/bin to PATH."

airpyrt-clean:
	rm -rf $(AIRPYRT_ENV)
	@echo "Removed $(AIRPYRT_ENV)."

airpyrt-uninstall-pyenv:
	@if [ -z "$(PYENV_BIN)" ]; then \
		echo "pyenv not found; skipping uninstall."; \
		exit 0; \
	fi
	@if ! pyenv versions --bare | grep -qx "$(PYENV_VERSION)"; then \
		echo "pyenv Python $(PYENV_VERSION) not installed; skipping."; \
		exit 0; \
	fi
	pyenv uninstall -f $(PYENV_VERSION)
	@echo "Uninstalled pyenv Python $(PYENV_VERSION)."
