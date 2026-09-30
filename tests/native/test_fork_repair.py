"""The fork() repair for Apple's NetBSD 6 kernel (Samba patch 0070's overlay
file, also linked into the service and rsync): its registry of private
mappings, the repair of ranges the test maps itself, and the fork() wrapper.
The kernel bug is exercised on the device by tests/samba/tc_fork_repair_test.c."""
from pathlib import Path
import subprocess

import pytest

from tests.native.build import compile_modules

CASES = (
    "map_records_private_only", "map_fixed_replaces", "unmap_splits_and_trims", "merge_adjacent",
    "remap_moves", "remap_drops_replaced", "protect_updates", "overflow_falls_back", "fallback_lock_failure",
    "repair_keeps_contents", "fork_mask_and_errno", "advice_restored", "advice_partial_repair",
    "read_only_ranges_skipped", "updates_keep_mask_and_errno",
)


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    output = tmp_path_factory.mktemp("fork_repair") / "fork_repair"
    return compile_modules(output, ("patches/samba4x/overlay/lib/replace/tc_fork_repair.c",),
                           flags=("-DTC_FORK_REPAIR=1", "-DTC_FORK_REPAIR_TEST=1"),
                           extra_sources=(Path(__file__).parent / "unit/test_fork_repair.c",))


@pytest.mark.parametrize("case", CASES)
def test_fork_repair(binary, case):
    run = subprocess.run([str(binary), case], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == f"PASS {case}"


def test_every_case_is_listed(binary):
    # The driver runs all cases when given none; each must be one pytest runs.
    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert tuple(line.split()[1] for line in run.stdout.splitlines()) == CASES
