# TimeCapsuleSMB Makefile
#
# Prerequisites:
#   - macOS: Homebrew recommended, plus brew install zlib bzip2 readline for AirPyrt
#   - Linux: python3 and smbclient are enough for configure/deploy/doctor if SSH is already enabled
#
# Quick start:
#   1) ./tcapsule bootstrap
#   2) .venv/bin/tcapsule prep-device
#   3) .venv/bin/tcapsule configure
#
# Targets:
#   make venv                    - create local virtualenv at .venv
#   make install                 - install Python dependencies into .venv
#   make test                    - run C compile checks and Python unittest suite
#   make coverage                - run Python tests with coverage and show missing lines
#   make coverage-html           - write an HTML coverage report to htmlcov/
#   make test-c                  - compile-check mdns/nbns helper sources
#   make discover                - run tcapsule discover (depends on install)
#   make bootstrap-host          - run the host bootstrap helper
#   make prep-device             - discover the device and enable SSH if needed
#   make airpyrt                 - clone+install AirPyrt into local .airpyrt-venv (macOS/Homebrew-oriented helper)
#   make airpyrt-clone           - clone AirPyrt repo only
#   make airpyrt-bootstrap       - ensure pyenv Python 2.7.18 is installed
#   make airpyrt-venv            - create local Python 2 venv from pyenv version
#   make airpyrt-install         - install AirPyrt into .airpyrt-venv
#   make clean                   - remove the .venv directory
#   make airpyrt-clean           - remove the local .airpyrt-venv
#   make airpyrt-uninstall-pyenv - uninstall the pyenv Python 2.7.18 interpreter

.PHONY: venv install test coverage coverage-html test-c discover bootstrap-host prep-device setup clean \
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
	$(PIP) install -e .
	@echo "Optional: run 'make airpyrt' to install AirPyrt (Python 2)."

test: install test-c
	PYTHONPATH=src $(PY) -m unittest discover -s tests -v

coverage: install
	PYTHONPATH=src $(PY) -m coverage run -m unittest discover -s tests -v
	$(PY) -m coverage report

coverage-html: coverage
	$(PY) -m coverage html
	@echo "Open htmlcov/index.html to inspect line-by-line coverage."

test-c:
	cc -Wall -Wextra -Werror -o /tmp/mdns-advertiser-test build/mdns-advertiser.c
	cc -Wall -Wextra -Werror -o /tmp/nbns-advertiser-test build/nbns-advertiser.c

discover: install
	$(VENVDIR)/bin/tcapsule discover

bootstrap-host:
	./tcapsule bootstrap

prep-device: install
	$(VENVDIR)/bin/tcapsule prep-device

setup: prep-device

clean: airpyrt-clean
	rm -rf $(VENVDIR)

# --- AirPyrt (acp) installation helpers ---
# AirPyrt is Python 2.x-based. We isolate it in .airpyrt-venv using pyenv's Python 2.7.18.
# pyenv does not need shell initialization here because we invoke the binary directly.
# These helpers are primarily for the macOS/Homebrew path. Linux users should prefer
# running deploy/doctor directly if SSH is already enabled on the Time Capsule.
DEPSDIR := .deps
AIRPYRT_DIR := $(DEPSDIR)/airpyrt-tools
AIRPYRT_ENV := .airpyrt-venv
AIRPYRT_PY := $(AIRPYRT_ENV)/bin/python
AIRPYRT_PIP := $(AIRPYRT_ENV)/bin/pip

