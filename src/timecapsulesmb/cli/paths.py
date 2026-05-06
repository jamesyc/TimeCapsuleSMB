from __future__ import annotations

import argparse
import json
from typing import Optional

from timecapsulesmb.core.paths import resolve_app_paths
from timecapsulesmb.install_validation import paths_to_jsonable


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Show TimeCapsuleSMB local path resolution.")
    parser.add_argument("--json", action="store_true", help="Output paths as JSON")
    args = parser.parse_args(argv)

    app_paths = resolve_app_paths()
    data = paths_to_jsonable(app_paths)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    print(f"distribution root: {data['distribution_root']}")
    print(f"config path: {data['config_path']}")
    print(f"state dir: {data['state_dir']}")
    print(f"package root: {data['package_root']}")
    print(f"artifact manifest: {data['artifact_manifest']}")
    print("artifacts:")
    for artifact in data["artifacts"]:
        status = "ok" if artifact["ok"] else "missing/invalid"
        print(f"  {status} {artifact['name']}: {artifact['absolute_path']}")
    return 0
