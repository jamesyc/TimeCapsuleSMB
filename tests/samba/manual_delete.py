"""Exercise macOS SMB first-attempt deletion on a mounted test share.

Run with an explicit mount point. Only uniquely named objects created by this
script are removed. Cleanup retries are reported separately from test results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def failure(error: Exception) -> dict:
    # Do not put the full hex-encoded xattr argument into a failure report.
    if isinstance(error, subprocess.SubprocessError):
        detail = (getattr(error, "stderr", b"") or b"").decode(errors="replace")
        return {"ok": False, "error": type(error).__name__ + ": " + detail}
    return {"ok": False, "error": str(error), "errno": getattr(error, "errno", None)}


def set_attribute(path: Path, value: bytes) -> None:
    subprocess.run(["xattr", "-wx", "com.example.tc263", value.hex(), str(path)],
                   check=True, capture_output=True, timeout=60)


def get_attribute(path: Path) -> bytes:
    value = subprocess.check_output(["xattr", "-px", "com.example.tc263", str(path)], timeout=60)
    return bytes.fromhex(value.decode())


def exercise(mount: Path, trials: int = 8) -> list[dict]:
    if not os.path.ismount(mount):
        raise ValueError(f"Not a mount point: {mount}")
    prefix = "tc263-" + uuid.uuid4().hex
    parent = mount / (prefix + "-parent")
    owned: list[Path] = [parent]
    results: list[dict] = []
    parent.mkdir()
    nested = parent / "nested"
    nested.mkdir()
    try:
        for depth, directory in enumerate((mount, parent, nested)):
            for kind in ("file", "directory", "rm_rf", "metadata_file", "metadata_directory"):
                for trial in range(trials):
                    path = directory / f"{prefix}-{kind}-{trial}"
                    owned.append(path)
                    result = {"depth": depth, "kind": kind, "trial": trial, "phase": "create"}
                    try:
                        if "directory" in kind or kind == "rm_rf":
                            path.mkdir()
                        else:
                            path.write_bytes(b"TimeCapsuleSMB regression\n")
                            assert path.read_bytes() == b"TimeCapsuleSMB regression\n"
                        result["phase"] = "metadata"
                        if kind.startswith("metadata"):
                            set_attribute(path, b"metadata")
                            assert get_attribute(path) == b"metadata"
                        result["phase"] = "delete"
                        if kind == "rm_rf":
                            (path / "child").write_bytes(b"child content")
                            proc = subprocess.run(["rm", "-rf", str(path)], check=False)
                            result["exit"] = proc.returncode
                            assert proc.returncode == 0
                        elif "directory" in kind:
                            path.rmdir()
                        else:
                            path.unlink()
                        assert not path.exists(), "first delete left object behind"
                        result["ok"] = True
                    except (OSError, AssertionError, subprocess.SubprocessError) as error:
                        result.update(failure(error))
                    results.append(result)
        path = mount / (prefix + "-large-stream")
        owned.append(path)
        result = {"kind": "stream_roundtrip_shrink", "depth": 0}
        try:
            path.write_bytes(b"base file must survive stream updates")
            for size in (90000, 70000, 32):
                result["phase"] = f"stream-{size}"
                value = bytes(i % 251 for i in range(size))
                set_attribute(path, value)
                assert get_attribute(path) == value
                assert path.read_bytes() == b"base file must survive stream updates"
            result["phase"] = "delete"
            path.unlink()
            assert not path.exists()
            result["ok"] = True
        except (OSError, AssertionError, subprocess.SubprocessError) as error:
            result.update(failure(error))
        results.append(result)
    finally:
        # The unfixed server may need a second cleanup attempt. Never let that
        # turn the recorded first-attempt failure into a passing test.
        for path in reversed(owned):
            for _ in range(3):
                if not path.exists():
                    break
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError:
                    pass
            if path.exists():
                results.append({"kind": "cleanup", "ok": False, "path": str(path)})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount", type=Path)
    parser.add_argument("--trials", type=int, default=8)
    args = parser.parse_args()
    results = exercise(args.mount.resolve(), args.trials)
    print(json.dumps(results, indent=2))
    raise SystemExit(any(not result["ok"] for result in results))


if __name__ == "__main__":
    main()