PYENV_VERSION := 2.7.18
BREW := $(shell command -v brew 2>/dev/null)
PYENV_BIN := $(shell command -v pyenv 2>/dev/null || if [ -x "$$(brew --prefix pyenv 2>/dev/null)/bin/pyenv" ]; then printf "%s\n" "$$(brew --prefix pyenv 2>/dev/null)/bin/pyenv"; fi)

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
	@if [ -z "$(BREW)" ]; then \
		echo "Homebrew not found. Install from https://brew.sh to provide build deps (zlib, bzip2, readline)."; \
		exit 1; \
	fi
	@pyenv_bin="$(PYENV_BIN)"; \
	if [ -z "$$pyenv_bin" ]; then \
		echo "pyenv not found. Installing it via Homebrew (one-time)..."; \
		echo "This may take a minute or two depending on your network and Homebrew state."; \
		"$(BREW)" install pyenv; \
		pyenv_bin="$$( "$(BREW)" --prefix pyenv )/bin/pyenv"; \
	fi; \
	if [ ! -x "$$pyenv_bin" ]; then \
		echo "pyenv install did not produce a usable binary."; \
		exit 1; \
	fi
	@pyenv_bin="$(PYENV_BIN)"; \
	if [ -z "$$pyenv_bin" ]; then \
		pyenv_bin="$$( "$(BREW)" --prefix pyenv )/bin/pyenv"; \
	fi; \
	openssl_prefix="$$( "$(BREW)" --prefix openssl@3 2>/dev/null || true )"; \
	if [ ! -d "$$openssl_prefix" ]; then \
		echo "Missing openssl@3. Installing it via Homebrew (one-time)..."; \
		"$(BREW)" install openssl@3; \
		openssl_prefix="$$( "$(BREW)" --prefix openssl@3 )"; \
	fi; \
	zlib_prefix="$$( "$(BREW)" --prefix zlib 2>/dev/null || true )"; \
	if [ ! -d "$$zlib_prefix" ]; then \
		echo "Missing zlib. Installing it via Homebrew (one-time)..."; \
		"$(BREW)" install zlib; \
		zlib_prefix="$$( "$(BREW)" --prefix zlib )"; \
	fi; \
	bzip2_prefix="$$( "$(BREW)" --prefix bzip2 2>/dev/null || true )"; \
	if [ ! -d "$$bzip2_prefix" ]; then \
		echo "Missing bzip2. Installing it via Homebrew (one-time)..."; \
		"$(BREW)" install bzip2; \
		bzip2_prefix="$$( "$(BREW)" --prefix bzip2 )"; \
	fi; \
	readline_prefix="$$( "$(BREW)" --prefix readline 2>/dev/null || true )"; \
	if [ ! -d "$$readline_prefix" ]; then \
		echo "Missing readline. Installing it via Homebrew (one-time)..."; \
		"$(BREW)" install readline; \
		readline_prefix="$$( "$(BREW)" --prefix readline )"; \
	fi; \
	if ! "$$pyenv_bin" versions --bare | grep -qx "$(PYENV_VERSION)"; then \
		echo "Installing Python $(PYENV_VERSION) via pyenv (one-time)..."; \
		echo "This is the slowest AirPyrt step and can take several minutes while Python 2.7.18 builds."; \
		env \
			PYTHON_BUILD_HOMEBREW_OPENSSL_FORMULA="openssl@3 openssl" \
			LDFLAGS="-L$$zlib_prefix/lib -L$$bzip2_prefix/lib -L$$readline_prefix/lib" \
			CPPFLAGS="-I$$zlib_prefix/include -I$$bzip2_prefix/include -I$$readline_prefix/include" \
			PKG_CONFIG_PATH="$$openssl_prefix/lib/pkgconfig:$$zlib_prefix/lib/pkgconfig:$$bzip2_prefix/lib/pkgconfig:$$readline_prefix/lib/pkgconfig" \
			"$$pyenv_bin" install -s $(PYENV_VERSION); \
	else \
		echo "pyenv Python $(PYENV_VERSION) already installed"; \
	fi

airpyrt-venv: install airpyrt-bootstrap
	@$(PIP) install -q "virtualenv==$(VIRTUALENV_VERSION)"
	@pyenv_bin="$(PYENV_BIN)"; \
	if [ -z "$$pyenv_bin" ]; then \
		pyenv_bin="$$( "$(BREW)" --prefix pyenv )/bin/pyenv"; \
	fi; \
	pyenv_root="$$( "$$pyenv_bin" root )"; \
	pyenv_py2="$$pyenv_root/versions/$(PYENV_VERSION)/bin/python2.7"; \
	echo "Creating local AirPyrt venv at $(AIRPYRT_ENV) using $$pyenv_py2"; \
	if [ ! -x "$$pyenv_py2" ]; then \
		echo "Missing Python $(PYENV_VERSION) at $$pyenv_py2"; \
		exit 1; \
	fi; \
	if [ ! -d $(AIRPYRT_ENV) ]; then \
		$(VENVDIR)/bin/virtualenv -p "$$pyenv_py2" $(AIRPYRT_ENV); \
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
	@echo "Done. Use AIRPYRT_PY=$(AIRPYRT_PY) when running '.venv/bin/tcapsule prep-device', or add $(AIRPYRT_ENV)/bin to PATH."

airpyrt-clean:
	rm -rf $(AIRPYRT_ENV)
	@echo "Removed $(AIRPYRT_ENV)."

airpyrt-uninstall-pyenv:
	@if [ -z "$(PYENV_BIN)" ]; then \
		echo "pyenv not found; skipping uninstall."; \
		exit 0; \
	fi
	@if ! "$(PYENV_BIN)" versions --bare | grep -qx "$(PYENV_VERSION)"; then \
		echo "pyenv Python $(PYENV_VERSION) not installed; skipping."; \
		exit 0; \
	fi
	"$(PYENV_BIN)" uninstall -f $(PYENV_VERSION)
	@echo "Uninstalled pyenv Python $(PYENV_VERSION)."
