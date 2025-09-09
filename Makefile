. # TimeCapsuleSMB Makefile
. #
. # Quick start:
. #   1) make discover        # creates venv, installs deps, runs discovery
. #   2) source .venv/bin/activate  # use Python tools manually
. #
. # Targets:
. #   make venv      - create local virtualenv at .venv
. #   make install   - install Python dependencies into .venv
. #   make discover  - run discovery/discover_timecapsules.py (depends on install)
. #   make clean     - remove the .venv directory

.PHONY: venv install discover clean

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

discover: install
	$(PY) discovery/discover_timecapsules.py

clean:
	rm -rf $(VENVDIR)
