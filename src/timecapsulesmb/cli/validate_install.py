from __future__ import annotations

import argparse
import json
from typing import Optional

from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.install_validation import install_checks_to_jsonable, install_ok, validate_install


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the local TimeCapsuleSMB repo-only install.")
    parser.add_argument("--json", action="store_true", help="Output validation results as JSON")
    args = parser.parse_args(argv)

    app_paths = resolve_app_paths()
    checks = validate_install(app_paths)
    ok = install_ok(checks)
    if args.json:
        print(json.dumps({
            "ok": ok,
            "checks": install_checks_to_jsonable(checks),
        }, indent=2, sort_keys=True))
        return 0 if ok else 1

    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.message}")
    print("Summary: install validation passed." if ok else "Summary: install validation failed.")
    return 0 if ok else 1
