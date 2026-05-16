from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"
SRC_ROOT = REPO_ROOT / "src"


@dataclass(frozen=True)
class ModuleResult:
    module: str
    returncode: int
    elapsed: float
    stdout: str
    stderr: str


def discover_test_modules() -> list[str]:
    return [f"tests.{path.stem}" for path in sorted(TESTS_ROOT.glob("test_*.py"))]


def parse_jobs(value: str) -> int:
    if value == "auto":
        return max(1, min(os.cpu_count() or 1, 6))
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--jobs must be 'auto' or a positive integer") from exc
    if jobs < 1:
        raise argparse.ArgumentTypeError("--jobs must be 'auto' or a positive integer")
    return jobs


def run_module(module: str, *, verbose: bool) -> ModuleResult:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    command = [sys.executable, "-m", "unittest"]
    if verbose:
        command.append("-v")
    command.append(module)

    start = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return ModuleResult(module, proc.returncode, time.perf_counter() - start, proc.stdout, proc.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unittest modules in isolated parallel subprocesses.")
    parser.add_argument("--jobs", default="auto", type=parse_jobs, help="parallel jobs: auto or a positive integer")
    parser.add_argument("--verbose", action="store_true", help="run unittest modules with -v and print pass progress")
    parser.add_argument("modules", nargs="*", help="optional unittest module names; defaults to tests/test_*.py")
    args = parser.parse_args(argv)

    modules = args.modules or discover_test_modules()
    if not modules:
        print("No test modules found.", file=sys.stderr)
        return 1

    start = time.perf_counter()
    results: list[ModuleResult] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [executor.submit(run_module, module, verbose=args.verbose) for module in modules]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if args.verbose or result.returncode != 0:
                status = "ok" if result.returncode == 0 else f"FAIL({result.returncode})"
                print(f"{status:8s} {result.elapsed:7.2f}s {result.module}", flush=True)

    failures = [result for result in results if result.returncode != 0]
    elapsed = time.perf_counter() - start
    for result in sorted(results, key=lambda item: item.elapsed, reverse=True):
        if not args.verbose and result.returncode == 0:
            continue
        print(f"{result.elapsed:7.2f}s {result.module}")

    if failures:
        print("\nFailures:", file=sys.stderr)
        for result in sorted(failures, key=lambda item: item.module):
            print(f"\n=== {result.module} failed in {result.elapsed:.2f}s (rc={result.returncode}) ===", file=sys.stderr)
            if result.stdout:
                print("--- stdout ---", file=sys.stderr)
                print(result.stdout, file=sys.stderr, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print("--- stderr ---", file=sys.stderr)
                print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        print(f"\nFAILED {len(failures)}/{len(results)} modules in {elapsed:.2f}s", file=sys.stderr)
        return 1

    print(f"OK {len(results)} modules in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
